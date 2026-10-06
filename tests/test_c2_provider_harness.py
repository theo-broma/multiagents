"""Proof that the C2 harness (tests/support/c2_harness.py) runs.

Not a characterization suite — one or two tests per entry point, enough to
show the seams actually reach real production code. The next phase's
characterizers own the full suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# Entry point 1 — scripts.run_action / exec_action, the plugin seam itself
# ---------------------------------------------------------------------------

def test_run_action_captures_a_controlled_scripts_exit_code_and_streams(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo "on stdout"; echo "on stderr" >&2; exit 10 ;;')
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check", tmp_path)
    assert code == 10
    assert out.strip() == "on stdout"
    assert err.strip() == "on stderr"


def test_exec_action_hands_over_argv_and_env_for_login(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    argv, env = h.exec_action("p", provider, h.FakeExecutor(), "login", tmp_path)
    assert argv == ["sh", str(tmp_path / "providers" / "p.sh"), "login"]
    assert env["MULTIAGENTS_PROVIDER"] == "p"
    assert env["MULTIAGENTS_EXECUTOR"] == "local"


def test_build_env_carries_the_docker_executors_private_state(tmp_path):
    """A characterizer touching the docker branch of build_env does not need
    the real DockerExecutor (see c1_harness.make_docker_executor for that) —
    FakeExecutor is enough to prove build_env reads what it says it reads."""
    provider = h.make_provider("p")
    executor = h.FakeExecutor(
        kind="docker", container="cty",
        private={"/container/path": "/host/path"},
        vault={"/container/vault": "/host/vault"},
        auth_proxy=True,
    )
    env = h.build_env("p", provider, executor)
    assert env["MULTIAGENTS_CONTAINER"] == "cty"
    assert env["MULTIAGENTS_PRIVATE_HOME"] == "/container/path"
    assert env["MULTIAGENTS_PRIVATE_BACKING"] == "/host/path"
    assert env["MULTIAGENTS_PRIVATE_VAULT"] == "/host/vault"
    assert env["MULTIAGENTS_AUTH_PROXY"] == "1"


# ---------------------------------------------------------------------------
# Entry point 2 — budget.read_provider / read_all, without a subscription
# ---------------------------------------------------------------------------

def test_read_provider_parses_a_scripts_budget_json(tmp_path):
    provider = h.make_provider("p")
    h.case_script(
        tmp_path, "p.sh",
        'budget) printf \'{"known": true, "headroom": 0.42, "source": "test"}\'; exit 0 ;;',
    )
    budget = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert budget.known is True
    assert budget.headroom == 0.42
    assert budget.severity == "normal"


def test_read_provider_caches_until_invalidated(tmp_path):
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(
        tmp_path, "p.sh",
        f'budget) echo -n x >> "{calls}"; printf \'{{"known": true, "headroom": 0.1}}\'; exit 0 ;;',
    )
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "x"        # second call served from cache

    h.invalidate_cache()
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "xx"       # cache cleared, script ran again


# ---------------------------------------------------------------------------
# Entry point 3 — providers.load_providers and the shipped providers.yaml
# ---------------------------------------------------------------------------

def test_shipped_providers_yaml_parses_into_the_eight_real_providers():
    # DI-R1: opencode-deepinfra ships too (disabled). CX-D1: codex ships as a
    # default provider; ZA-R1: so does opencode-zai,
    # extending opencode but disabled by default; PS-R8: so does agy-partner.
    providers = h.shipped_providers()
    assert set(providers) == {"claude", "opencode", "agy", "codex", "opencode-zai",
                              "agy-partner", "opencode-deepinfra", "opencode-zen"}
    assert providers["opencode-zai"].enabled is False
    assert providers["claude"].script_name == "claude.sh"
    assert providers["agy"].script_name == "agy.sh"


def test_load_providers_folds_extends_for_a_second_account():
    raw = {
        "claude": {"bin": "claude", "script": "claude.sh"},
        "claude-work": {"extends": "claude", "env": {"CLAUDE_CONFIG_DIR": "/work"}},
    }
    providers = h.load_providers(raw)
    assert providers["claude-work"].script_name == "claude.sh"   # inherited
    assert providers["claude-work"].family == "claude"           # shares the family
    assert providers["claude-work"].env["CLAUDE_CONFIG_DIR"] == "/work"


# ---------------------------------------------------------------------------
# Entry point 4 — the shipped provider shell scripts, invoked directly
# ---------------------------------------------------------------------------

def test_agy_script_usage_action_demands_a_budget_first():
    result = h.run_shipped_script("agy", "usage", env={})
    assert result.returncode == 64


def test_opencode_script_budget_action_reports_unknown_with_no_auth_store(tmp_path):
    result = h.run_shipped_script(
        "opencode", "budget", env={"XDG_DATA_HOME": str(tmp_path)})
    assert result.returncode == 0
    assert '"known": false' in result.stdout


# ---------------------------------------------------------------------------
# Entry point 5 — auth.py, the thin layer over the same seam
# ---------------------------------------------------------------------------

def test_check_reports_not_authenticated_from_a_controlled_script(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo "no credentials"; exit 10 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "not_authenticated"
    assert not state.ok
    assert state.fix == "multiagents auth login p"


def test_looks_like_auth_failure_ignores_a_bare_permission_denial():
    assert h.looks_like_auth_failure("error", "please log in again")
    assert not h.looks_like_auth_failure("error", "permission auto-denied for Bash")
