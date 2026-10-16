"""D-S4B.7 (slice 4B.1): no credential material in durable or public places, and
producer-side redaction in the PID-1 entrypoint.

Contract: context/specs/phase7-part2-contract.md, D-S4B.7 and the 4B test
prerequisites. "Logging approval": nothing here accepts display-only redaction.

Two kinds of test:

* public surfaces (fake Docker CLI): after each phase of an attempt the issued
  password appears in no log, event, argv, error body, evidence, label, status,
  orphan row or node view.
* the redactor: the entrypoint the host really passes to `docker create` is
  extracted from the created container and RUN on this machine against the very
  invocation input the host wrote, with a child process that prints the issued
  credential across write boundaries. What leaves the entrypoint is what
  Docker's json-file log would hold. UID separation, checkpoints and the real
  log path are in test_ic_s4b_docker.py.
"""
import base64
import json
import time
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s3 as s3  # noqa: E402
import ic_s4b as nb  # noqa: E402

RUN_TIMEOUT = 20
UNPRIVILEGED = "MULTIAGENTS_TEST_REDACTOR_UNPRIVILEGED"


@pytest.fixture
def ns(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    yield s
    s.close()


# ---------------------------------------------------------- public surfaces

def secrets_of(s, container):
    env = s.invocation(container)["env"]
    user, password = nb.proxy_url(env)
    return [password, env["HTTP_PROXY"], nb.basic(user, password).split()[1]]


@pytest.mark.parametrize("phase", ["running", "ended", "failed_exit", "cancelled", "restarted", "disposed"])
def test_d_s4b_7_no_public_or_durable_surface_holds_an_issued_credential(ns, phase):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    secrets = secrets_of(ns, container)
    if phase == "ended":
        ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
        nb.wait_done(ns, node)
    elif phase == "failed_exit":
        ns.fd.finish(container["Id"], 2, stderr=b"it failed\n")
        ns.wait(lambda: ns.get(node)["state"] in ("done", "held", "failed") or None, nb.WAIT, "the end")
    elif phase == "cancelled":
        assert ns.cancel_raw(node)["ok"] is True
        ns.wait(lambda: ns.get(node)["state"] in ("cancelled", "held", "failed", "done") or None, nb.WAIT, "the end")
    elif phase == "restarted":
        ns.restart(hard=True)
    elif phase == "disposed":
        ns.fd.finish(container["Id"], 0, stdout=s3.events_stdout())
        nb.wait_done(ns, node)
        reply = ns.rpc("dispose_node", {"id": node, "revision": ns.get(node)["revision"]}, timeout=nb.WAIT)
        assert reply.get("ok") is True, reply
    found = nb.leaks(ns.public_texts(node), *secrets)
    assert found == {}, found


def test_d_s4b_7_a_failed_launch_leaks_no_credential_in_its_error(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.start()
        first = s.launch()
        s.running(first)
        s.fd.rule("network", "connect", effect="fail", stderr="Error response from daemon: attach failed")
        second = s.launch()
        s.wait(lambda: s.runs(second) or s.get(second)["state"] != "open" or None, nb.WAIT, "a decision")
        assert nb.leaks(s.public_texts(second)) == {}
    finally:
        s.close()


def test_d_s4b_7_the_credential_is_in_the_invocation_input_and_nowhere_in_the_workspace_or_conversation(ns):
    ns.start()
    node = ns.launch()
    container = ns.running(node)
    password = nb.proxy_url(ns.invocation(container)["env"])[1]
    mounts = {m["destination"]: m for m in nb.fdk.mounts_of(container)}
    holders = []
    for destination, mount in mounts.items():
        if destination == "/multiagents-input":
            continue
        source = Path(mount["source"])
        files = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()] if source.is_dir() else []
        for path in files:
            try:
                if password.encode() in path.read_bytes():
                    holders.append((destination, str(path)))
            except OSError:
                pass
    assert holders == [], f"the credential reached a writable guest location: {holders}"


# ------------------------------------------------------------- the redactor

def entrypoint_of(container) -> str:
    argv = container["CreateArgv"]
    assert "-c" in argv, f"D-S4B.7: cannot find the entrypoint script in the create argv: {argv[:8]}"
    return argv[argv.index("-c") + 1]


class Guest:
    """The host's own invocation input and entrypoint, run on this machine."""

    def __init__(self, ns, tmp_path):
        self.ns = ns
        ns.start()
        self.node = ns.launch()
        container = ns.running(self.node)
        self.script = entrypoint_of(container)
        source = Path({m["destination"]: m for m in nb.fdk.mounts_of(container)}["/multiagents-input"]["source"])
        self.input = tmp_path / "guest-input"
        shutil.copytree(source, self.input)
        self.invocation = json.loads((self.input / "invocation.json").read_text())
        env = self.invocation["env"]
        self.user, self.password = nb.proxy_url(env)
        self.url = env["HTTP_PROXY"]
        self.basic = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        self.prefix = self.invocation["output_prefix"]
        self.cwd = tmp_path / "guest-cwd"
        self.cwd.mkdir()

    def run(self, child: str, expect_code: int = 0):
        invocation = dict(self.invocation, argv=[sys.executable, "-c", textwrap.dedent(child)], cwd=str(self.cwd),
                          stdin=None)
        (self.input / "invocation.json").write_text(json.dumps(invocation))
        script = self.script.replace("/multiagents-input", str(self.input))
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=RUN_TIMEOUT, cwd=self.cwd,
                              env=dict(os.environ, **{UNPRIVILEGED: "1"}))      # K4B.7: explicit test mode
        assert proc.returncode == expect_code, (proc.returncode, proc.stderr[-400:])
        return self.decode(proc.stdout), self.decode(proc.stderr)

    def decode(self, framed: bytes) -> bytes:
        out = b""
        for line in framed.splitlines():
            assert line.startswith(self.prefix.encode()), line[:80]
            out += base64.b64decode(line[len(self.prefix):])
        return out

    def child(self, body: str) -> str:
        return ("import os, sys, time, base64\n"
                "def w(data, stream=sys.stdout, pause=0.12):\n"
                "    if isinstance(data, str): data = data.encode()\n"
                "    stream.buffer.write(data); stream.buffer.flush(); time.sleep(pause)\n"
                "pw = os.environ['HTTP_PROXY'].split('@')[0].split(':')[2]\n"
                "user = os.environ['HTTP_PROXY'].split('@')[0].split('//')[1].split(':')[0]\n"
                "url = os.environ['HTTP_PROXY']\n"
                "enc = base64.b64encode((user + ':' + pw).encode()).decode()\n" + textwrap.dedent(body))


@pytest.fixture
def guest(ns, tmp_path):
    return Guest(ns, tmp_path)


def clean(guest, data: bytes):
    assert guest.password.encode() not in data, "the issued password reached the log stream"
    assert guest.url.encode() not in data and guest.basic.encode() not in data
    assert not nb.SECRET_URL.search(data.decode("latin-1"))


def test_d_s4b_7_a_credential_printed_in_one_write_is_replaced(guest):
    out, _ = guest.run(guest.child("w('before ' + pw + ' after\\n')"))
    clean(guest, out)
    assert out == b"before [REDACTED] after\n"


@pytest.mark.parametrize("cut", [1, 2, 31, 32, 33, 62, 63])
def test_d_s4b_7_a_password_split_across_two_writes_is_still_replaced(guest, cut):
    out, _ = guest.run(guest.child(f"w('A:' + pw[:{cut}]); w(pw[{cut}:] + ':Z\\n')"))
    clean(guest, out)
    assert out == b"A:[REDACTED]:Z\n"


def test_d_s4b_7_a_password_dribbled_one_character_at_a_time_is_replaced(guest):
    out, _ = guest.run(guest.child("w('<'); [w(c, pause=0.06) for c in pw]; w('>\\n')"))
    clean(guest, out)
    assert out == b"<[REDACTED]>\n"


def test_d_s4b_7_the_proxy_url_and_its_basic_encoding_are_replaced_even_when_split(guest):
    out, _ = guest.run(guest.child("""
        w('url=' + url[:20]); w(url[20:] + '\\n')
        w('Proxy-Authorization: Basic ' + enc[:9]); w(enc[9:] + '\\n')
        """))
    clean(guest, out)
    assert out.startswith(b"url=") and b"Proxy-Authorization: Basic " in out and b"[REDACTED]" in out


def test_d_s4b_7_stderr_is_redacted_with_a_separate_tail_per_stream(guest):
    out, err = guest.run(guest.child("""
        w(pw[:30], sys.stdout); w('between\\n', sys.stderr); w(pw[30:] + '\\n', sys.stdout)
        w('err:' + pw + '\\n', sys.stderr)
        """))
    clean(guest, out), clean(guest, err)
    assert out == b"[REDACTED]\n" and err == b"between\nerr:[REDACTED]\n"


def test_d_s4b_7_halves_on_different_streams_are_not_joined_and_not_lost(guest):
    out, err = guest.run(guest.child("w(pw[:32], sys.stdout); w(pw[32:], sys.stderr)"))
    assert out == guest.password[:32].encode() and err == guest.password[32:].encode()


def test_d_s4b_7_non_secret_bytes_keep_order_and_appear_exactly_once(guest):
    out, _ = guest.run(guest.child("""
        for i in range(100): w('line %03d\\n' % i, pause=0)
        w('secret=' + pw + '\\n')
        for i in range(100, 200): w('line %03d\\n' % i, pause=0)
        """))
    clean(guest, out)
    expected = "".join(f"line {i:03d}\n" for i in range(100)) + "secret=[REDACTED]\n" \
        + "".join(f"line {i:03d}\n" for i in range(100, 200))
    assert out.decode() == expected


def test_d_s4b_7_a_held_back_prefix_that_never_completes_is_emitted_at_the_end_unchanged(guest):
    out, _ = guest.run(guest.child("w('tail:' + pw[:40])"))
    assert out == b"tail:" + guest.password[:40].encode()


def test_d_s4b_7_near_misses_of_the_password_are_not_redacted(guest):
    flipped = guest.password[:-1] + ("0" if guest.password[-1] != "0" else "1")
    out, _ = guest.run(guest.child(f"w({flipped!r} + '\\n'); w(pw[::-1] + '\\n')"))
    assert out == (flipped + "\n" + guest.password[::-1] + "\n").encode()


def test_d_s4b_7_binary_non_secret_output_is_preserved_byte_for_byte(guest):
    blob = bytes(range(256)) * 3
    out, _ = guest.run(guest.child(f"w({blob!r}); w(pw); w({blob!r})"))
    assert out == blob + b"[REDACTED]" + blob


def test_d_s4b_7_back_to_back_and_repeated_credentials_are_all_replaced(guest):
    out, _ = guest.run(guest.child("w(pw + pw); w(pw[:10]); w(pw[10:] + pw)"))
    clean(guest, out)
    assert out == b"[REDACTED]" * 4


def test_d_s4b_7_redaction_does_not_change_the_exit_code(guest):
    out, _ = guest.run(guest.child("w(pw); sys.exit(3)"), expect_code=3)
    clean(guest, out)


def test_d_s4b_7_a_child_that_writes_nothing_secret_is_untouched(guest):
    out, err = guest.run(guest.child("w('hello\\n'); w('oops\\n', sys.stderr)"))
    assert out == b"hello\n" and err == b"oops\n"


def test_d_s4b_7_the_entrypoint_does_not_put_the_credential_in_its_own_command_line(guest):
    assert guest.password not in guest.script and guest.password not in " ".join(
        guest.ns.fd.container(guest.ns.running(guest.node)["Id"])["CreateArgv"])


# --------------------------------------------- K4B.7: privilege separation

def test_d_s4b_7_a_non_root_start_without_the_test_variable_fails_closed_and_runs_no_child(guest, monkeypatch):
    """The unprivileged mode is never inferred from a non-root start (K4B.7)."""
    if os.geteuid() == 0:
        pytest.skip("this test needs a non-root test runner")
    marker = guest.cwd / "child-ran"
    child = guest.child(f"open({str(marker)!r}, 'w').write('x'); w('child-output\\n')")
    invocation = dict(guest.invocation, argv=[sys.executable, "-c", child], cwd=str(guest.cwd), stdin=None)
    (guest.input / "invocation.json").write_text(json.dumps(invocation))
    script = guest.script.replace("/multiagents-input", str(guest.input))
    env = {k: v for k, v in os.environ.items() if k != UNPRIVILEGED}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=RUN_TIMEOUT, cwd=guest.cwd, env=env)
    assert proc.returncode != 0, "a non-root start without the explicit test mode must fail closed"
    assert not marker.exists(), "the child was exec'd although the separation could not be established"
    assert b"child-output" not in proc.stdout + proc.stderr
    assert guest.password.encode() not in proc.stdout + proc.stderr


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "2"])
def test_d_s4b_7_only_the_exact_value_one_selects_the_unprivileged_mode(guest, value):
    if os.geteuid() == 0:
        pytest.skip("this test needs a non-root test runner")
    marker = guest.cwd / "child-ran"
    invocation = dict(guest.invocation, argv=[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"],
                      cwd=str(guest.cwd), stdin=None)
    (guest.input / "invocation.json").write_text(json.dumps(invocation))
    script = guest.script.replace("/multiagents-input", str(guest.input))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=RUN_TIMEOUT, cwd=guest.cwd,
                          env=dict(os.environ, **{UNPRIVILEGED: value}))
    assert proc.returncode != 0 and not marker.exists(), value


# the privileged argv, as the host passes it to `docker create` (fake CLI)

def test_d_s4b_7_the_isolated_attempt_with_gateway_credentials_starts_as_root_with_exactly_the_separation_caps(ns):
    ns.start()
    container = ns.running(ns.launch())
    host = container["HostConfig"]
    assert host["CapDrop"] == ["ALL"], host["CapDrop"]
    assert sorted(c.upper().removeprefix("CAP_") for c in host["CapAdd"] or []) in (
        ["SETGID", "SETUID"], ["SETGID", "SETPCAP", "SETUID"]), host["CapAdd"]
    assert any(o.replace(":", "=").split("=")[0] == "no-new-privileges"
               and o.replace(":", "=").split("=")[-1] != "false" for o in host["SecurityOpt"] or []), host["SecurityOpt"]
    assert host["Privileged"] is False
    assert container["Config"]["User"] in ("0", "0:0", "root", "root:root"), \
        f"the entrypoint must start as uid 0 explicitly: {container['Config']['User']!r}"


def test_d_s4b_7_a_host_uid_of_zero_is_refused_with_isolation_unavailable(tmp_path, monkeypatch):
    # The scheduler is a separate process: its interpreter loads support/uid_site, which makes the host
    # UID read by the executor code report 0 while the flag file exists.
    flag = tmp_path / "host-uid-is-zero"
    monkeypatch.setenv("MA_TEST_HOST_UID_FLAG", str(flag))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(Path(__file__).resolve().parent / "support" / "uid_site"), os.environ.get("PYTHONPATH", "")]))
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        s.start()
        s.running(s.launch())                      # positive control: a non-root host is served
        created = len(s.fd.calls_of("create"))
        flag.write_text("")
        node = s.launch()
        s.wait(lambda: "isolation_unavailable" in ic_text(s, node) or None, nb.WAIT, "the refusal")
        assert len(s.fd.calls_of("create")) == created, "no container may be created for a root host"
        assert not any(r.get("container") for r in s.runs(node))
    finally:
        s.close()


def ic_text(s, node):
    import ic_support as ic
    return ic.text_of(s.blocked(node)) + ic.text_of(ic.causes(s.get(node)))


@pytest.mark.parametrize("where", ["host", "provider"])
def test_d_s4b_7_the_test_variable_is_stripped_from_every_run_environment(tmp_path, monkeypatch, where):
    s = nb.NetSched(tmp_path, monkeypatch)
    try:
        if where == "host":
            monkeypatch.setenv(UNPRIVILEGED, "1")
        else:
            s.fake.entry["env"] = {UNPRIVILEGED: "1"}
        s.start()
        container = s.running(s.launch())
        assert UNPRIVILEGED not in s.invocation(container)["env"]
        # environment VALUES only: trusted code (CreateArgv, the entrypoint source) may name the variable
        container_env = container["Config"].get("Env") or []
        assert not any(e.partition("=")[0] == UNPRIVILEGED for e in container_env), \
            "the test-mode switch reached the container environment"
        assert UNPRIVILEGED not in s.invocation(container)["env"]
    finally:
        s.close()


def test_k4b_2_the_gateway_probe_variable_is_stripped_from_every_run_environment(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch)       # sets MULTIAGENTS_TEST_GATEWAY_PROBE on the host
    try:
        s.start()
        container = s.running(s.launch())
        env = container["Config"].get("Env") or []
        assert not any(e.partition("=")[0] == nb.PROBE_ENV for e in env)
        assert nb.PROBE_ENV not in s.invocation(container)["env"]
    finally:
        s.close()


def test_k4b_2_without_the_probe_the_real_check_runs_and_nothing_starts(tmp_path, monkeypatch):
    s = nb.NetSched(tmp_path, monkeypatch, probe=False)
    try:
        s.start()
        node = s.launch()
        deadline = time.monotonic() + nb.WAIT
        while time.monotonic() < deadline and s.get(node)["state"] not in ("held", "failed", "done", "cancelled"):
            time.sleep(0.05)
        # fake Docker gives no HTTP listener: readiness fails, so the attempt's workload never starts
        owners = {r["attempt_id"] for r in s.runs(node)}
        started = [c for c in s.fd.containers().values()
                   if c["Config"]["Labels"].get("multiagents.owner") in owners
                   and (c["State"]["Running"] or c.get("StartSnapshots"))]
        assert not started, "an attempt started although the real readiness check cannot succeed under fake Docker"
    finally:
        s.close()
