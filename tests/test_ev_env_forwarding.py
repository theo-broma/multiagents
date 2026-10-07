"""EV — what environment reaches scripts and agents (context/specs/env-forwarding.md).

EV-R1 (scripts.build_env allowlist), EV-R3 (executor build_env honours
`blocked` for base keys), EV-R4 (default executor environment unchanged).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as ex_h  # noqa: E402
import c2_harness as h  # noqa: E402

ALLOWLISTED = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TZ", "TMPDIR",
    "CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "MULTIAGENTS_CODEX_PROFILE", "MULTIAGENTS_OPENCODE_PLAN", "MULTIAGENTS_ZAI_ORIGIN",
]

SECRETS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
    "OPENAI_BASE_URL", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "EV_SENTINEL_SECRET",
    "MULTIAGENTS_EV_UNLISTED", "MULTIAGENTS_TOKEN", "MULTIAGENTS_PROFILE_X",
]


def _script_env(extra=None, provider=None):
    provider = provider or h.make_provider("p")
    return h.build_env("p", provider, h.FakeExecutor(), extra=extra)


# ---------------------------------------------------------------------------
# EV-R1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SECRETS)
def test_ev_r1_unlisted_ambient_variable_is_absent(monkeypatch, name):
    monkeypatch.setenv(name, "ev-sentinel-value")
    assert name not in _script_env()


@pytest.mark.parametrize("name", ALLOWLISTED)
def test_ev_r1_each_allowlisted_variable_is_forwarded_when_set(monkeypatch, name):
    monkeypatch.setenv(name, f"/ev/{name}")
    assert _script_env()[name] == f"/ev/{name}"


@pytest.mark.parametrize("name", ["TZ", "XDG_RUNTIME_DIR", "HTTPS_PROXY", "LC_NUMERIC",
                                  "MULTIAGENTS_ZAI_ORIGIN", "SSL_CERT_DIR"])
def test_ev_r1_allowlisted_variable_that_is_unset_is_absent(monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    assert name not in _script_env()


def test_ev_r1_lc_star_is_forwarded_as_a_family(monkeypatch):
    monkeypatch.setenv("LC_TELEPHONE", "de_DE.UTF-8")
    monkeypatch.setenv("LC_X_EV_ODD", "x")
    env = _script_env()
    assert env["LC_TELEPHONE"] == "de_DE.UTF-8"
    assert env["LC_X_EV_ODD"] == "x"


def test_ev_r1_lookalike_names_are_not_matched_by_the_lc_pattern(monkeypatch):
    # "wider than LC_*" is a decision, so a name merely containing LC is not in.
    monkeypatch.setenv("LCX_SECRET", "s")
    monkeypatch.setenv("XLC_ALL", "s")
    env = _script_env()
    assert "LCX_SECRET" not in env
    assert "XLC_ALL" not in env


def test_ev_r1_empty_allowlisted_value_is_forwarded_as_empty(monkeypatch):
    monkeypatch.setenv("TZ", "")
    assert _script_env()["TZ"] == ""


def test_ev_r1_unlisted_multiagents_variable_is_absent_but_named_selectors_pass(monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_EV_UNLISTED", "x")
    monkeypatch.setenv("MULTIAGENTS_CODEX_PROFILE", "work")
    env = _script_env()
    assert "MULTIAGENTS_EV_UNLISTED" not in env
    assert env["MULTIAGENTS_CODEX_PROFILE"] == "work"


def test_ev_r1_ambient_values_of_computed_keys_do_not_win(monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_PROVIDER", "ambient-liar")
    monkeypatch.setenv("MULTIAGENTS_UID", "99999")
    env = _script_env()
    assert env["MULTIAGENTS_PROVIDER"] == "p"
    assert env["MULTIAGENTS_UID"] == str(os.getuid())
    assert env["MULTIAGENTS_EXECUTOR"] == "local"


def test_ev_r1_computed_multiagents_keys_are_present():
    env = _script_env()
    for key in ("MULTIAGENTS_PROVIDER", "MULTIAGENTS_EXECUTOR", "MULTIAGENTS_UID",
                "MULTIAGENTS_GID", "MULTIAGENTS_BIN", "MULTIAGENTS_BIN_ERROR"):
        assert key in env


def test_ev_r1_the_environment_is_exactly_allowlist_plus_computed(monkeypatch):
    """With only a sentinel plus a handful of allowlisted values set, every key
    in the result is allowlisted, `LC_*`, or `MULTIAGENTS_*` computed."""
    monkeypatch.setenv("EV_SENTINEL_SECRET", "x")
    monkeypatch.setenv("PYTHONPATH_EV", "x")
    env = _script_env()
    allowed = set(ALLOWLISTED)
    stray = {k for k in env
             if k not in allowed and not k.startswith("LC_") and not k.startswith("MULTIAGENTS_")}
    assert stray == set()


def test_ev_r1_provider_env_block_is_forwarded(monkeypatch):
    monkeypatch.delenv("EV_FROM_BLOCK", raising=False)
    env = _script_env(provider=h.make_provider("p", env={"EV_FROM_BLOCK": "yes"}))
    assert env["EV_FROM_BLOCK"] == "yes"


def test_ev_r1_provider_env_block_beats_the_ambient_allowlisted_value(monkeypatch):
    monkeypatch.setenv("TZ", "Ambient/Zone")
    env = _script_env(provider=h.make_provider("p", env={"TZ": "Block/Zone"}))
    assert env["TZ"] == "Block/Zone"


def test_ev_r1_provider_env_block_can_still_expand_an_ambient_variable(monkeypatch):
    # Expansion reads the host environment even for names not on the allowlist;
    # the *value* is forwarded because the provider's block names it.
    monkeypatch.setenv("EV_SENTINEL_SECRET", "from-host")
    env = _script_env(provider=h.make_provider("p", env={"WANTED": "$EV_SENTINEL_SECRET"}))
    assert env["WANTED"] == "from-host"
    assert "EV_SENTINEL_SECRET" not in env


def test_ev_r1_action_overlay_is_forwarded(monkeypatch):
    monkeypatch.delenv("EV_OVERLAY", raising=False)
    assert _script_env(extra={"EV_OVERLAY": "1"})["EV_OVERLAY"] == "1"


def test_ev_r1_action_overlay_beats_ambient_and_provider_block(monkeypatch):
    monkeypatch.setenv("TZ", "Ambient/Zone")
    env = _script_env(extra={"TZ": "Overlay/Zone", "X": "o"},
                      provider=h.make_provider("p", env={"TZ": "Block/Zone", "X": "b"}))
    assert env["TZ"] == "Overlay/Zone"
    assert env["X"] == "o"


def test_ev_r1_overlay_may_name_an_unlisted_key_that_is_also_ambient(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient")
    env = _script_env(extra={"ANTHROPIC_API_KEY": "overlay"})
    assert env["ANTHROPIC_API_KEY"] == "overlay"


def test_ev_r1_protected_executor_key_still_cannot_be_overlaid_or_ambient(monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_EXECUTOR", "docker")
    env = _script_env(extra={"MULTIAGENTS_EXECUTOR": "docker"})
    assert env["MULTIAGENTS_EXECUTOR"] == "local"


def test_ev_r1_host_profile_selector_is_never_inherited_from_ambient(monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_PROFILE", "ambient")
    assert "MULTIAGENTS_PROFILE" not in _script_env()
    assert _script_env(extra={"MULTIAGENTS_PROFILE": "p1"})["MULTIAGENTS_PROFILE"] == "p1"


def test_ev_r1_binary_is_still_resolved_from_the_ambient_path(monkeypatch, tmp_path):
    exe = tmp_path / "ev-fake-cli"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    env = _script_env(provider=h.make_provider("p", bin="ev-fake-cli"))
    assert env["MULTIAGENTS_BIN"] == str(exe)


def test_ev_r1_a_script_cannot_see_an_ambient_secret(monkeypatch, tmp_path):
    """End to end through a real script: the child shell does not see it."""
    monkeypatch.setenv("EV_SENTINEL_SECRET", "leak")
    monkeypatch.setenv("TZ", "Ev/Zone")
    h.case_script(tmp_path, "p.sh",
                  'check) echo "s=${EV_SENTINEL_SECRET:-unset} tz=${TZ:-unset}"; exit 0 ;;')
    code, out, _ = h.run_action("p", h.make_provider("p"), h.FakeExecutor(), "check", tmp_path)
    assert code == 0
    assert "s=unset" in out
    assert "tz=Ev/Zone" in out


# ---------------------------------------------------------------------------
# EV-R3 / EV-R4 — executor build_env
# ---------------------------------------------------------------------------

def _ex_env(**kw):
    kw.setdefault("passthrough", [])
    kw.setdefault("blocked", [])
    kw.setdefault("home", None)
    kw.setdefault("identity", {})
    return ex_h.build_env(**kw)


@pytest.mark.parametrize("name", ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
                                  "TMPDIR", "SHELL", "USER"])
def test_ev_r3_each_blocked_base_key_is_not_forwarded(monkeypatch, name):
    monkeypatch.setenv(name, "/ev/value")
    assert name not in _ex_env(blocked=[name])


def test_ev_r3_blocking_one_base_key_leaves_the_others(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-ev")
    monkeypatch.setenv("TZ", "Ev/Zone")
    env = _ex_env(blocked=["TERM"])
    assert "TERM" not in env
    assert env["TZ"] == "Ev/Zone"


def test_ev_r3_blocked_passthrough_key_is_not_forwarded(monkeypatch):
    monkeypatch.setenv("EV_TOOL_HOME", "/opt/x")
    env = _ex_env(passthrough=["EV_TOOL_HOME"], blocked=["EV_TOOL_HOME"])
    assert "EV_TOOL_HOME" not in env


def test_ev_r3_blocked_passthrough_literal_is_not_set():
    assert "EV_LIT" not in _ex_env(passthrough=["EV_LIT=v"], blocked=["EV_LIT"])


def test_ev_r3_base_key_blocked_stays_blocked_when_also_in_passthrough(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-ev")
    assert "TERM" not in _ex_env(passthrough=["TERM"], blocked=["TERM"])


def test_ev_r3_blocked_is_not_affected_by_unrelated_names(monkeypatch):
    monkeypatch.setenv("PATH", "/fake/bin")
    assert _ex_env(blocked=["NOT_A_KEY"])["PATH"] == "/fake/bin"


def test_ev_r4_empty_blocked_default_passthrough_forwards_every_set_base_key(monkeypatch):
    keys = ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "SHELL", "USER"]
    for k in keys:
        monkeypatch.setenv(k, f"/ev/{k}")
    env = _ex_env()
    for k in keys:
        assert env[k] == f"/ev/{k}"


def test_ev_r4_default_environment_is_exactly_base_keys_plus_fixed_additions(monkeypatch):
    keys = ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "SHELL", "USER"]
    for k in keys:
        monkeypatch.setenv(k, f"/ev/{k}")
    monkeypatch.setenv("EV_SENTINEL_SECRET", "x")
    env = _ex_env()
    assert {k for k in env if k in keys} == set(keys)
    assert "EV_SENTINEL_SECRET" not in env
    # the additions main makes today, with no home and no identity
    assert set(env) - set(keys) <= {"GIT_CONFIG_COUNT"} | {
        k for k in env if k.startswith("GIT_CONFIG_")}


def test_ev_r4_unset_base_keys_stay_absent(monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    env = _ex_env()
    assert "TMPDIR" not in env and "TZ" not in env


def test_ev_r4_home_and_identity_behaviour_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", "/real/home")
    env = _ex_env(home=tmp_path / "agent", identity={"GIT_AUTHOR_NAME": "a"})
    assert env["HOME"] == str(tmp_path / "agent")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "agent" / ".config")
    assert env["MULTIAGENTS_USER_HOME"] == str(Path.home())
    assert env["GIT_AUTHOR_NAME"] == "a"


def test_ev_r4_codex_profile_forwarding_with_home_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("MULTIAGENTS_CODEX_PROFILE", "work")
    assert _ex_env(home=tmp_path)["MULTIAGENTS_CODEX_PROFILE"] == "work"
    assert "MULTIAGENTS_CODEX_PROFILE" not in _ex_env(
        home=tmp_path, blocked=["MULTIAGENTS_CODEX_PROFILE"])
