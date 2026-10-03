"""C19 adversarial tests: the quota details page and the identity reveal.

Contract: context/specs/c19-quota-details-page.md (QD-R1..R7, QD-R1a/R3a/R4a/R7).
Each test attacks a path that the C19 suite leaves unchecked. A test that fails
shows a defect. A test that passes records an attack that held, so a later
change cannot quietly reopen it.

Fixtures are reused from the C19 black-box suite. Every HOME is temporary,
nothing reads a real credential and nothing starts docker.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import urllib.parse
from types import SimpleNamespace

import pytest

from multiagents.monitor import quota, snapshot
from test_c19_quota_details_page import (  # noqa: F401 - `make` is a fixture
    REFRESH_SECRET, TOKEN_SECRET, Actions, b64, claude_profile, codex,
    codex_profile, docker_claude, jwt, make, reading, run_script, uniq, win)

pytestmark = pytest.mark.real_providers


# --------------------------------------------------------------------------
# helpers


def raw_request(fx, method, path, *, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return SimpleNamespace(status=resp.status, text=data.decode("utf-8", "replace"),
                               headers={k.lower(): v for k, v in resp.getheaders()})
    finally:
        conn.close()


def assert_unknown(done, *forbidden):
    assert done.returncode == 64, (done.returncode, done.stdout, done.stderr)
    assert done.stdout.strip() == ""
    for value in forbidden:
        assert value not in done.stdout and value not in done.stderr, value


# --------------------------------------------------------------------------
# identity actions: symlinks out of the profile


def test_adv_claude_local_profile_symlinked_outside_home_is_not_followed(tmp_path):
    """The action must read the selected profile, not wherever a link points."""
    outside = tmp_path / "elsewhere"
    claude_profile(outside, "outside@example.invalid")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").symlink_to(outside / ".claude.json")
    done = run_script("claude.sh", "identity",
                      {"HOME": str(home), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert_unknown(done, "outside@example.invalid")


@pytest.mark.parametrize("how", ["file-link", "dir-link"])
def test_adv_claude_b_vault_profile_linked_to_the_host_default_is_never_shown(tmp_path, how):
    """QD-R4a: claude-b shows vault account b, "never the host default". A link
    in the vault that resolves to the host profile must not leak the host
    identity under b's name."""
    env = docker_claude(tmp_path, "b", vault_b=False)
    host = tmp_path / "host-home"
    accounts = tmp_path / "vault" / "accounts"
    accounts.mkdir(parents=True)
    if how == "file-link":
        (accounts / "b").mkdir()
        (accounts / "b" / ".claude.json").symlink_to(host / ".claude.json")
    else:
        (accounts / "b").symlink_to(host, target_is_directory=True)
    done = run_script("claude.sh", "identity", env, tmp_path)
    assert_unknown(done, "host-default@example.invalid")


def test_adv_codex_auth_file_symlinked_outside_the_profile_is_not_followed(tmp_path):
    outside = tmp_path / "elsewhere"
    codex_profile(outside, {"email": "outside@example.invalid"})
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "auth.json").symlink_to(outside / "auth.json")
    assert_unknown(codex(tmp_path, prof), "outside@example.invalid")


# --------------------------------------------------------------------------
# identity actions: credential material inside the claim


@pytest.mark.parametrize("field", ["refresh_token", "access_token"])
def test_adv_codex_never_prints_a_token_from_its_own_auth_file(tmp_path, field):
    """QD-R4/R4a: a fixture token is never in the action's output. The guard is
    a list of known token prefixes, so a token outside that list passes through
    when the claim embeds it."""
    secret = "C19RAWTOKEN0123456789abcdefXYZ"
    prof = tmp_path / "prof"
    codex_profile(prof, {"email": f"{secret}@example.test"})
    doc = json.loads((prof / "auth.json").read_text())
    doc["tokens"][field] = secret
    (prof / "auth.json").write_text(json.dumps(doc))
    done = codex(tmp_path, prof)
    assert secret not in done.stdout and secret not in done.stderr


def test_adv_claude_never_prints_a_token_from_its_own_credentials(tmp_path):
    """The same property for claude: a sidecar name-tag (`mxa2_…`) is the
    credential a container profile carries, and it matches no blocked prefix."""
    secret = "mxa2_C19NAMETAGSECRET0123456789"
    home = tmp_path / "home"
    claude_profile(home, f"{secret}@example.test")
    (home / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": secret, "refreshToken": REFRESH_SECRET}}))
    done = run_script("claude.sh", "identity",
                      {"HOME": str(home), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert secret not in done.stdout and secret not in done.stderr


@pytest.mark.parametrize("payload", [
    b"{" + b'"pad":"' + b"A" * 3_000_000 + b'","email":"big@example.invalid"}',
    b"\xff\xfe not json",
    json.dumps({"email": {"value": "nested@example.invalid", "token": TOKEN_SECRET}}).encode(),
    json.dumps({"email": ["list@example.invalid", TOKEN_SECRET]}).encode(),
    json.dumps([{"email": "array@example.invalid"}]).encode(),
    json.dumps({"email": "crlf@example.invalid\r\nX-Injected: 1"}).encode(),
    json.dumps({"email": f"{TOKEN_SECRET}@example.test"}).encode(),
], ids=["huge", "binary", "nested-object", "nested-array", "array-root", "crlf",
        "token-in-email"])
def test_adv_codex_hostile_id_token_payloads_leak_nothing(tmp_path, payload):
    prof = tmp_path / "prof"
    token = f"{b64({'alg': 'none'})}.{b64(payload)}.sig"
    codex_profile(prof, {}, id_token=token)
    done = codex(tmp_path, prof)
    out = done.stdout + done.stderr
    for value in (TOKEN_SECRET, REFRESH_SECRET, "X-Injected", "AAAAAAAA"):
        assert value not in out
    if done.returncode == 0:
        data = json.loads(done.stdout)
        assert set(data) == {"identity", "kind"} and isinstance(data["identity"], str)
    else:
        assert done.returncode == 64 and done.stdout.strip() == ""


@pytest.mark.parametrize("id_token", [
    "not-a-jwt", "a.b", "a.!!!!.c", "a..c", f"{b64({})}.{'A' * 5}.c",
    jwt({"email": "x@example.invalid"}) + ".extra",
])
def test_adv_codex_malformed_jwt_shapes_are_unknown(tmp_path, id_token):
    prof = tmp_path / "prof"
    codex_profile(prof, {}, id_token=id_token)
    assert_unknown(codex(tmp_path, prof), TOKEN_SECRET)


def test_adv_claude_extra_secret_fields_in_oauth_account_stay_inside(tmp_path):
    home = tmp_path / "home"
    claude_profile(home, "dana@example.invalid")
    doc = json.loads((home / ".claude.json").read_text())
    doc["oauthAccount"].update({"accessToken": TOKEN_SECRET, "apiKey": "sk-ant-<redacted>",
                                "sessionKey": "C19SESSIONKEY"})
    (home / ".claude.json").write_text(json.dumps(doc))
    done = run_script("claude.sh", "identity",
                      {"HOME": str(home), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert done.returncode == 0
    assert json.loads(done.stdout) == {"identity": "dana@example.invalid", "kind": "email"}
    for value in (TOKEN_SECRET, "C19KEY", "C19SESSIONKEY", "uuid-1", "Org"):
        assert value not in done.stdout + done.stderr


@pytest.mark.parametrize("email", ["a@b\r\nSet-Cookie: x=1", "a@b\x00c", "a b@c",
                                   "@", "a@b@c", "x" * 300 + "@example.test"])
def test_adv_claude_malformed_email_values_are_unknown(tmp_path, email):
    home = tmp_path / "home"
    claude_profile(home, email)
    done = run_script("claude.sh", "identity",
                      {"HOME": str(home), "MULTIAGENTS_EXECUTOR": "local"}, tmp_path)
    assert_unknown(done, "Set-Cookie")


@pytest.mark.parametrize("pin", ["../b", "/etc", "b/../../host-home", "B", "b\n"])
def test_adv_claude_docker_rejects_a_pin_that_is_a_path(tmp_path, pin):
    env = docker_claude(tmp_path, pin)
    done = run_script("claude.sh", "identity", env, tmp_path)
    assert_unknown(done, "host-default@example.invalid", "placeholder@example.invalid")


# --------------------------------------------------------------------------
# the reveal request


@pytest.mark.parametrize("account", ["../a", "/etc/passwd", "a\x00", "ａ", "A", "a ", ""])
def test_adv_reveal_refuses_path_like_or_lookalike_accounts(make, account):
    name = uniq("claude")
    acts = Actions(labels={"a"})
    acts.identities[(name, "a")] = "alice@example.invalid"
    fx = make({name: reading(name, {"a/week": win(20, "a")})}, acts)
    assert fx.revealed(name, "a") == "alice@example.invalid"
    resp = fx.reveal(name, account)
    assert resp.status == 400, (resp.status, resp.text)
    assert "alice@example.invalid" not in resp.text
    assert all(acct == "a" for _n, acct in acts.calls)


@pytest.mark.parametrize("body", [
    b"[]", b'"provider"', b"null", b"42", b"{}", b'{"account": null}',
    b'{"provider": ["x"], "account": null}', b'{"provider": {"a": 1}}',
    b'{"provider": "x", "account": 5}', b"\xff\xfe", b"{" * 10000,
])
def test_adv_reveal_bad_bodies_are_400_without_echo(make, body):
    fx, name, _acts = _one(make)
    resp = raw_request(fx, "POST", "/api/quota/identity", body=body,
                       headers={"X-Monitor-Token": fx.token,
                                "Content-Type": "application/json"})
    assert resp.status == 400, (resp.status, resp.text)
    assert "Traceback" not in resp.text and "Error(" not in resp.text


def test_adv_reveal_refuses_an_oversized_body(make):
    fx, name, _acts = _one(make)
    body = json.dumps({"provider": name, "account": None, "pad": "x" * 100_000}).encode()
    resp = raw_request(fx, "POST", "/api/quota/identity", body=body,
                       headers={"X-Monitor-Token": fx.token})
    assert resp.status == 400


@pytest.mark.parametrize("length", ["-1", "abc", "1e3", "99999999999"])
def test_adv_reveal_bad_content_length_is_400(make, length):
    fx, name, _acts = _one(make)
    conn = http.client.HTTPConnection("127.0.0.1", fx.port, timeout=10)
    try:
        conn.putrequest("POST", "/api/quota/identity")
        conn.putheader("X-Monitor-Token", fx.token)
        conn.putheader("Content-Length", length)
        conn.endheaders()
        assert conn.getresponse().status == 400
    finally:
        conn.close()


def test_adv_reveal_of_a_disabled_or_unknown_provider_is_refused(make, monkeypatch):
    live, off = uniq("live"), uniq("off")
    acts = Actions()
    acts.identities = {(live, None): "live@example.invalid", (off, None): "off@example.invalid"}
    fx = make({n: reading(n, {"week": win(20)}) for n in (live, off)}, acts)
    providers = {n: SimpleNamespace(name=n, billing="plan", available=lambda: "/fake",
                                    container_account="", enabled=(n == live))
                 for n in (live, off)}
    monkeypatch.setattr(snapshot, "load_providers", lambda _: providers)
    assert fx.revealed(live, None) == "live@example.invalid"
    for name in (off, uniq("ghost")):
        resp = fx.reveal(name, None)
        assert resp.status == 400, (name, resp.status, resp.text)
        assert "off@example.invalid" not in resp.text
    assert (off, None) not in acts.calls
    assert off not in fx.rows()


def test_adv_a_cached_identity_is_not_served_after_the_provider_is_disabled(make, monkeypatch):
    name = uniq("stale")
    acts = Actions()
    acts.identities[(name, None)] = "stale@example.invalid"
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    provider = SimpleNamespace(name=name, billing="plan", available=lambda: "/fake",
                               container_account="", enabled=True)
    monkeypatch.setattr(snapshot, "load_providers", lambda _: {name: provider})
    assert fx.revealed(name, None) == "stale@example.invalid"
    provider.enabled = False
    resp = fx.reveal(name, None)
    assert resp.status == 400 and "stale@example.invalid" not in resp.text
    assert "stale@example.invalid" not in fx.get("/api/quota").text


def test_adv_a_cached_identity_does_not_follow_a_changed_pin(make, monkeypatch):
    """A pin moved from a to c: the old account is refused, the new one fetched."""
    name = uniq("claude-b")
    acts = Actions(labels={"a", "c"})
    acts.identities = {(name, "a"): "alice@example.invalid", (name, "c"): "carol@example.invalid"}
    fx = make({name: reading(name, {"week": win(20)})}, acts, pins={name: "a"})
    assert fx.revealed(name, "a") == "alice@example.invalid"
    provider = SimpleNamespace(name=name, billing="plan", available=lambda: "/fake",
                               container_account="c")
    monkeypatch.setattr(snapshot, "load_providers", lambda _: {name: provider})
    assert fx.reveal(name, "a").status == 400
    assert fx.revealed(name, "c") == "carol@example.invalid"


# --------------------------------------------------------------------------
# authentication, origin and host


def _one(make):
    name = uniq("codex")
    acts = Actions()
    acts.identities[(name, None)] = "solo@example.invalid"
    fx = make({name: reading(name, {"weekly": win(30)})}, acts)
    return fx, name, acts


@pytest.mark.parametrize("how", ["cookie", "query", "query-good-header-bad",
                                 "header-empty", "header-prefix", "header-padded",
                                 "bearer"])
def test_adv_the_token_is_accepted_only_in_its_own_header(make, how):
    fx, name, _acts = _one(make)
    body = json.dumps({"provider": name, "account": None})
    path = "/api/quota/identity"
    headers = {"Content-Type": "application/json"}
    if how == "cookie":
        headers["Cookie"] = f"token={fx.token}; X-Monitor-Token={fx.token}"
    elif how == "query":
        path += "?" + urllib.parse.urlencode({"token": fx.token})
    elif how == "query-good-header-bad":
        path += "?" + urllib.parse.urlencode({"token": fx.token})
        headers["X-Monitor-Token"] = "wrong"
    elif how == "header-empty":
        headers["X-Monitor-Token"] = ""
    elif how == "header-prefix":
        headers["X-Monitor-Token"] = fx.token[:-1]
    elif how == "header-padded":
        headers["X-Monitor-Token"] = fx.token + "x"
    elif how == "bearer":
        headers["Authorization"] = f"Bearer {fx.token}"
    for method, route, data in (("POST", path, body),
                                ("GET", path.replace("/identity", ""), b"")):
        resp = raw_request(fx, method, route, body=data, headers=headers)
        assert resp.status == 403, (how, route, resp.status, resp.text)
        assert "solo@example.invalid" not in resp.text


@pytest.mark.parametrize("origin", ["null", "http://evil.example", "https://127.0.0.1:{port}",
                                    "http://127.0.0.1:{other}", "http://127.0.0.1.evil:{port}",
                                    "http://user@example.invalid:{port}", "file://"])
def test_adv_odd_origins_are_refused_on_the_reveal_and_the_handoff(make, origin):
    fx, name, _acts = _one(make)
    origin = origin.format(port=fx.port, other=fx.port + 1)
    resp = fx.reveal(name, None, origin=origin)
    assert resp.status == 403, (origin, resp.status)
    assert "solo@example.invalid" not in resp.text
    page = fx.open_details(origin=origin)
    assert page.status == 403 and fx.token not in page.text


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example",
                                  "evil.example:{port}", "localhost.evil"])
def test_adv_a_foreign_host_is_refused_everywhere(make, host):
    fx, name, _acts = _one(make)
    host = host.format(port=fx.port)
    assert fx.reveal(name, None, host=host).status == 403
    assert fx.get("/api/quota", host=host).status == 403
    page = fx.open_details(host=host)
    assert page.status == 403 and fx.token not in page.text


def test_adv_the_handoff_never_redirects_and_never_puts_the_token_in_a_url(make):
    fx, _name, _acts = _one(make)
    cases = [
        fx.request("POST", "/quota?" + urllib.parse.urlencode({"token": fx.token}),
                   auth=False),
        fx.request("POST", "/quota", body="token=wrong", auth=False,
                   headers={"Content-Type": "application/x-www-form-urlencoded"}),
        fx.request("POST", "/quota", body=json.dumps({"token": fx.token}), auth=False,
                   headers={"Content-Type": "application/json"}),
        fx.request("POST", "/quota", body="", auth=False),
        fx.request("GET", "/quota?" + urllib.parse.urlencode({"token": fx.token}),
                   auth=False),
        fx.open_details(),
    ]
    *refused, get_page, good = cases
    for resp in refused:
        assert resp.status in (400, 403), resp.status
        assert fx.token not in resp.text
    assert fx.token not in get_page.text
    for resp in cases:
        assert 300 > resp.status or resp.status >= 400
        assert "location" not in resp.headers
        assert resp.headers.get("referrer-policy") == "no-referrer"
        assert resp.headers.get("cache-control") == "no-store"
    assert good.status == 200


def test_adv_the_handoff_does_not_reflect_its_input(make):
    fx, _name, _acts = _one(make)
    marker = "<script>alert('c19')</script>"
    for body in (urllib.parse.urlencode({"token": marker}),
                 urllib.parse.urlencode({"token": fx.token, "x": marker}),
                 urllib.parse.urlencode({"token": fx.token}) + "&" + marker):
        resp = fx.open_details(body=body)
        assert marker not in resp.text
    resp = fx.request("GET", "/quota?x=" + urllib.parse.quote(marker), auth=False)
    assert marker not in resp.text


def test_adv_the_handoff_takes_the_first_token_only_when_it_is_right(make):
    fx, _name, _acts = _one(make)
    wrong_first = fx.open_details(body=f"token=wrong&token={fx.token}")
    assert wrong_first.status == 403 and fx.token not in wrong_first.text


# --------------------------------------------------------------------------
# concurrency


def test_adv_many_reveals_and_polls_run_each_account_once_and_stay_bounded(make):
    n = 6
    name = uniq("claude")
    labels = {f"p{i}" for i in range(n)}
    acts = Actions(labels=labels)
    acts.delay = 0.3
    acts.identities = {(name, a): f"{a}@example.test" for a in labels}
    fx = make({name: reading(name, {f"{a}/week": win(10, a) for a in labels})}, acts)
    before = threading.active_count()
    peak = [before]
    results, errors = [], []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            peak[0] = max(peak[0], threading.active_count())
            time.sleep(0.01)

    def retried(call):
        # http.server listens with a backlog of 5, so a burst of connections
        # can be reset before accept on any route (/api/action too). That is
        # the monitor's, not C19's; a reset connection is simply retried.
        for _ in range(20):
            try:
                return call()
            except ConnectionResetError:
                time.sleep(0.05)
        return call()

    def go(account):
        try:
            results.append((account, retried(lambda: fx.revealed(name, account))))
        except Exception as exc:          # noqa: BLE001 - reported below
            errors.append(repr(exc))

    def poll():
        try:
            retried(fx.rows)
        except Exception as exc:          # noqa: BLE001
            errors.append(repr(exc))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    workers = [threading.Thread(target=go, args=(a,)) for a in sorted(labels) for _ in range(5)]
    workers += [threading.Thread(target=poll) for _ in range(10)]
    started = time.monotonic()
    for t in workers:
        t.start()
    for t in workers:
        t.join(30)
    stop.set()
    watcher.join(2)
    assert not any(t.is_alive() for t in workers), "deadlock: workers still running"
    assert not errors, errors
    assert time.monotonic() - started < 25
    assert sorted(results) == sorted((a, f"{a}@example.test") for a in labels for _ in range(5))
    for a in labels:
        assert acts.calls.count((name, a)) == 1, (a, acts.calls)
    # client threads + server request threads + at most 4 identity workers
    assert peak[0] - before <= len(workers) * 2 + 4 + 2


def test_adv_a_busy_background_pool_does_not_turn_a_known_identity_into_unknown(make):
    """The reveal shares C18's four background slots with the usage scripts.
    While four usage refreshes are in flight (a slow network, a hung CLI), a
    reveal of an identity that exists answers "unknown" — the same answer as
    an account that has none."""
    fx, name, acts = _one(make)
    slots = snapshot._BACKGROUND_SLOTS
    held = 0
    try:
        while slots.acquire(blocking=False):
            held += 1
        assert held == 4
        result = {}

        def reveal():
            result["value"] = fx.revealed(name, None)

        t = threading.Thread(target=reveal, daemon=True)
        t.start()
        time.sleep(1.0)
        # The usage jobs end after the reveal's own wait has run out.
        t.join(25)
        assert not t.is_alive()
    finally:
        for _ in range(held):
            slots.release()
    assert result["value"] == "solo@example.invalid"


def test_adv_reveal_does_not_wait_forever_on_a_hung_identity_action(make):
    name = uniq("hung")
    acts = Actions()
    gate = threading.Event()
    acts.behave[name] = lambda _a: (gate.wait(60), (64, "", ""))[1]
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    try:
        started = time.monotonic()
        resp = fx.reveal(name, None)
        assert resp.status == 200
        assert json.loads(resp.text)["identity"] in (None, "", "unknown")
        assert time.monotonic() - started < 20
        poll_started = time.monotonic()
        fx.rows()
        assert time.monotonic() - poll_started < 5
    finally:
        gate.set()


# --------------------------------------------------------------------------
# no leaks through the state poll


def test_adv_the_poll_never_carries_a_clear_identity_even_after_a_reveal(make):
    fx, name, _acts = _one(make)
    assert fx.revealed(name, None) == "solo@example.invalid"
    for _ in range(3):
        text = fx.get("/api/quota").text
        assert "solo@example.invalid" not in text
        assert "solo" not in text.replace(name, "")


def test_adv_identity_kinds_other_than_email_still_pass_the_secret_guard(make):
    """`kind: account` skips the e-mail shape check; the claim must still not be
    a long opaque token copied from the credential file."""
    name = uniq("acct")
    acts = Actions()
    acts.behave[name] = lambda _a: (0, json.dumps({"identity": REFRESH_SECRET,
                                                   "kind": "org"}), "")
    fx = make({name: reading(name, {"week": win(20)})}, acts)
    assert fx.revealed(name, None) is None


def test_adv_quota_module_writes_nothing_and_holds_identities_only_in_memory(make, tmp_path):
    fx, name, _acts = _one(make)
    assert fx.revealed(name, None) == "solo@example.invalid"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"solo@example.invalid" not in path.read_bytes(), path
    assert any(v[1] == "solo@example.invalid" for v in quota._CACHE.values())
