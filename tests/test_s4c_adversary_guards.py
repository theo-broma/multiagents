"""Adversary, slice 4C: guards for lines that survived mutation of the 4C suite.

Each test here passes on the current tree and fails under the mutation named in
its docstring; before it existed, that mutation left every tests/test_s4c_*.py
green (fake-CLI run, without SG_TEST_DOCKER). All values are fictional and every
vault is synthetic.
"""
import base64
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4b as nb  # noqa: E402
import ic_s4c as c4  # noqa: E402
import ic_s4c_refresh as rf  # noqa: E402
import ic_support as ic  # noqa: E402
from multiagents.isolated_credentials import CredentialStore, Projection  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.scheduler import claude_credentials as cc  # noqa: E402

KEY_1 = "FICTION-ADV-GUARD-KEY-1-77e0"
KEY_2 = "FICTION-ADV-GUARD-KEY-2-19bd"
ENDED = ("held", "failed", "done", "cancelled")


# ============================================================ K4C.3 (runner: `available` check removed)

@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []

    def build(**kw):
        s = c4.CredSched(tmp_path, monkeypatch, **kw)
        made.append(s)
        return s
    yield build
    for s in made:
        s.close()


def _held_for_missing_credential(s, node):
    view = s.wait(lambda: (v := s.get(node)) and v["state"] in ENDED and v, nb.WAIT, "the node to end")
    assert view["state"] == "held", f"K4C.3: a missing vault credential must hold the node, got {view['state']}"
    assert "credential_missing" in ic.text_of(view), ic.text_of(view)[:800]
    assert s.fd.containers() == {}, "K4C.3: launched without credentials"
    assert s.fake.spawns() == 0, "K4C.3: fell back to the host CLI"
    time.sleep(1.0)
    assert len(s.runs(node)) == 1, "K4C.3: retried silently"


@pytest.mark.parametrize("mode", ["projection", "gateway"])
def test_adv_s4c_guard_an_empty_vault_holds_credential_missing_before_launch(make, mode):
    """Kills: runner.py `executor.credentials.available(provider.name)` removed."""
    s = make(mode=mode, vault=False)
    s.start()
    _held_for_missing_credential(s, s.launch())


def test_adv_s4c_guard_a_vault_without_the_pinned_account_holds_credential_missing(make):
    """Kills the same mutation; also K4C.9: another account in the vault is never a substitute."""
    s = make(mode="gateway", vault=False, account="work")
    c4.write_claude_vault(s.vault, labels=("default", "spare"))
    s.start()
    _held_for_missing_credential(s, s.launch())


# ============================================================ K4C.6 (`due`: margin removed)

@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    return ProjectPaths(project)


def test_adv_s4c_guard_a_projection_inside_the_refresh_margin_is_renewed(paths, tmp_path, monkeypatch):
    """Kills: isolated_credentials.due without `+ REFRESH_MARGIN`."""
    monkeypatch.setenv(rf.LOG_ENV, str(tmp_path / "refresh.jsonl"))
    monkeypatch.setenv("MULTIAGENTS_TEST_CREDENTIAL_REFRESH", rf.SPEC)
    providers = load_providers({"oc": {"bin": "oc", "isolated_credentials": "projection",
                                       "isolated_projection_path": ".local/share/oc"}})
    store = CredentialStore(paths, providers)
    source = store.source("oc")
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"oc": {"type": "oauth", "access": "FICTION-ADV-SOON", "refresh": "FICTION-ADV-R",
                                         "expires": int((time.time() + 600) * 1000)}}))
    data, _ = store.projection("oc")
    assert data["oc"]["access"].startswith("FICTION-RENEWED-ACCESS-"), "a token expiring in 10 min was not renewed"


def test_adv_s4c_guard_the_gateway_refreshes_inside_the_margin(tmp_path):
    """Kills the same mutation on the Claude adapter's path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "FICTION-ADV-SOON", "refreshToken": "FICTION-ADV-R",
        "expiresAt": int((time.time() + 600) * 1000)}}))
    adapter = cc.ClaudeAdapter(vault, pins={"claude": "default"}, upstream=lambda *a: None,
                               refresh=lambda t: {"accessToken": "FICTION-ADV-NEW", "refreshToken": "FICTION-ADV-R2",
                                                  "expiresAt": rf.FAR_FUTURE_MS})
    assert adapter._token("claude") == "FICTION-ADV-NEW"


# ============================================================ K4C.2 and revocation (Projection)

class _Store:
    def __init__(self, providers):
        self.providers = providers
        self.key = KEY_1

    def projection(self, name):
        return {"k": {"type": "api", "key": self.key}}, [self.key]


@pytest.fixture
def projection(tmp_path):
    providers = load_providers({"oc": {"bin": "oc", "isolated_credentials": "projection",
                                       "isolated_projection_path": ".local/share/oc"}})
    home, inputs = tmp_path / "home", tmp_path / "input"
    home.mkdir()
    inputs.mkdir()
    store = _Store(providers)
    p = Projection(store, "oc", home, inputs / "redaction-secrets.jsonl", interval=3600)
    p.start()
    yield store, p, home / ".local/share/oc/auth.json", inputs
    p.revoke()


def test_adv_s4c_guard_a_renewed_secret_is_in_the_redactor_file_before_it_is_projected(projection, monkeypatch):
    """Kills: Projection._publish not appending to redaction-secrets.jsonl (K4C.2)."""
    store, p, auth, inputs = projection
    seen = []
    real = os.rename

    def rename(src, dst, *a, **k):
        if dst == "auth.json":
            seen.append((inputs / "redaction-secrets.jsonl").read_text())
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", rename)
    store.key = KEY_2
    p._publish(*store.projection("oc"))
    assert json.loads(auth.read_text())["k"]["key"] == KEY_2
    assert seen and json.dumps(KEY_2) in seen[-1], "the renewed key was projected before the redactor could know it"


def test_adv_s4c_guard_a_revocation_by_another_executor_stops_this_workers_publication(projection):
    """Kills: Projection._publish ignoring the `credential-revoked` marker. Revocation after a scheduler
    restart, or from the runner's predecessor executor, reaches only the marker, never this object."""
    store, p, auth, inputs = projection
    (inputs / "credential-revoked").touch()
    auth.unlink()
    store.key = KEY_2
    p._publish(*store.projection("oc"))
    assert not auth.exists(), "a revoked attempt's projection was published again by its renewal worker"


def test_adv_s4c_guard_revocation_after_the_resource_is_gone_still_deletes_the_projection(projection):
    """Kills: Projection.revoke skipping the unlink when the input directory has been removed."""
    store, p, auth, inputs = projection
    shutil.rmtree(inputs)
    p.revoke()
    assert not auth.exists()


# ============================================================ the host bridge's authority (IC-C33)

class _Fake:
    status, headers = 200, {}

    def read(self, n):
        return b""

    def close(self):
        pass


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cc.ClaudeAdapter, "_upstream", staticmethod(lambda *a: calls.append(a) or _Fake()))
    base = tmp_path / "b"
    (base / "s").mkdir(parents=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "FICTION-ADV-BRIDGE-ACCESS", "refreshToken": "FICTION-ADV-BRIDGE-REFRESH",
        "expiresAt": rf.FAR_FUTURE_MS}}))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"grants": {"g1": {"provider": "claude", "run_id": "r-granted"}}}))
    config = tmp_path / "host-adapter.json"
    sock = base / "s" / "adapter.sock"
    route = {"vault": str(vault), "account": "default", "binary": "/nonexistent"}
    config.write_text(json.dumps({"routes": {"claude": route}, "policy": str(policy),
                                  "socket": str(sock), "policy_digest": "d"}))
    thread = threading.Thread(target=cc.serve_bridge, args=(config,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not cc._health(sock, "d"):
        assert time.monotonic() < deadline, "bridge never became healthy"
        time.sleep(0.05)

    def send(run_id):
        conn = cc._UnixConnection(sock, timeout=10)
        try:
            route = base64.b64encode(json.dumps({"provider": "claude", "run_id": run_id}).encode()).decode()
            conn.request("POST", "/v1/messages", body=b"{}", headers={"X-Multiagents-Route": route})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()
    yield send, calls, config, vault, policy, route
    conn = cc._UnixConnection(sock)
    try:
        conn.request("POST", "/shutdown", body=b"")
        conn.getresponse().read()
    except OSError:
        pass
    finally:
        conn.close()
    thread.join(5)


def test_adv_s4c_guard_the_bridge_refuses_a_run_without_a_host_grant(bridge):
    """Kills: serve_bridge's `if not allowed(): return self.reply(403)` removed."""
    send, calls, *_ = bridge
    assert send("r-not-granted") == 403
    assert calls == [], "the vault token went upstream for a run the host never granted"


def test_adv_s4c_guard_the_bridge_rechecks_the_grant_after_a_slow_refresh(bridge, tmp_path):
    """Kills: the post-refresh `not allowed()` recheck in serve_bridge's transport removed. The vendor
    refresh (a fake `claude`) renews the scratch profile and, meanwhile, the host revokes the grant."""
    send, calls, config, vault, policy, route = bridge
    data = json.loads((vault / ".credentials.json").read_text())
    data["claudeAiOauth"]["expiresAt"] = 1
    (vault / ".credentials.json").write_text(json.dumps(data))
    fake = tmp_path / "fake-claude"
    fake.write_text(f"""#!{sys.executable}
import json, os, pathlib
p = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
d = json.loads(p.read_text())
d["claudeAiOauth"].update(accessToken="FICTION-ADV-BRIDGE-NEW", expiresAt={rf.FAR_FUTURE_MS})
p.write_text(json.dumps(d))
pathlib.Path({str(policy)!r}).write_text(json.dumps({{"grants": {{}}}}))
""")
    fake.chmod(0o700)
    whole = json.loads(config.read_text())
    whole["routes"]["claude"] = dict(route, binary=str(fake))
    config.write_text(json.dumps(whole))
    status = send("r-granted")
    assert json.loads((vault / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == \
        "FICTION-ADV-BRIDGE-NEW", "precondition: the host-side refresh ran"
    assert calls == [], "a request whose grant was revoked during the refresh still went upstream"
    assert status != 200


# ============================================================ K4C.5 (modes)

def test_adv_s4c_guard_projection_modes_hold_over_a_preexisting_loose_directory(tmp_path):
    """Kills: Projection.__init__ without `os.fchmod(self.directory, 0o700)`. A conversation HOME (or a
    seed) may already hold the provider's directory at 0755 and a 0644 auth.json."""
    import stat
    providers = load_providers({"oc": {"bin": "oc", "isolated_credentials": "projection",
                                       "isolated_projection_path": ".local/share/oc"}})
    home, inputs = tmp_path / "home", tmp_path / "input"
    (home / ".local/share/oc").mkdir(parents=True)
    os.chmod(home / ".local/share/oc", 0o755)
    (home / ".local/share/oc/auth.json").write_text("{}")
    os.chmod(home / ".local/share/oc/auth.json", 0o644)
    inputs.mkdir()
    p = Projection(_Store(providers), "oc", home, inputs / "redaction-secrets.jsonl", interval=3600)
    try:
        p.start()
        directory = (home / ".local/share/oc").stat()
        auth = (home / ".local/share/oc/auth.json").stat()
        secrets = (inputs / "redaction-secrets.jsonl").stat()
        assert stat.S_IMODE(directory.st_mode) == 0o700
        assert stat.S_IMODE(auth.st_mode) == 0o600
        assert stat.S_IMODE(secrets.st_mode) == 0o600
        assert directory.st_uid == auth.st_uid == os.getuid()
    finally:
        p.revoke()
