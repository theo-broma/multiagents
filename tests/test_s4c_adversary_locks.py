"""Adversary, slice 4C (D-S4C.4): refresh races on ONE stored credential.

The renewal lock is keyed on the provider's NAME and the account label
(`isolated_credentials.account_lock(vault, name, account)`), while the thing
being renewed is a FILE in a vault that several provider names can share:

* an `auth_from` group shares its owner's vault (PS-R2), so `oc` and `oc-b`
  renew the same `auth.json` under two different locks;
* under `executor.docker.auth_proxy`, every Claude provider uses the owner's
  vault, so `claude` and an extending `claude-p` with no `container_account`
  both renew `vault/.credentials.json` under two different locks;
* the Docker executor's own renewal (`DockerExecutor._refresh_lock`) of that
  same vault takes a third, unrelated lock.

Two renewals racing on one refresh token is what the existing docker.py comment
calls "how a provider decides it has been stolen". All values are fictional.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_s4c_refresh as rf  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.isolated_credentials import CredentialStore  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.scheduler.claude_credentials import ClaudeAdapter  # noqa: E402

DELAY = 0.5
FAR = rf.FAR_FUTURE_MS


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    return ProjectPaths(project)


def _overlap(events):
    depth = best = 0
    for e in sorted(events, key=lambda e: e["t"]):
        depth += 1 if e["event"] == "enter" else -1
        best = max(best, depth)
    return best


def _both(calls):
    errors = []

    def run(fn):
        try:
            fn()
        except Exception as exc:          # surfaced below, never swallowed
            errors.append(repr(exc))
    threads = [threading.Thread(target=run, args=(fn,)) for fn in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return errors


def test_adv_s4c_an_auth_from_group_renews_its_one_store_once_not_once_per_member(world, tmp_path, monkeypatch):
    log = tmp_path / "refresh.jsonl"
    monkeypatch.setenv(rf.LOG_ENV, str(log))
    monkeypatch.setenv(rf.DELAY_ENV, str(DELAY))
    monkeypatch.setenv("MULTIAGENTS_TEST_CREDENTIAL_REFRESH", rf.SPEC)
    providers = load_providers({
        "oc": {"bin": "oc", "isolated_credentials": "projection", "isolated_projection_path": ".local/share/oc"},
        "oc-b": {"extends": "oc", "auth_from": "oc"},
    })
    store = CredentialStore(world, providers)
    assert store.source("oc") == store.source("oc-b"), "precondition: the group shares one stored credential"
    source = store.source("oc")
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"oc": {"type": "oauth", "access": "FICTION-ADV-ACCESS",
                                         "refresh": "FICTION-ADV-REFRESH", "expires": 1}}))

    errors = _both([lambda: store.projection("oc"), lambda: store.projection("oc-b")])
    events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    entered = [e for e in events if e["event"] == "enter"]
    assert errors == []
    assert _overlap(events) <= 1, "two renewals of one stored refresh token ran at the same time"
    assert len(entered) == 1, f"one expired credential renewed {len(entered)} times by one group"


def test_adv_s4c_two_claude_providers_on_one_vault_account_refresh_it_once(world, tmp_path):
    providers = load_providers({
        "claude": {"bin": "claude", "isolated_credentials": "gateway", "gateway_auth": "anthropic",
                   "container_private_home": [".claude"]},
        "claude-p": {"extends": "claude", "family": "claude-p"},
    })
    store = CredentialStore(world, providers, {"auth_proxy": True})
    # The bridge builds one route per provider from exactly these values (gateways._host_routes).
    assert store.source("claude") == store.source("claude-p"), "precondition: one stored credential"
    vault = store.vault("claude")
    (vault).mkdir(parents=True)
    (vault / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "FICTION-ADV-C-ACCESS", "refreshToken": "FICTION-ADV-C-REFRESH", "expiresAt": 1}}))
    used, lock = [], threading.Lock()
    active = {"n": 0, "max": 0}

    def refresh(token):
        with lock:
            used.append(token)
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(DELAY)
        with lock:
            active["n"] -= 1
        tag = str(len(used))
        return {"accessToken": "FICTION-ADV-C-NEW-" + tag, "refreshToken": "FICTION-ADV-C-ROT-" + tag,
                "expiresAt": FAR}

    adapter = ClaudeAdapter(vault, pins={"claude": "default", "claude-p": "default"}, refresh=refresh,
                            upstream=lambda *a: None)
    errors = _both([lambda: adapter._token("claude"), lambda: adapter._token("claude-p")])
    assert errors == []
    assert active["max"] <= 1, "two refreshes of one account's refresh token overlapped"
    assert used == ["FICTION-ADV-C-REFRESH"], f"one refresh token was spent {len(used)} times: {used}"


def test_adv_s4c_the_gateway_never_refreshes_while_the_docker_executor_renews_the_same_vault(world):
    providers = load_providers({
        "claude": {"bin": "claude", "isolated_credentials": "gateway", "gateway_auth": "anthropic",
                   "container_private_home": [".claude"]},
    })
    store = CredentialStore(world, providers)
    docker = DockerExecutor({}, paths=world, providers=providers)
    assert docker.vault_state("claude")["claude"] == store.vault("claude"), \
        "precondition: shared-mode Docker and the isolated gateway renew one vault"
    vault = store.vault("claude")
    vault.mkdir(parents=True)
    (vault / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "FICTION-ADV-D-ACCESS", "refreshToken": "FICTION-ADV-D-REFRESH", "expiresAt": 1}}))
    seen = {}

    def refresh(token):
        seen["at"] = time.monotonic()
        return {"accessToken": "FICTION-ADV-D-NEW", "refreshToken": "FICTION-ADV-D-ROT", "expiresAt": FAR}

    adapter = ClaudeAdapter(vault, pins={"claude": "default"}, refresh=refresh, upstream=lambda *a: None)
    with docker._refresh_lock("claude") as held:
        assert held, "precondition: the Docker executor's renewal of claude is in progress"
        worker = threading.Thread(target=lambda: adapter._token("claude"))
        worker.start()
        time.sleep(DELAY)
        released = time.monotonic()
    worker.join(30)
    assert "at" in seen, "the gateway never refreshed at all"
    assert seen["at"] >= released, "the gateway refreshed the vault while Docker's renewal of it was in progress"
