"""C6: a pinned provider's auth status reports only its own account.

Contract: context/specs/phase6-closing-fixes.md, C6-R1, C6-R1a, C6-R2, C6-R3.
Harness follows tests/test_dk_vault_integration.py (fake vault, fake claude CLI,
synthetic credentials only).
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import auth, scripts
from multiagents.executor import docker
from multiagents.paths import ProjectPaths
from multiagents.providers import load_providers


def login(path, token, offset=7200, refresh=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    block = {"accessToken": token, "expiresAt": int((time.time() + offset) * 1000)}
    if refresh:
        block["refreshToken"] = "fake-refresh"
    path.write_text(json.dumps({"claudeAiOauth": block}))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    binary = tmp_path / "fake-cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    raw = {
        "claude": {"bin": str(binary), "script": "claude.sh",
                   "container_private_home": [".claude"],
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "second": {"extends": "claude", "container_account": "b",
                   "env": {"CLAUDE_CONFIG_DIR": str(home / "second")}},
        "borrowed": {"auth_from": "claude", "container_account": "b"},
        # no pin of its own; its owner `second` is pinned to b
        "heir": {"auth_from": "second"},
        "heir_ext": {"extends": "second"},
        # explicit pin wins over the owner's pin
        "override": {"auth_from": "second", "container_account": "c"},
    }
    providers = load_providers(raw)
    paths = ProjectPaths(tmp_path / "project")
    paths.root.mkdir()
    paths.config.mkdir(parents=True)
    ex = docker.DockerExecutor({"auth_proxy": True, "mount_cli_from_host": False},
                               paths=paths, providers=providers)
    monkeypatch.setattr(ex, "inside", lambda: False)
    vault = ex.vault_state("claude")["claude"]
    mounted = next(iter(ex.private_state("claude").values()))
    mounted.mkdir(parents=True)
    return SimpleNamespace(ex=ex, vault=vault, paths=paths, providers=providers,
                           raw=raw)


def states(rig):
    return auth.check_all(rig.providers, lambda _: rig.ex, rig.paths.config)


def put(rig, label, token=None, offset=7200, refresh=False):
    target = (rig.vault / ".credentials.json" if label == "default"
              else rig.vault / "accounts" / label / ".credentials.json")
    login(target, token or label, offset, refresh)


def test_c6_r1_pinned_provider_with_expired_unrenewable_pin_is_not_authenticated(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)          # expired, no refresh token: not renewable
    s = states(rig)
    for name in ("second", "borrowed"):
        assert s[name].status == "not_authenticated", s[name]
        assert not s[name].ok
        assert s[name].fix == f"multiagents auth login {name}"


def test_c6_r1_detail_names_only_the_pinned_account(rig):
    put(rig, "default")
    put(rig, "a")
    put(rig, "b", offset=-60)
    s = states(rig)
    for name in ("second", "borrowed"):
        detail = s[name].detail
        assert '"b"' in detail
        assert '"default"' not in detail
        assert '"a"' not in detail


def test_c6_r1_pinned_with_usable_pin_is_authenticated_even_if_pool_is_broken(rig):
    put(rig, "default", offset=-60)
    put(rig, "a", offset=-60)
    put(rig, "b")
    s = states(rig)
    assert s["second"].ok and s["borrowed"].ok
    assert not s["claude"].ok
    assert '"b"' in s["second"].detail and '"default"' not in s["second"].detail


def test_c6_r1_pinned_provider_ignores_unusable_pool_accounts_when_pin_is_ok(rig):
    put(rig, "b")
    s = states(rig)
    assert s["second"].ok and s["borrowed"].ok


def test_c6_r1_direct_check_matches_check_all(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    state = auth.check("second", rig.providers["second"], rig.ex, rig.paths.config)
    assert state.status == "not_authenticated"
    assert '"default"' not in state.detail and '"b"' in state.detail


def test_c6_r1_provider_check_action_reports_pinned_account_only(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    code, out, err = scripts.run_action("second", rig.providers["second"], rig.ex,
                                        "check", rig.paths.config)
    text = out + err
    assert code != 0, text
    assert '"b"' in text and '"default"' not in text


def test_c6_r1_to_dict_carries_pinned_detail_and_fix(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    d = states(rig)["second"].to_dict()
    assert d["authenticated"] is False
    assert '"default"' not in d["detail"] and d["fix"] == "multiagents auth login second"


def test_c6_r1a_missing_pinned_account_is_not_authenticated_and_names_it(rig):
    put(rig, "default")
    s = states(rig)
    for name in ("second", "borrowed"):
        assert s[name].status == "not_authenticated"
        assert '"b"' in s[name].detail
        assert '"default"' not in s[name].detail


def test_c6_r1a_renewable_expired_pin_counts_as_usable(rig):
    put(rig, "b", offset=-60, refresh=True)
    s = states(rig)
    assert s["second"].ok and s["borrowed"].ok


def test_c6_r1a_dependent_of_pinned_owner_inherits_the_pin(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    s = states(rig)
    for name in ("heir", "heir_ext"):
        assert s[name].status == "not_authenticated", (name, s[name])
        assert '"b"' in s[name].detail and '"default"' not in s[name].detail


def test_c6_r1a_inheriting_dependent_is_authenticated_when_owners_pin_is_usable(rig):
    put(rig, "default", offset=-60)
    put(rig, "b")
    s = states(rig)
    assert s["heir"].ok and s["heir_ext"].ok


def test_c6_r1a_explicit_pin_on_dependent_wins_over_owners_pin(rig):
    put(rig, "default")
    put(rig, "c")
    put(rig, "b", offset=-60)
    s = states(rig)
    assert s["override"].ok
    assert '"c"' in s["override"].detail and '"b"' not in s["override"].detail
    # and the converse: its own pin broken while the owner's pin is fine
    put(rig, "c", offset=-60)
    put(rig, "b")
    s = states(rig)
    assert s["override"].status == "not_authenticated"
    assert '"c"' in s["override"].detail and '"b"' not in s["override"].detail


def test_c6_r2_unpinned_provider_is_authenticated_through_default_and_omits_pin(rig):
    put(rig, "default")
    put(rig, "b", offset=-60)
    s = states(rig)
    assert s["claude"].ok
    assert '"default": "ok"' in s["claude"].detail
    assert '"b"' not in s["claude"].detail


def test_c6_r2_pool_needs_only_one_usable_account(rig):
    put(rig, "default", offset=-60)
    put(rig, "a")
    s = states(rig)
    assert s["claude"].ok
    assert '"a": "ok"' in s["claude"].detail


def test_c6_r2_pool_with_no_usable_account_is_not_authenticated(rig):
    put(rig, "default", offset=-60)
    put(rig, "a", offset=-60)
    put(rig, "b")                      # reserved by the pins: not in the pool
    s = states(rig)
    assert not s["claude"].ok
    assert '"b"' not in s["claude"].detail
    assert '"a": "expired"' in s["claude"].detail


def test_c6_r2_pool_never_lists_an_account_pinned_by_another_provider(rig):
    put(rig, "default")
    put(rig, "b")
    put(rig, "a", offset=-60)
    s = states(rig)
    assert '"b"' not in s["claude"].detail


def test_c6_r3_local_executor_ignores_the_pin(rig, tmp_path):
    local = SimpleNamespace(kind="local")
    state = auth.check("second", rig.providers["second"], local, rig.paths.config)
    assert state.status in {"authenticated", "not_authenticated", "unknown", "no_script"}
    assert "accounts" not in state.detail or '"b"' not in state.detail
