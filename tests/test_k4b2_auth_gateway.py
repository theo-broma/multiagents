"""Slice 4B.2, K4B2.1..K4B2.3, K4B2.5 and IC-C28/IC-C29/IC-C30 for the auth
gateway: through the scheduler on the real Docker backend over the fake `docker`
CLI, with a provider that declares `needs.network` and `gateway_auth: anthropic`.

Contract: context/specs/phase7-part2-contract.md, "Slice 4B.2 decisions".
The fake establishes issuance, environment, ordering, receipts and failure
handling. It is NEVER evidence of containment: the handler's own answers are in
test_ic_c28_auth_handler.py and the real-Docker cases in test_k4b2_auth_docker.py.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402
import ic_s4b2 as ab  # noqa: E402
import ic_s4b2_backend as be  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import kind_of  # noqa: E402

CUSTOM_IMAGE = "registry.test/gateway:7"
UNPRIVILEGED = "MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED"
EVIL_KEY, EVIL_TOKEN, EVIL_URL = "sk-ant-fiction-key", "fiction-token", "http://evil.test:9"


@pytest.fixture
def asd(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    yield s
    s.close()


def env_of(s, node=None):
    node = node or s.launch()
    return s.guest_env(node)


def decided(s, node):
    def ready():
        runs = s.runs(node)
        c = runs and s.container_of(runs[-1]["attempt_id"])
        view = s.get(node)
        return (c and c["StartCount"] > 0) or view["state"] in ("held", "done") or None
    return s.wait(ready, nb.DECIDED_WAIT, f"{node}'s launch to be decided")


# ======================================================== K4B2.3: issuing the bearer

def test_k4b2_3_the_guest_gets_the_auth_base_url_and_a_fresh_ma_auth_v1_bearer(asd):
    asd.start()
    env = env_of(asd)
    assert env.get("ANTHROPIC_BASE_URL") == ab.AUTH_URL
    assert ab.BEARER.match(env.get("ANTHROPIC_AUTH_TOKEN", "")), env.get("ANTHROPIC_AUTH_TOKEN")


def test_k4b2_3_the_egress_credential_is_still_issued_and_is_a_different_secret(asd):
    asd.start()
    env = env_of(asd)
    user, password = nb.proxy_url(env)
    token = ab.bearer_of(env)
    assert password not in token and token not in env["HTTP_PROXY"]
    assert env["NO_PROXY"] == nb.NO_PROXY_VALUE, "ma-auth must be reached directly, not through the egress proxy"


def test_k4b2_3_each_attempt_has_its_own_bearer_and_a_retry_gets_a_fresh_one(asd):
    asd.start()
    a, b = asd.launch(), asd.launch()
    ba, bb = ab.bearer_of(asd.guest_env(a)), ab.bearer_of(asd.guest_env(b))
    assert ba != bb
    reply = asd.rpc("steer_run", {"run_id": asd.runs(a)[-1]["run_id"], "message": "again"}, timeout=nb.WAIT)
    assert reply.get("ok") is True, reply
    runs = asd.wait_runs(a, 2, nb.WAIT)
    container = asd.wait(lambda: asd.container_of(runs[-1]["attempt_id"]), nb.WAIT, "the second container")
    def written():
        try:
            return asd.invocation(container).get("env")
        except (FileNotFoundError, ValueError):
            return None
    again = asd.wait(written, nb.WAIT, "the second invocation")
    assert ab.bearer_of(again) not in (ba, bb)


def test_k4b2_3_the_bearer_is_random_not_derived_from_any_id(asd):
    asd.start()
    nodes = [asd.launch() for _ in range(3)]
    tokens = [ab.bearer_of(asd.guest_env(n)) for n in nodes]
    assert len(set(tokens)) == 3
    run = asd.runs(nodes[0])[-1]
    seeds = [run["attempt_id"], run["run_id"], nodes[0], asd.slug, "ma1-" + run["attempt_id"]]
    derived = set()
    for seed in seeds:
        for algo in (hashlib.sha256, hashlib.md5, hashlib.sha1, hashlib.sha512, hashlib.blake2b, hashlib.blake2s):
            derived.add(algo(seed.encode()).hexdigest())
        derived.add(seed.encode().hex())
    digits = tokens[0].removeprefix("ma-auth-v1.")
    assert digits not in derived and not any(digits.startswith(d[:16]) for d in derived)
    for token in tokens:
        assert len(set(token.removeprefix("ma-auth-v1."))) >= 8, token


@pytest.mark.parametrize("declared", [None, "none"])
def test_k4b2_3_a_provider_without_gateway_auth_anthropic_gets_no_bearer_and_no_auth_gateway(tmp_path, monkeypatch, declared):
    s = ab.AuthSched(tmp_path, monkeypatch, gateway_auth=declared)
    try:
        s.start()
        node = s.launch()
        env = s.guest_env(node)
        assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env, \
            {k: v for k, v in env.items() if k.startswith("ANTHROPIC")}
        assert nb.proxy_url(env), "the egress credential is unaffected"
        assert s.gateways("auth") == [], "an auth gateway was started for a provider that does not use one"
        assert len(s.egress_gateways()) == 1
    finally:
        s.close()


def test_k4b2_3_gateway_auth_without_a_network_need_issues_nothing(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(network=False)
        s.start()
        container = s.running(s.launch())
        env = s.invocation(container)["env"]
        assert container["HostConfig"]["NetworkMode"] == "none"
        assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env
        assert s.gateways("auth") == [] and s.fd.networks() == {}
    finally:
        s.close()


def test_k4b2_4_gateway_auth_does_not_lift_needs_credentials(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    try:
        s.provider_declares(credentials=True, network=True, rpc=False)
        s.start()
        node = s.launch()
        view = s.wait(lambda: (v := s.get(node)) and v["state"] in ("held", "failed", "done", "cancelled") and v,
                      nb.WAIT, "the refusal")
        assert view["state"] != "done"
        assert "credential_handle" in ic.text_of(ic.causes(view)).lower()
        assert s.fake.spawns() == 0
        assert s.fd.containers() == {} and s.fd.networks() == {}
    finally:
        s.close()


# ------------------------------------------------------------------ the scrubbing

SCRUBBED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
# K4B2.8: EVERY inherited ANTHROPIC_* variable is scrubbed, not only the three above.
EVIL_OTHERS = {"ANTHROPIC_MODEL": "fiction-model", "ANTHROPIC_CUSTOM_HEADERS": "X-Fiction: 1",
               "ANTHROPIC_DEFAULT_SONNET_MODEL": "fiction-sonnet", "ANTHROPIC_FICTION_FLAG": "fiction-flag"}
EVIL_ALL = {"ANTHROPIC_API_KEY": EVIL_KEY, "ANTHROPIC_AUTH_TOKEN": EVIL_TOKEN, "ANTHROPIC_BASE_URL": EVIL_URL,
            **EVIL_OTHERS}


def poison(s, monkeypatch, where):
    values = dict(EVIL_ALL)
    if where == "host":
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    else:
        s.fake.entry["env"] = dict(values)
        s.p.write()
        s._reset_server()


@pytest.mark.parametrize("where", ["host", "provider-overlay"])
@pytest.mark.parametrize("mode", ["anthropic", "none", "networkless", "undeclared"])
def test_k4b2_3_inherited_anthropic_overrides_are_scrubbed_for_every_isolated_attempt(tmp_path, monkeypatch, where, mode):
    if where == "host":
        for k, v in EVIL_ALL.items():
            monkeypatch.setenv(k, v)
    s = ab.AuthSched(tmp_path, monkeypatch, gateway_auth="anthropic" if mode == "anthropic" else "none")
    try:
        if where == "provider-overlay":
            poison(s, monkeypatch, where)
        if mode == "networkless":
            s.provider_declares(network=False)
        elif mode == "undeclared":
            # Only the auth protocol is undeclared: omitted needs would require
            # credentials by default (K17.2), which 4B.2 still refuses.
            s.provider_declares(network=True, credentials=False)
            s.set_gateway_auth(None)
        s.start()
        env = s.invocation(s.running(s.launch()))["env"]
        assert "ANTHROPIC_API_KEY" not in env, "an inherited API key reached the guest"
        text = json.dumps(env)
        assert EVIL_KEY not in text and EVIL_TOKEN not in text and EVIL_URL not in text, \
            {k: env[k] for k in SCRUBBED if k in env}
        for name, value in EVIL_OTHERS.items():
            assert name not in env and value not in text, f"K4B2.8: inherited {name} reached the guest"
        if mode == "anthropic":
            assert env["ANTHROPIC_BASE_URL"] == ab.AUTH_URL and ab.BEARER.match(env["ANTHROPIC_AUTH_TOKEN"])
        else:
            assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env
        assert {k for k in env if k.startswith("ANTHROPIC_")} <= (
            {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} if mode == "anthropic" else set()), \
            {k for k in env if k.startswith("ANTHROPIC_")}
    finally:
        s.close()


# ---------------------------------------------- no credential material in public places

@pytest.mark.parametrize("phase", ["running", "ended", "failed_exit", "cancelled", "restarted", "disposed"])
def test_k4b2_3_no_public_or_durable_surface_holds_an_issued_bearer(asd, phase):
    asd.start()
    node = asd.launch()
    container = asd.running(node)
    env = asd.invocation(container)["env"]
    token = ab.bearer_of(env)
    secrets = [token, "Bearer " + token,
               nb.proxy_url(env)[1], env["HTTP_PROXY"]]
    if phase == "ended":
        asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
        nb.wait_done(asd, node)
    elif phase == "failed_exit":
        asd.fd.finish(container["Id"], 2, stderr=b"it failed\n")
        asd.wait(lambda: asd.get(node)["state"] in ("done", "held", "failed") or None, nb.WAIT, "the end")
    elif phase == "cancelled":
        assert asd.cancel_raw(node)["ok"] is True
        asd.wait(lambda: asd.get(node)["state"] in ("cancelled", "held", "failed", "done") or None, nb.WAIT, "the end")
    elif phase == "restarted":
        asd.restart(hard=True)
    elif phase == "disposed":
        asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
        nb.wait_done(asd, node)
        reply = asd.rpc("dispose_node", {"id": node, "revision": asd.get(node)["revision"]}, timeout=nb.WAIT)
        assert reply.get("ok") is True, reply
    texts = asd.public_texts(node)
    assert nb.leaks(texts, *secrets) == {}
    assert ab.bearer_leaks(texts, *secrets) == {}


def test_k4b2_3_the_bearer_is_in_the_invocation_input_only(asd):
    asd.start()
    container = asd.running(asd.launch())
    token = ab.bearer_of(asd.invocation(container)["env"])
    assert token not in str(container["Config"]) and token not in str(container["CreateArgv"])
    assert token not in " ".join(" ".join(c) for c in asd.fd.calls())
    holders = []
    for mount in nb.fdk.mounts_of(container):
        if mount["destination"] == "/multiagents-input":
            continue
        source = Path(mount["source"])
        files = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()] if source.is_dir() else []
        for path in files:
            try:
                if token.encode() in path.read_bytes():
                    holders.append(str(path))
            except OSError:
                pass
    assert holders == [], f"the bearer reached a writable guest location: {holders}"


def test_k4b2_3_a_failed_launch_leaks_no_bearer_in_its_error(asd):
    asd.start()
    first = asd.launch()
    asd.running(first)
    ab.bearer_of(asd.guest_env(first))               # the run under test does hold a bearer
    asd.fd.rule("network", "connect", effect="fail", stderr="Error response from daemon: attach failed")
    second = asd.launch()
    asd.wait(lambda: asd.runs(second) or asd.get(second)["state"] != "open" or None, nb.WAIT, "a decision")
    assert ab.bearer_leaks(asd.public_texts(second)) == {}


# ------------------------------------------------------------- the redactor (D-S4B.7)

def entrypoint_of(container) -> str:
    argv = container["CreateArgv"]
    assert "-c" in argv, f"D-S4B.7: cannot find the entrypoint script in the create argv: {argv[:8]}"
    return argv[argv.index("-c") + 1]


class Guest:
    """The host's own invocation input and entrypoint, run on this machine."""

    def __init__(self, s, tmp_path):
        s.start()
        container = s.running(s.launch())
        self.script = entrypoint_of(container)
        source = Path({m["destination"]: m for m in nb.fdk.mounts_of(container)}["/multiagents-input"]["source"])
        self.input = tmp_path / "guest-input"
        shutil.copytree(source, self.input)
        self.invocation = json.loads((self.input / "invocation.json").read_text())
        env = self.invocation["env"]
        self.token = ab.bearer_of(env)
        self.prefix = self.invocation["output_prefix"]
        self.cwd = tmp_path / "guest-cwd"
        self.cwd.mkdir()

    def run(self, body: str):
        child = ("import os, sys, time\n"
                 "def w(data, stream=sys.stdout, pause=0.12):\n"
                 "    if isinstance(data, str): data = data.encode()\n"
                 "    stream.buffer.write(data); stream.buffer.flush(); time.sleep(pause)\n"
                 "tok = os.environ['ANTHROPIC_AUTH_TOKEN']\n" + textwrap.dedent(body))
        invocation = dict(self.invocation, argv=[sys.executable, "-c", child], cwd=str(self.cwd), stdin=None)
        (self.input / "invocation.json").write_text(json.dumps(invocation))
        script = self.script.replace("/multiagents-input", str(self.input))
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=20, cwd=self.cwd,
                              env=dict(os.environ, **{UNPRIVILEGED: "1"}))
        assert proc.returncode == 0, (proc.returncode, proc.stderr[-400:])
        return self.decode(proc.stdout), self.decode(proc.stderr)

    def decode(self, framed: bytes) -> bytes:
        out = b""
        for line in framed.splitlines():
            assert line.startswith(self.prefix.encode()), line[:80]
            out += base64.b64decode(line[len(self.prefix):])
        return out


@pytest.fixture
def guest(asd, tmp_path):
    return Guest(asd, tmp_path)


def test_k4b2_3_a_bearer_printed_in_one_write_is_redacted(guest):
    out, _ = guest.run("w('before ' + tok + ' after\\n')")
    assert guest.token.encode() not in out
    assert out == b"before [REDACTED] after\n"


def test_k4b2_3_the_authorization_header_form_is_redacted(guest):
    out, _ = guest.run("w('Authorization: Bearer ' + tok + '\\n')")
    assert guest.token.encode() not in out and b"ma-auth-v1" not in out
    assert out.startswith(b"Authorization: ") and b"[REDACTED]" in out


@pytest.mark.parametrize("cut", [1, 5, 10, 11, 12, 43, 74])
def test_k4b2_3_a_bearer_split_across_two_writes_is_still_redacted(guest, cut):
    out, _ = guest.run(f"w('A:' + tok[:{cut}]); w(tok[{cut}:] + ':Z\\n')")
    assert guest.token.encode() not in out
    assert out == b"A:[REDACTED]:Z\n"


def test_k4b2_3_a_bearer_split_in_its_bearer_header_form_is_redacted(guest):
    out, _ = guest.run("h = 'Bearer ' + tok\nw(h[:4]); w(h[4:20]); w(h[20:] + '\\n')")
    assert b"ma-auth-v1" not in out and guest.token.encode() not in out and b"[REDACTED]" in out


def test_k4b2_3_the_bearer_on_stderr_is_redacted_with_its_own_tail(guest):
    out, err = guest.run("w(tok[:30], sys.stdout); w('between\\n', sys.stderr); w(tok[30:] + '\\n', sys.stdout)\n"
                         "w('err:' + tok + '\\n', sys.stderr)")
    assert guest.token.encode() not in out + err and b"[REDACTED]" in out and b"[REDACTED]" in err


def test_k4b2_3_a_near_miss_of_the_bearer_is_not_redacted(guest):
    out, _ = guest.run("w('x ' + tok[:-1] + ('0' if tok[-1] != '0' else '1') + ' y\\n')")
    assert b"[REDACTED]" not in out and out.startswith(b"x ma-auth-v1.")


def test_k4b2_3_an_incomplete_bearer_prefix_at_the_end_is_emitted_unchanged(guest):
    out, _ = guest.run("w('tail ma-auth-v1.' + tok[11:20])")
    assert out == b"tail ma-auth-v1." + guest.token[11:20].encode()


def test_k4b2_3_the_egress_secrets_are_still_redacted_beside_the_bearer(guest):
    out, _ = guest.run("w(os.environ['HTTP_PROXY'] + ' ' + tok + '\\n')")
    assert out == b"[REDACTED] [REDACTED]\n"


# ================================================= K4B2.1: two services, two gateways

def test_k4b2_1_each_service_has_its_own_running_gateway_on_the_attempt_network(asd):
    node, run, container, network = asd.pair()
    egress, auth = asd.gateway("egress"), asd.gateway("auth")
    assert egress["Id"] != auth["Id"]
    assert egress["State"]["Running"] and auth["State"]["Running"]
    assert {egress["Id"], auth["Id"], container["Id"]} == set(asd.fd.members(network["Id"]))
    for gw, alias in ((egress, "ma-egress"), (auth, "ma-auth")):
        assert alias in asd.fd.networks_of(gw["Id"])[network["Name"]]["Aliases"]
    assert "ma-auth" not in asd.fd.networks_of(egress["Id"])[network["Name"]]["Aliases"]
    assert "ma-egress" not in asd.fd.networks_of(auth["Id"])[network["Name"]]["Aliases"]


def _receipts(value, key):
    """Every mapping recorded under `key` anywhere in the evidence."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and isinstance(v, dict):
                found.append(v)
            found += _receipts(v, key)
    elif isinstance(value, list):
        for v in value:
            found += _receipts(v, key)
    return found


def test_k4b2_8_the_auth_receipt_has_the_egress_keys_and_is_recorded_under_the_service_name(asd):
    node, run, container, network = asd.pair()
    evidence = asd.evidence(run["attempt_id"])
    found = _receipts(evidence, "auth")
    auth_receipt = found[0] if found else None
    assert auth_receipt is not None, f"no receipt recorded under 'auth': {sorted(evidence.get('executor', {}))}"
    gw = asd.gateway("auth")
    text = json.dumps(auth_receipt)
    assert gw["Id"] in text, "the receipt does not name the auth gateway container"
    assert isinstance(auth_receipt.get("policy_digest"), str) and auth_receipt["policy_digest"], auth_receipt
    assert auth_receipt.get("gateway_generation") is not None, auth_receipt
    assert network["Id"] in text or network["Name"] in text, "the receipt does not record the desired network"
    assert any("start" in k for k in auth_receipt), f"no start time recorded: {sorted(auth_receipt)}"
    egress = evidence["executor"]["network"]
    assert auth_receipt["policy_digest"] != egress["policy_digest"], "the auth receipt must be its own, not the egress one"
    assert ab.bearer_leaks({"evidence": json.dumps(evidence)}, ab.bearer_of(asd.guest_env(node))) == {}


def test_k4b2_1_the_gateways_carry_their_own_labels_and_names(asd):
    asd.pair()
    egress, auth = asd.gateway("egress"), asd.gateway("auth")
    assert egress["Config"]["Labels"].get("multiagents.gateway") == "egress"
    assert auth["Config"]["Labels"].get("multiagents.gateway") == "auth"
    assert auth["Config"]["Labels"].get("multiagents.project") == asd.slug
    assert egress["Name"] != auth["Name"]


def test_k4b2_1_the_auth_gateway_is_created_with_the_egress_gateways_confinement(asd):
    asd.pair()
    auth = asd.gateway("auth")
    argv = auth["CreateArgv"]
    flat = " ".join(argv)
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in flat
    assert not any(a in ("-p", "--publish", "-P", "--publish-all") for a in argv), "a gateway port was published"
    for forbidden in ("--network host", "--pid host", "--ipc host", "--privileged", "docker.sock"):
        assert forbidden not in flat, forbidden
    assert auth["HostConfig"]["NetworkMode"] not in ("host",)
    assert auth["Config"]["Image"] == nb.GATEWAY_IMAGE
    for mount in nb.fdk.mounts_of(auth):
        assert "docker.sock" not in mount["source"] + mount["destination"]
        assert "vault" not in mount["source"].lower(), f"the auth gateway mounts a vault path: {mount}"


def test_k4b2_1_two_attempts_share_one_auth_gateway_attached_to_both_networks(asd):
    asd.start()
    a, b = asd.launch(), asd.launch()
    asd.running(a), asd.running(b)
    nets = [asd.attempt_networks(asd.runs(n)[-1]["attempt_id"])[0] for n in (a, b)]
    auth = asd.gateway("auth")
    assert auth["StartCount"] == 1
    for net in nets:
        assert auth["Id"] in asd.fd.members(net["Id"])
        assert "ma-auth" in asd.fd.networks_of(auth["Id"])[net["Name"]]["Aliases"]
    assert ab.bearer_of(asd.guest_env(a)) != ab.bearer_of(asd.guest_env(b))


def test_k4b2_1_an_unchanged_configuration_keeps_the_same_auth_gateway(asd):
    asd.start()
    a = asd.launch()
    asd.running(a)
    first = asd.gateway("auth")
    asd.running(asd.launch())
    after = asd.gateway("auth")
    assert (after["Id"], after["StartCount"]) == (first["Id"], 1)


def test_k4b2_1_the_auth_gateway_uses_the_configured_gateway_image(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch, gateway_image=CUSTOM_IMAGE, images=[nb.GATEWAY_IMAGE, CUSTOM_IMAGE])
    try:
        s.start()
        container = s.running(s.launch())
        assert {g["Config"]["Image"] for g in s.gateways("auth")} == {CUSTOM_IMAGE}
        assert container["Config"]["Image"] == nb.GATEWAY_IMAGE
    finally:
        s.close()


def test_k4b2_1_reconcile_has_one_row_per_attempt_for_each_service_and_both_are_ready(asd):
    node, run, container, network = asd.pair()
    for service in ("egress", "auth"):
        rows = asd.rows_of(service)
        assert [r["attempt_id"] for r in rows] == [run["attempt_id"]], (service, rows)
        assert rows[0]["state"] == "ready" and set(rows[0]) >= {"attempt_id", "state", "reason"}


def test_k4b2_1_the_auth_registry_issues_the_same_grant_again_and_serialises_no_secret(asd):
    node, run, container, network = asd.pair()
    identity = ic.lifecycle().ResourceIdentity(container["Id"])
    registry = asd.registry("auth")
    first = ic.run(registry.grant(run["attempt_id"], identity))
    again = ic.run(registry.grant(run["attempt_id"], identity))
    grant_type = nb.have("GatewayGrant")
    assert isinstance(first, grant_type), first
    assert first.id == again.id and dict(first.environment) == dict(again.environment)
    guest = asd.invocation(container)["env"]
    assert first.environment["ANTHROPIC_AUTH_TOKEN"] == guest["ANTHROPIC_AUTH_TOKEN"]
    assert first.environment["ANTHROPIC_BASE_URL"] == ab.AUTH_URL
    token = guest["ANTHROPIC_AUTH_TOKEN"]
    assert token not in repr(first) and token not in str(first)
    egress = ic.run(asd.registry("egress").grant(run["attempt_id"], identity))
    assert egress.id != first.id, "each service has its own grant namespace"
    assert "ANTHROPIC_AUTH_TOKEN" not in egress.environment
    assert "HTTP_PROXY" not in first.environment


def test_k4b2_1_the_auth_registry_refuses_a_foreign_container_and_an_unowned_attempt(asd):
    node, run, container, network = asd.pair()
    other = asd.fd.inject("someone-else", {"multiagents.owner": "att_" + "9" * 32}, status="created")
    conflict = ic.run(asd.registry("auth").grant(run["attempt_id"], ic.lifecycle().ResourceIdentity(other)))
    assert kind_of(conflict) == "identity_conflict", conflict
    unowned = ic.run(asd.registry("auth").grant("att_" + "0" * 32, ic.lifecycle().ResourceIdentity("0" * 64)))
    assert kind_of(unowned) == "invalid_spec", unowned


def test_k4b2_3_the_auth_registry_issues_no_grant_for_a_provider_without_gateway_auth(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch, gateway_auth="none")
    try:
        s.start()
        node = s.launch()
        container = s.running(node)
        run = s.runs(node)[-1]
        got = ic.run(s.registry("auth").grant(run["attempt_id"], ic.lifecycle().ResourceIdentity(container["Id"])))
        assert got is None, f"K4B2.8: grant() returns None (not an error) without gateway_auth; got {got!r}"
        assert s.gateways("auth") == []
        assert ab.bearer_leaks(s.public_texts(node)) == {}
    finally:
        s.close()


# =============================================== IC-C29 for the auth gateway: revocation

def test_k4b2_1_revoking_the_auth_service_leaves_the_egress_service_untouched(asd):
    node, run, container, network = asd.pair()
    env = asd.invocation(container)["env"]
    auth, egress = asd.gateway("auth"), asd.gateway("egress")
    assert ic.run(asd.registry("auth").revoke(run["attempt_id"])) is None
    assert auth["Id"] not in asd.fd.members(network["Id"]), "revoke must remove the auth gateway's ingress"
    assert egress["Id"] in asd.fd.members(network["Id"]), "the egress gateway was disconnected by an auth revoke"
    assert asd.state_of("auth", run["attempt_id"]) == "revoked"
    assert asd.state_of("egress", run["attempt_id"]) == "ready"
    after = asd.fd.container(container["Id"])
    assert after["State"]["Running"] and after["StartCount"] == 1 and asd.invocation(after)["env"] == env
    assert ic.run(asd.registry("auth").revoke(run["attempt_id"])) is None, "revoking again is a success"


def test_k4b2_1_revoking_the_egress_service_leaves_the_auth_service_untouched(asd):
    node, run, container, network = asd.pair()
    auth, egress = asd.gateway("auth"), asd.gateway("egress")
    assert ic.run(asd.registry("egress").revoke(run["attempt_id"])) is None
    assert egress["Id"] not in asd.fd.members(network["Id"])
    assert auth["Id"] in asd.fd.members(network["Id"]), "the auth gateway was disconnected by an egress revoke"
    assert asd.state_of("egress", run["attempt_id"]) == "revoked"
    assert asd.state_of("auth", run["attempt_id"]) == "ready"


def test_k4b2_1_revoking_one_attempt_leaves_every_other_attempts_auth_authority(asd):
    asd.start()
    a, b = asd.launch(), asd.launch()
    asd.running(a), asd.running(b)
    ra, rb = asd.runs(a)[-1], asd.runs(b)[-1]
    nets = {r["attempt_id"]: asd.attempt_networks(r["attempt_id"])[0] for r in (ra, rb)}
    assert ic.run(asd.registry("auth").revoke(ra["attempt_id"])) is None
    assert asd.gateway("auth")["Id"] in asd.fd.members(nets[rb["attempt_id"]]["Id"])
    assert asd.state_of("auth", rb["attempt_id"]) == "ready"
    assert asd.state_of("auth", ra["attempt_id"]) == "revoked"


def test_k4b2_1_a_revoked_auth_grant_is_not_resurrected_by_a_restart_or_a_new_grant(asd):
    node, run, container, network = asd.pair()
    identity = ic.lifecycle().ResourceIdentity(container["Id"])
    ic.run(asd.registry("auth").revoke(run["attempt_id"]))
    asd.restart(hard=True)
    assert asd.state_of("auth", run["attempt_id"]) in ("revoked", "absent", None)
    again = ic.run(asd.registry("auth").grant(run["attempt_id"], identity))
    assert not isinstance(again, nb.have("GatewayGrant")), "a revoked grant was issued again"
    assert asd.state_of("egress", run["attempt_id"]) == "ready"


def test_k4b2_1_a_normal_end_revokes_both_services_and_detaches_both_gateways_before_the_network_goes(asd):
    node, run, container, network = asd.pair()
    auth, egress = asd.gateway("auth"), asd.gateway("egress")
    asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    assert nb.wait_done(asd, node)["state"] == "done"
    asd.wait(lambda: asd.fd.network(network["Id"]) is None or None, nb.WAIT, "the network to be removed")
    commands = asd.fd.commands()
    removal = max(i for i, c in enumerate(commands) if c[:2] in (["network", "rm"], ["network", "remove"])
                  and (network["Name"] in c[2:] or network["Id"] in c[2:]))
    for member in (container, auth, egress):
        positions = [i for i, c in enumerate(commands) if c[:2] == ["network", "disconnect"]
                     and (network["Name"] in c or network["Id"] in c)
                     and (member["Id"] in c or member["Name"].lstrip("/") in c)]
        assert positions and max(positions) < removal, (member["Name"], positions, removal)
    for service in ("egress", "auth"):
        assert asd.state_of(service, run["attempt_id"]) in (None, "absent", "revoked"), service
    kept = asd.fd.container(container["Id"])
    assert kept is not None and kept["State"]["Status"] == "exited"


def test_k4b2_1_the_auth_gateway_survives_an_attempts_end_and_other_attempts_keep_it(asd):
    asd.start()
    a, b = asd.launch(), asd.launch()
    ca, cb = asd.running(a), asd.running(b)
    ra, rb = asd.runs(a)[-1], asd.runs(b)[-1]
    na, nb_ = asd.attempt_networks(ra["attempt_id"])[0], asd.attempt_networks(rb["attempt_id"])[0]
    env = asd.invocation(cb)["env"]
    asd.fd.finish(ca["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(asd, a)
    asd.wait(lambda: asd.fd.network(na["Id"]) is None or None, nb.WAIT, "A's network to be removed")
    assert asd.has_gateway("auth", nb_) and asd.has_gateway("egress", nb_)
    assert asd.invocation(cb)["env"] == env
    assert asd.state_of("auth", rb["attempt_id"]) == "ready"


@pytest.mark.parametrize("code", [0, 1, 137])
def test_k4b2_1_every_kind_of_exit_leaves_no_ready_auth_grant(asd, code):
    node, run, container, network = asd.pair()
    asd.fd.finish(container["Id"], code, stdout=s3.events_stdout() if code == 0 else b"")
    asd.wait(lambda: asd.fd.network(network["Id"]) is None or None, nb.WAIT, "the network to be removed")
    assert asd.state_of("auth", run["attempt_id"]) in (None, "absent", "revoked")


def test_k4b2_1_a_cancelled_attempt_has_both_services_revoked_before_it_is_stopped(asd):
    node, run, container, network = asd.pair()
    asd.fd.rule("stop", effect="hang", seconds=4, times=1)
    asd.fd.rule("kill", effect="hang", seconds=4, times=1)
    assert asd.cancel_raw(node)["ok"] is True
    def both():
        return all(asd.state_of(s, run["attempt_id"]) == "revoked" for s in ("egress", "auth")) or None
    assert asd.wait(both, nb.SHORT, "both services to be revoked while the stop is pending")
    assert asd.fd.container(container["Id"])["State"]["Running"], "revocation must precede the stop"


def test_k4b2_1_cancellation_stops_the_container_even_when_the_auth_gateway_is_down(asd):
    node, run, container, network = asd.pair()
    asd.fd.finish(asd.gateway("auth")["Id"], 137)
    assert asd.cancel_raw(node)["ok"] is True
    assert asd.wait(lambda: not asd.fd.container(container["Id"])["State"]["Running"] or None, nb.WAIT,
                    "the cancelled attempt to be stopped despite the auth gateway outage")
    assert asd.state_of("auth", run["attempt_id"]) != "ready"


def test_k4b2_1_unknown_liveness_grants_no_auth_access_and_recovers_with_the_same_bearer(asd):
    node, run, container, network = asd.pair()
    env = asd.invocation(container)["env"]
    asd.fd.daemon_down()
    got = ic.run(asd.registry("auth").reconcile())
    if isinstance(got, list):
        assert all(r["state"] != "ready" for r in got if r["attempt_id"] == run["attempt_id"]), got
    else:
        assert kind_of(got) == "unknown", got
    asd.fd.daemon_down(False)
    assert asd.state_of("auth", run["attempt_id"]) == "ready"
    assert asd.invocation(asd.fd.container(container["Id"]))["env"] == env


def test_k4b2_1_adoption_after_a_hard_restart_keeps_the_gateway_and_the_recorded_bearer(asd):
    node, run, container, network = asd.pair()
    env = asd.invocation(container)["env"]
    gateway = asd.gateway("auth")
    asd.restart(hard=True)
    after = asd.fd.container(container["Id"])
    assert after["StartCount"] == 1 and after["State"]["Running"]
    assert asd.invocation(after)["env"] == env, "restart must not mint a new bearer"
    assert asd.gateway("auth")["Id"] == gateway["Id"], "restart replaced a healthy auth gateway"
    for service in ("egress", "auth"):
        assert asd.state_of(service, run["attempt_id"]) == "ready"
    assert asd.has_gateway("auth", network)


def test_k4b2_1_an_unexpected_member_withholds_readiness_from_the_auth_service(asd):
    node, run, container, network = asd.pair()
    intruder = asd.fd.inject("external", {"somebody": "else"}, status="running")
    asd.fd.attach(network["Name"], intruder)
    assert asd.state_of("auth", run["attempt_id"]) != "ready"
    assert intruder in asd.fd.members(network["Id"]), "an unknown external member was force-disconnected"


def test_k4b2_1_an_impostor_holding_the_ma_auth_alias_is_never_trusted_or_removed(asd):
    node, run, container, network = asd.pair()
    impostor = asd.fd.inject("impostor", {"unrelated": "yes"}, status="running")
    asd.fd.attach(network["Name"], impostor, aliases=["ma-auth"])
    asd.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
    nb.wait_done(asd, node)
    def reported():
        listing = json.loads(asd.cli("scheduler", "orphans", timeout=nb.WAIT).stdout)
        return any(o.get("resource_id") == network["Id"] for o in listing["orphans"] or []) or None
    asd.wait(reported, nb.WAIT, "the undeletable network to be reported")
    assert asd.fd.network(network["Id"]) is not None
    assert impostor in asd.fd.members(network["Id"])


# ======================================== IC-C30 for the auth gateway: replacement

def second_image_sched(tmp_path, monkeypatch):
    return ab.AuthSched(tmp_path, monkeypatch, images=[nb.GATEWAY_IMAGE, CUSTOM_IMAGE])


def change_policy(s):
    s.configure(gateway_image=CUSTOM_IMAGE)


def test_k4b2_1_a_changed_policy_replaces_the_auth_gateway_without_recreating_running_attempts(tmp_path, monkeypatch):
    s = second_image_sched(tmp_path, monkeypatch)
    try:
        node, run, container, network = s.pair()
        env = s.invocation(container)["env"]
        before = s.gateway("auth")
        change_policy(s)
        second = s.launch()
        s.running(second)
        rb = s.runs(second)[-1]
        nb2 = s.attempt_networks(rb["attempt_id"])[0]
        after = s.gateway("auth")
        assert after["Config"]["Image"] == CUSTOM_IMAGE and after["Id"] != before["Id"]
        for net in (network, nb2):
            assert s.has_gateway("auth", net) and s.has_gateway("egress", net)
            assert "ma-auth" in s.fd.networks_of(after["Id"])[net["Name"]]["Aliases"], "aliases are stable"
        kept = s.fd.container(container["Id"])
        assert kept["Id"] == container["Id"] and kept["StartCount"] == 1 and kept["State"]["Running"]
        assert s.invocation(kept)["env"] == env, "replacement keeps the bearer stable"
        assert s.state_of("auth", run["attempt_id"]) == "ready"
        assert len(s.gateways("auth")) == 1 and len(s.gateways("egress")) == 1
    finally:
        s.close()


FAILING = [(("create",), "gateway=auth"), (("network", "connect"), "ma-auth"), (("start",), None),
           (("stop",), None), (("rm",), None)]


@pytest.mark.parametrize("failing,contains", FAILING)
def test_k4b2_1_a_failure_at_any_auth_replacement_step_never_launches_on_a_stale_gateway_and_recovers(
        tmp_path, monkeypatch, failing, contains):
    s = second_image_sched(tmp_path, monkeypatch)
    try:
        node, run, container, network = s.pair()
        change_policy(s)
        s.fd.rule(*failing, effect="fail", times=1, stderr="Error response from daemon: injected failure",
                  **({"contains": contains} if contains else {}))
        second = s.launch()
        decided(s, second)
        for c in s.fd.containers().values():
            for snapshot in c.get("StartSnapshots", []):
                for members in snapshot.values():
                    running = [cid for cid, up in members.items() if up and cid != c["Id"]]
                    assert len(running) >= 2, f"started without both running gateways: {snapshot}"
        assert s.fd.container(container["Id"])["State"]["Running"] and s.fd.container(container["Id"])["StartCount"] == 1
        s.fd.clear_rules()
        third = s.launch()
        s.running(third)
        rt = s.attempt_networks(s.runs(third)[-1]["attempt_id"])[0]
        for net in (network, rt):
            assert s.has_gateway("auth", net) and s.has_gateway("egress", net)
        assert len(s.gateways("auth")) == 1 and len(s.gateways("egress")) == 1
    finally:
        s.close()


def test_k4b2_1_an_auth_gateway_that_died_is_not_trusted_for_a_new_launch(asd):
    node, run, container, network = asd.pair()
    asd.fd.finish(asd.gateway("auth")["Id"], 137)
    second = asd.launch()
    cb = asd.running(second)
    (members,) = list(cb["StartSnapshots"][0].values())
    assert len([cid for cid, up in members.items() if up and cid != cb["Id"]]) >= 2, members
    rb = asd.runs(second)[-1]
    for net in (network, asd.attempt_networks(rb["attempt_id"])[0]):
        assert asd.has_gateway("auth", net)
    assert len(asd.gateways("auth")) == 1


def test_k4b2_1_a_failed_auth_attachment_rolls_back_only_that_attempt(asd):
    node, run, container, network = asd.pair()
    asd.fd.rule("network", "connect", contains="ma-auth", effect="fail", times=6,
                stderr="Error response from daemon: cannot attach")
    second = asd.launch()
    decided(asd, second)
    for r in asd.runs(second):
        c = asd.container_of(r["attempt_id"])
        assert c is None or c["StartCount"] == 0, "an attempt whose auth attachment failed was started"
    assert asd.fd.container(container["Id"])["State"]["Running"]
    assert asd.has_gateway("auth", network)
    assert asd.state_of("auth", run["attempt_id"]) == "ready"
    assert ab.bearer_leaks(asd.public_texts(second)) == {}


# ==================================================== K4B2.5: the test backend seam

def test_k4b2_5_a_backend_factory_that_fails_refuses_the_launch_and_never_falls_back(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch, backend_spec=be.BROKEN_SPEC)
    try:
        s.start()
        node = s.launch()
        decided(s, node)
        started = [c for c in s.fd.containers().values() if c.get("StartSnapshots") or c["State"]["Running"]]
        assert started == [], "an attempt started although the synthetic upstream factory fails"
        assert s.get(node)["state"] in ("held", "failed"), s.get(node)["state"]
        assert ab.bearer_leaks(s.public_texts(node)) == {}
    finally:
        s.close()


def test_k4b2_5_without_the_variable_the_real_upstream_is_used_and_launch_proceeds(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch, backend_spec=None)
    try:
        s.start()
        env = s.invocation(s.running(s.launch()))["env"]
        assert ab.BEARER.match(env.get("ANTHROPIC_AUTH_TOKEN", ""))
        assert len(s.gateways("auth")) == 1
    finally:
        s.close()


@pytest.mark.parametrize("where", ["host", "provider-overlay"])
def test_k4b2_5_the_backend_variable_is_stripped_from_every_run_environment(tmp_path, monkeypatch, where):
    s = ab.AuthSched(tmp_path, monkeypatch, backend_spec=None)
    try:
        if where == "host":
            monkeypatch.setenv(ab.BACKEND_ENV, be.SPEC)
        else:
            s.fake.entry["env"] = {ab.BACKEND_ENV: be.SPEC}
            s.p.write()
            s._reset_server()
        s.start()
        container = s.running(s.launch())
        assert ab.BACKEND_ENV not in s.invocation(container)["env"]
        assert not any(e.partition("=")[0] == ab.BACKEND_ENV for e in container["Config"].get("Env") or [])
        assert ab.bearer_of(s.invocation(container)["env"])
    finally:
        s.close()


def test_k4b2_5_a_provider_overlay_cannot_install_a_failing_backend(tmp_path, monkeypatch):
    s = ab.AuthSched(tmp_path, monkeypatch)
    try:
        s.fake.entry["env"] = {ab.BACKEND_ENV: be.BROKEN_SPEC}
        s.p.write()
        s._reset_server()
        s.start()
        container = s.running(s.launch())
        assert ab.bearer_of(s.invocation(container)["env"]), "a guest-controlled variable changed the launch"
    finally:
        s.close()
