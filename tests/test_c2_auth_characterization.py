"""Characterization of authentication, the shipped provider scripts, and
config folding — what they DO, not what they should do.

Surface: `auth.check` / `auth.check_all` / `auth.login_command` /
`auth.looks_like_auth_failure`; the real shipped `claude.sh`, `agy.sh`,
`opencode.sh`; and `load_providers` / `resolve_inheritance` / `families`
against `providers.yaml`.

Findings this suite's failures/surprises are filed under live in
`context/review/C2-auth.md`, ids F130+. See that file for the reasoning
behind each one; this docstring only flags which test proves which finding.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# auth.check — exit code -> AuthState mapping
# ---------------------------------------------------------------------------

def test_check_maps_exit_0_to_authenticated_using_the_last_output_line(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh",
                  'check) echo "first line"; echo "logged in as bob"; exit 0 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "authenticated"
    assert state.ok is True
    assert state.detail == "logged in as bob"
    assert state.script.endswith("p.sh")
    assert state.fix == ""


def test_check_maps_exit_10_to_not_authenticated_and_builds_a_fix_command(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo "not logged in"; exit 10 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "not_authenticated"
    assert state.ok is False
    assert state.fix == "multiagents auth login p"


def test_check_maps_any_other_exit_code_to_unknown(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo "weird"; exit 3 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "unknown"
    assert state.ok is False
    # unknown still gets a fix suggestion, same as not_authenticated
    assert state.fix == "multiagents auth login p"


def test_check_falls_back_to_stderr_when_stdout_is_empty(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) echo "boom" >&2; exit 1 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "unknown"
    assert state.detail == "boom"


def test_check_with_no_output_at_all_reports_the_bare_exit_code(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "check) exit 5 ;;")
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "unknown"
    assert state.detail == "exit 5"


def test_check_truncates_the_detail_line_to_300_characters(tmp_path):
    provider = h.make_provider("p")
    long_line = "x" * 500
    h.case_script(tmp_path, "p.sh", f'check) echo "{long_line}"; exit 0 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert len(state.detail) == 300


def test_check_only_reads_the_last_line_of_multi_line_stdout(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh",
                  'check) printf "one\\ntwo\\nthree\\n"; exit 0 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.detail == "three"


def test_check_with_a_missing_script_reports_no_script_and_never_runs_anything(tmp_path):
    provider = h.make_provider("p")
    # No script written at all for this provider.
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    assert state.status == "no_script"
    assert state.ok is False
    assert state.fix == ""             # no_script carries no fix suggestion
    assert "no script for provider" in state.detail


def test_check_on_a_script_that_times_out_reports_unknown_not_a_crash(tmp_path):
    provider = h.make_provider("p")
    h.write_script(tmp_path, "p.sh", "sleep 3\nexit 0\n")
    # auth.check hardcodes CHECK_TIMEOUT=90s; drive scripts.run_action directly
    # with a short timeout to prove the TimeoutExpired path without waiting.
    code, out, err = h.run_action("p", provider, h.FakeExecutor(), "check",
                                  tmp_path, timeout=1)
    assert code == 124
    assert "TimeoutExpired" in err


def test_check_never_raises_on_a_script_with_no_shebang_and_not_executable(tmp_path):
    # A script that exists but the kernel refuses to run (no chmod +x, and
    # `case_script`/`write_script` always chmod — build one by hand).
    d = tmp_path / "providers"
    d.mkdir(parents=True)
    path = d / "p.sh"
    path.write_text('case "$1" in\ncheck) exit 0 ;;\nesac\n')   # no shebang, not +x
    provider = h.make_provider("p")
    state = h.check("p", provider, h.FakeExecutor(), tmp_path)
    # `.sh` always runs under `sh` (scripts.script_argv), which ignores both
    # the missing shebang and the missing +x bit entirely — `sh <path>` reads
    # the file as a text stream, permission bits notwithstanding.
    assert state.status == "authenticated"


# ---------------------------------------------------------------------------
# auth.check — the `profile` parameter
# ---------------------------------------------------------------------------

def test_check_passes_profile_through_as_an_env_var_and_records_it(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh",
                  'check) echo "saw profile=$MULTIAGENTS_PROFILE"; exit 0 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path, profile=h.HOST)
    assert state.profile == "host"
    assert state.detail == "saw profile=host"


def test_check_with_empty_profile_sets_no_env_var_at_all(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh",
                  'check) echo "profile-var=[$MULTIAGENTS_PROFILE]"; exit 0 ;;')
    state = h.check("p", provider, h.FakeExecutor(), tmp_path, profile="")
    assert state.profile == ""
    # MULTIAGENTS_PROFILE is unset (not set to ""), which sh renders as [ ]
    assert state.detail == "profile-var=[]"


def test_check_fix_message_names_host_only_for_the_host_profile(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'check) exit 10 ;;')
    normal = h.check("p", provider, h.FakeExecutor(), tmp_path, profile="")
    host = h.check("p", provider, h.FakeExecutor(), tmp_path, profile=h.HOST)
    assert normal.fix == "multiagents auth login p"
    assert host.fix == "multiagents auth login p --host"


# ---------------------------------------------------------------------------
# auth.check_all
# ---------------------------------------------------------------------------

def test_check_all_runs_every_provider_independently_and_keys_by_name(tmp_path):
    p1 = h.make_provider("alpha")
    p2 = h.make_provider("beta")
    h.case_script(tmp_path, "alpha.sh", 'check) exit 0 ;;')
    h.case_script(tmp_path, "beta.sh", 'check) exit 10 ;;')
    results = h.check_all({"alpha": p1, "beta": p2},
                          lambda name: h.FakeExecutor(), tmp_path)
    assert set(results) == {"alpha", "beta"}
    assert results["alpha"].status == "authenticated"
    assert results["beta"].status == "not_authenticated"


def test_check_all_calls_executor_for_once_per_provider_with_its_name(tmp_path):
    p1 = h.make_provider("alpha")
    h.case_script(tmp_path, "alpha.sh", 'check) exit 0 ;;')
    seen = []

    def executor_for(name):
        seen.append(name)
        return h.FakeExecutor()

    h.check_all({"alpha": p1}, executor_for, tmp_path)
    assert seen == ["alpha"]


def test_check_all_one_providers_crash_does_not_stop_the_others(tmp_path):
    p1 = h.make_provider("alpha")
    p2 = h.make_provider("beta")
    h.case_script(tmp_path, "beta.sh", 'check) exit 0 ;;')
    # alpha has NO script at all
    results = h.check_all({"alpha": p1, "beta": p2},
                          lambda name: h.FakeExecutor(), tmp_path)
    assert results["alpha"].status == "no_script"
    assert results["beta"].status == "authenticated"


# ---------------------------------------------------------------------------
# auth.login_command
# ---------------------------------------------------------------------------

def test_login_command_returns_argv_and_env_for_the_login_action(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    argv, env = h.login_command("p", provider, h.FakeExecutor(), tmp_path)
    assert argv == ["sh", str(tmp_path / "providers" / "p.sh"), "login"]
    assert env["MULTIAGENTS_PROVIDER"] == "p"
    assert "MULTIAGENTS_PROFILE" not in env


def test_login_command_with_no_script_returns_none(tmp_path):
    provider = h.make_provider("p")
    assert h.login_command("p", provider, h.FakeExecutor(), tmp_path) is None


def test_login_command_sets_multiagents_profile_only_when_given(tmp_path):
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    _, env = h.login_command("p", provider, h.FakeExecutor(), tmp_path,
                             profile=h.HOST)
    assert env["MULTIAGENTS_PROFILE"] == "host"


def test_login_commands_extra_env_can_override_everything_build_env_sets(tmp_path):
    """`extra_env` is applied LAST inside `scripts.build_env`
    (`env.update(extra or {})`), after `MULTIAGENTS_*` plumbing and the
    provider's own configured `env:`. There is no protected subset — a caller
    of `login_command` can override the provider's identity variable itself.
    """
    provider = h.make_provider("p", env={"FIXED": "provider-value"})
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    _, env = h.login_command(
        "p", provider, h.FakeExecutor(), tmp_path,
        extra_env={"MULTIAGENTS_PROVIDER": "hijacked", "FIXED": "overridden"})
    assert env["MULTIAGENTS_PROVIDER"] == "hijacked"
    assert env["FIXED"] == "overridden"


def test_login_commands_extra_env_and_profile_compose_profile_wins_on_conflict(tmp_path):
    """If a caller's `extra_env` also names `MULTIAGENTS_PROFILE`, the
    `profile=` kwarg overwrites it — `login_command` builds
    `env = dict(extra_env); if profile: env["MULTIAGENTS_PROFILE"] = profile`,
    so the explicit kwarg always wins over whatever extra_env said."""
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "login) exit 0 ;;")
    _, env = h.login_command(
        "p", provider, h.FakeExecutor(), tmp_path, profile=h.HOST,
        extra_env={"MULTIAGENTS_PROFILE": "something-else"})
    assert env["MULTIAGENTS_PROFILE"] == "host"


# ---------------------------------------------------------------------------
# auth.looks_like_auth_failure
# ---------------------------------------------------------------------------

def test_looks_like_auth_failure_catches_the_documented_markers():
    for stderr in (
        "Error: 401 Unauthorized",
        "invalid_api_key: The provided API key is invalid",
        "credentials not found",
        "Please log in to continue",
        "oauth token missing",
        "token expired, please renew",
    ):
        assert h.looks_like_auth_failure("failed", stderr), stderr


def test_looks_like_auth_failure_excludes_pure_permission_denials():
    assert not h.looks_like_auth_failure(
        "failed", "Tool use was auto-denied due to permission settings")
    assert not h.looks_like_auth_failure(
        "failed", "dangerously-skip-permissions is not allowed here")


def test_looks_like_auth_failure_a_permission_message_that_also_names_a_credential_still_counts():
    # The `_NOT_AUTH` veto only fires when NONE of ("log in", "login",
    # "credential", "token", "unauthorized") also appear in the blob.
    assert h.looks_like_auth_failure(
        "failed", "permission denied: credentials not found for this tool")


def test_looks_like_auth_failure_ignores_unrelated_runtime_errors():
    for stderr in (
        "connection refused",
        "rate limit exceeded",
        "context_length_exceeded: too many tokens",
        "500 Internal Server Error",
        "ECONNRESET",
        "the model is overloaded",
        "This organization has been suspended",
    ):
        assert not h.looks_like_auth_failure("failed", stderr), stderr


def test_looks_like_auth_failure_the_401_marker_is_an_unanchored_substring():
    """F13x: "401" is matched as a bare substring of the whole
    status+stderr blob, with no word boundary and no requirement that it look
    like an HTTP status. Any message that happens to contain the digits "401"
    anywhere — a byte count, an item count, part of a longer number — is
    classified as an authentication failure."""
    assert h.looks_like_auth_failure("failed", "wrote 40105 bytes to disk")
    assert h.looks_like_auth_failure("failed", "processed 401 items successfully")


def test_looks_like_auth_failure_misses_common_real_world_phrasings():
    """F13x: several phrasings a real CLI plausibly uses for an expired
    session or missing key are NOT in `_AUTH_MARKERS` and so are not caught —
    they share no substring with any marker in the list."""
    for stderr in (
        "your session has expired, please sign in again",
        "Error: session expired",
        "API key missing",
        "missing api key",
    ):
        assert not h.looks_like_auth_failure("failed", stderr), stderr


def test_looks_like_auth_failure_reads_status_too_not_just_stderr():
    assert h.looks_like_auth_failure("not authenticated", "")


def test_looks_like_auth_failure_is_case_insensitive():
    assert h.looks_like_auth_failure("FAILED", "INVALID API KEY")


# ---------------------------------------------------------------------------
# Shipped claude.sh — container-profile `check`, credential-file shapes
# ---------------------------------------------------------------------------

def _claude_docker_env(profile_dir: Path, **extra) -> dict:
    env = {"MULTIAGENTS_EXECUTOR": "docker",
          "MULTIAGENTS_PRIVATE_BACKING": str(profile_dir),
          "MULTIAGENTS_BIN": "true"}
    env.update(extra)
    return env


def test_claude_sh_check_empty_profile_directory_reports_not_authenticated(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 10
    assert "no credentials yet" in result.stdout


def test_claude_sh_check_treats_an_empty_oauth_block_as_logged_in(tmp_path):
    """Already known (see MAP.md / the failing host test in test_core.py):
    `{"claudeAiOauth": {}}` carries no `expiresAt` at all, so the script's own
    expiry-reading branch finds nothing to read and falls through to the
    unconditional "container profile is logged in" at the bottom of the
    `check` case. Recorded here as a baseline for the two variants below,
    which are NOT the already-known case."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 0
    assert "container profile is logged in" in result.stdout


def test_claude_sh_check_unparseable_json_is_also_reported_as_logged_in(tmp_path):
    """F130: a genuinely CORRUPT credentials file — not merely missing a
    field, but invalid JSON that the embedded python cannot parse at all —
    hits the same `except Exception: sys.exit(0)` in the expiry-reading
    heredoc, prints nothing, and the shell script falls through to the same
    unconditional "container profile is logged in" / exit 0. A corrupt file
    is treated identically to a genuinely valid one."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".credentials.json").write_text("not json at all {{{")
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 0
    assert "container profile is logged in" in result.stdout


def test_claude_sh_check_an_access_token_with_no_expiresat_is_also_logged_in(tmp_path):
    """F130 (same root cause as the two tests above): a credentials file that
    HAS an accessToken but no `expiresAt` key at all is, again, silently
    unreadable by the clock-extraction loop (`if not block.get('expiresAt'):
    continue`) and falls through to the same "logged in" default. There is no
    code path in this script that ever reports "present but unparseable" or
    "present but incomplete" — every failure to read a clock collapses into
    success."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "abc"}}))
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 0
    assert "container profile is logged in" in result.stdout


def test_claude_sh_check_expired_access_with_no_refresh_token_reports_not_authenticated(tmp_path):
    """The one case in this family that IS caught: an access token whose
    `expiresAt` is in the past, with nothing that could renew it (`refresh`
    ends up equal to `access`, taking the "no refresh token to renew it with"
    branch)."""
    profile = tmp_path / "profile"
    profile.mkdir()
    past_ms = int((time.time() - 100_000) * 1000)
    (profile / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": past_ms}}))
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 10
    assert "no refresh token to renew it with" in result.stdout


def test_claude_sh_check_expired_access_with_a_live_refresh_token_still_reports_logged_in(tmp_path):
    """A dead access token next to a REFRESH token that is still valid is
    reported as authenticated (exit 0), with a note that the host will renew
    it before the next spawn — this is the documented "renewal" behaviour,
    not a false positive, since the refresh token is what actually decides
    whether re-authentication is required."""
    profile = tmp_path / "profile"
    profile.mkdir()
    past_ms = int((time.time() - 100_000) * 1000)
    future_ms = int((time.time() + 100_000) * 1000)
    (profile / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"expiresAt": past_ms, "refreshTokenExpiresAt": future_ms},
    }))
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 0
    assert "logged in" in result.stdout
    assert "the host renews it before the next spawn" in result.stdout


def test_claude_sh_check_expired_refresh_token_is_correctly_reported_as_expired(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    past_access = int((time.time() - 200_000) * 1000)
    past_refresh = int((time.time() - 100_000) * 1000)
    (profile / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"expiresAt": past_access, "refreshTokenExpiresAt": past_refresh},
    }))
    result = h.run_shipped_script("claude", "check", _claude_docker_env(profile))
    assert result.returncode == 10
    assert "the refresh token, not just the access token" in result.stdout


def test_claude_sh_check_profile_host_bypasses_the_container_branch_entirely(tmp_path):
    """`MULTIAGENTS_PROFILE=host` clears `PROFILE` unconditionally, so the
    container's credentials.json is never consulted at all — even one that
    would report "logged in" moments earlier is ignored, and the script
    instead shells out to `$BIN auth status --json` on the (here, fake) host
    binary."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    result = h.run_shipped_script(
        "claude", "check",
        _claude_docker_env(profile, MULTIAGENTS_PROFILE="host"))
    # `true` as BIN prints nothing, so `auth status --json` output is empty
    # and matches neither the loggedIn:true nor any other special case here —
    # it falls to the bare `*)` arm, "not logged in".
    assert result.returncode == 10
    assert "not logged in" in result.stdout


def test_claude_sh_check_host_path_unreachable_binary_reports_could_not_run(tmp_path):
    result = h.run_shipped_script(
        "claude", "check", {"MULTIAGENTS_BIN": "/nonexistent/claude"})
    assert result.returncode == 20
    assert "could not run" in result.stdout


def test_claude_sh_launch_never_sets_claude_config_dir(tmp_path):
    """The one property `test_core.py` already pins directly on the source
    text; re-asserted here against the actual shipped file this suite reads,
    for the `launch` action specifically rather than the whole file."""
    body = h.shipped_script_path("claude").read_text()
    launch = body[body.index("\nlaunch)"):]
    assert "CLAUDE_CONFIG_DIR" not in launch


# ---------------------------------------------------------------------------
# Shipped agy.sh — same "presence over validity" pattern, less guarded
# ---------------------------------------------------------------------------

def test_agy_sh_check_docker_no_token_file_reports_not_authenticated(tmp_path):
    gemini = tmp_path / "profile" / ".gemini"
    gemini.mkdir(parents=True)
    result = h.run_shipped_script(
        "agy", "check",
        {"MULTIAGENTS_EXECUTOR": "docker", "MULTIAGENTS_PRIVATE_BACKING": str(gemini),
         "MULTIAGENTS_BIN": "true"})
    assert result.returncode == 10
    assert "not been logged in inside the container" in result.stdout


def test_agy_sh_check_docker_treats_any_non_empty_token_file_as_present(tmp_path):
    """F131: unlike claude.sh, agy.sh's container branch does not parse the
    token at all — `[ -s "$backing/$TOKEN_REL" ]` only asks "is this file
    non-empty", so ANY content, however invalid, reports "container token
    present" with exit 0. There is no expiry, no format check, nothing —
    every guard claude.sh grew for its own credential file (F130 above) was
    never applied here."""
    gemini_dir = tmp_path / "profile" / ".gemini"
    token_dir = gemini_dir / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text("not-a-real-token-just-garbage")
    result = h.run_shipped_script(
        "agy", "check",
        {"MULTIAGENTS_EXECUTOR": "docker", "MULTIAGENTS_PRIVATE_BACKING": str(gemini_dir),
         "MULTIAGENTS_BIN": "true"})
    assert result.returncode == 0
    assert "container token present" in result.stdout


def test_agy_sh_check_host_path_with_no_binary_reports_unclear_not_authenticated(tmp_path):
    """The host (non-docker) branch is comparatively careful: an unreadable
    or unexpected CLI reply falls to "unclear", exit 20 (unknown) — never a
    false "logged in"."""
    result = h.run_shipped_script(
        "agy", "check", {"MULTIAGENTS_EXECUTOR": "local",
                         "MULTIAGENTS_BIN": "/nonexistent/agy"})
    assert result.returncode == 20
    assert "unclear:" in result.stdout


def test_agy_sh_check_host_path_a_silent_success_exit_is_also_just_unclear(tmp_path):
    result = h.run_shipped_script(
        "agy", "check", {"MULTIAGENTS_EXECUTOR": "local", "MULTIAGENTS_BIN": "true"})
    assert result.returncode == 20
    assert "unclear:" in result.stdout


def test_agy_sh_check_host_path_recognizes_the_documented_success_marker(tmp_path):
    fake_bin = tmp_path / "agy"
    fake_bin.write_text('#!/bin/sh\necho \'{"status":"SUCCESS"}\'\n')
    fake_bin.chmod(0o755)
    result = h.run_shipped_script(
        "agy", "check", {"MULTIAGENTS_EXECUTOR": "local", "MULTIAGENTS_BIN": str(fake_bin)})
    assert result.returncode == 0
    assert "logged in (host keyring)" in result.stdout


# ---------------------------------------------------------------------------
# Shipped opencode.sh — no credential file at all, trusts the CLI's own text
# ---------------------------------------------------------------------------

def test_opencode_sh_check_zero_credentials_reports_not_authenticated(tmp_path):
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text('#!/bin/sh\necho "0 credentials stored"\n')
    fake_bin.chmod(0o755)
    result = h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": str(fake_bin)})
    assert result.returncode == 10
    assert "no stored credentials" in result.stdout


def test_opencode_sh_check_extracts_the_reported_credential_count(tmp_path):
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text('#!/bin/sh\necho "found 3 credentials stored"\n')
    fake_bin.chmod(0o755)
    result = h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": str(fake_bin)})
    assert result.returncode == 0
    assert "3 stored credential(s)" in result.stdout


def test_opencode_sh_check_count_extraction_fails_when_the_digit_opens_the_line(tmp_path):
    """F135: the extraction sed is `s/.*[^0-9]\\(...\\) credential.*/\\1/p` —
    it requires a NON-DIGIT character immediately before the count. When the
    CLI's own line starts directly with the digits (no leading word or
    space), `[^0-9]` has nothing to match before the first digit and the
    whole substitution fails silently, `n` stays empty, and `${n:-1}`
    reports exactly "1" regardless of the real count. Moving the same
    sentence one word earlier (see the test above) makes extraction work —
    this is purely a property of the CLI's own phrasing, invisible from this
    script's contract."""
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text('#!/bin/sh\necho "3 credentials stored"\n')
    fake_bin.chmod(0o755)
    result = h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": str(fake_bin)})
    assert result.returncode == 0
    assert "1 stored credential(s)" in result.stdout


def test_opencode_sh_check_defaults_to_one_credential_on_any_unmatched_output(tmp_path):
    """F132: the count-extraction `sed` only fires when the CLI's own output
    contains the literal word "credential"; anything else that is non-empty,
    does not say "0 credentials", and exits 0 falls to `${n:-1}` — reporting
    exactly "1 stored credential(s)" regardless of what the CLI actually
    printed. There is no credential file this script ever reads on its own;
    it trusts `$BIN providers list`'s exit code and a fixed default entirely."""
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text(
        '#!/bin/sh\necho "some unrelated informational message, not about credentials"\n')
    fake_bin.chmod(0o755)
    result = h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": str(fake_bin)})
    assert result.returncode == 0
    assert "1 stored credential(s)" in result.stdout


def test_opencode_sh_check_defaults_to_one_credential_even_on_silent_success(tmp_path):
    """Same root cause as the test above, pushed to its most surprising
    case: a binary that runs, exits 0, and prints NOTHING is still reported
    as one stored credential and exit 0 (authenticated)."""
    result = h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": "true"})
    assert result.returncode == 0
    assert "1 stored credential(s)" in result.stdout


def test_opencode_sh_check_missing_binary_reports_could_not_run(tmp_path):
    result = h.run_shipped_script(
        "opencode", "check", {"MULTIAGENTS_BIN": "/nonexistent/opencode"})
    assert result.returncode == 20
    assert "could not run" in result.stdout


# ---------------------------------------------------------------------------
# Config folding — load_providers / resolve_inheritance / families
# ---------------------------------------------------------------------------

def test_load_providers_builds_the_six_real_shipped_providers():
    # CX-D1: codex ships as a default provider; ZA-R1: so does opencode-zai
    # (disabled by default).
    providers = h.shipped_providers()
    # PS-R8: agy-partner is the sixth.
    assert set(providers) == {"claude", "opencode", "agy", "codex", "opencode-zai",
                              "agy-partner"}
    assert providers["opencode-zai"].enabled is False
    assert providers["claude"].script_name == "claude.sh"
    assert providers["claude"].bin == "claude"
    assert providers["claude"].container_private_home == [".claude"]


def test_extends_shallow_merges_the_parent_one_level_deep():
    raw = {
        "base": {"bin": "claude", "env": {"A": "1"}, "family": "claude"},
        "child": {"extends": "base", "env": {"B": "2"}},
    }
    providers = h.load_providers(raw)
    # env is a dict, so it recurses: child keeps A from the parent and adds B.
    assert providers["child"].env == {"A": "1", "B": "2"}
    assert providers["child"].bin == "claude"       # inherited untouched


def test_extends_a_missing_parent_is_left_unresolved_not_raised(tmp_path):
    """`resolve_inheritance`'s own docstring says this is deliberate: "A
    missing base is left alone rather than raised on." Pinning the actual
    shape of "left alone" — the child keeps ONLY what it wrote itself, none
    of a parent's fields, and the raw `extends: ghost` key survives into the
    resolved dict unchanged."""
    raw = {"child": {"extends": "ghost", "bin": "x", "env": {"A": "1"}}}
    resolved = h.resolve_inheritance(raw)
    assert resolved == raw               # untouched: no merge happened at all
    providers = h.load_providers(raw)
    assert providers["child"].env == {"A": "1"}
    assert providers["child"].bin == "x"


def test_extends_a_missing_parent_still_sets_family_to_the_ghost_name():
    """F133: `Provider.from_dict`'s family fallback is
    `family = data.get("family") or data.get("extends") or name` — it reads
    the UNRESOLVED `extends:` value even when that base was never found by
    `resolve_inheritance`. The child is placed in a family named after a
    provider that does not exist, and `families()` then reports a group
    containing only the orphan — not an error, not a fallback to `name`."""
    raw = {"child": {"extends": "ghost", "bin": "x"}}
    providers = h.load_providers(raw)
    assert providers["child"].family == "ghost"
    assert h.families(providers) == {"ghost": ["child"]}


def test_extends_a_direct_cycle_does_not_hang_and_merges_once_each_way():
    """Two providers extending each other: `resolve_inheritance` guards with
    a per-name `seen` set, so this terminates rather than looping forever —
    but each side still picks up ONE round of the other's fields on the way,
    which is not "left alone" the way a missing parent is."""
    raw = {
        "a": {"extends": "b", "env": {"FROM_A": "1"}},
        "b": {"extends": "a", "env": {"FROM_B": "1"}},
    }
    providers = h.load_providers(raw)
    assert providers["a"].env == {"FROM_A": "1", "FROM_B": "1"}
    assert providers["b"].env == {"FROM_B": "1", "FROM_A": "1"}


def test_extends_self_reference_does_not_hang(tmp_path):
    raw = {"a": {"extends": "a", "env": {"X": "1"}}}
    providers = h.load_providers(raw)
    assert providers["a"].env == {"X": "1"}
    assert providers["a"].family == "a"


def test_extends_a_list_field_is_replaced_wholesale_never_merged():
    """`config.deep_merge` only recurses into dict values; a list on the
    child completely REPLACES the parent's list rather than concatenating or
    deduplicating against it."""
    raw = {
        "base": {"bin": "claude", "models_include": ["opus", "sonnet"]},
        "child": {"extends": "base", "models_include": ["haiku"]},
    }
    providers = h.load_providers(raw)
    assert providers["child"].models_include == ["haiku"]


def test_extends_a_scalar_field_the_child_omits_is_inherited_verbatim():
    raw = {
        "base": {"bin": "claude", "usage_mode": "delta"},
        "child": {"extends": "base"},
    }
    providers = h.load_providers(raw)
    assert providers["child"].usage_mode == "delta"


def test_enabled_false_still_produces_a_provider_object_usable_is_false():
    raw = {"p": {"bin": "x", "enabled": False}}
    providers = h.load_providers(raw)
    assert "p" in providers                  # not filtered out of the dict
    assert providers["p"].enabled is False
    assert providers["p"].usable() is False


def test_an_unknown_key_in_a_provider_block_is_silently_dropped():
    """F134: `Provider.from_dict` reads a fixed set of named keys off the
    raw dict and never checks for anything left over — a typo'd key
    (`mdoels_include:` for `models_include:`) or an entirely made-up one
    produces no error, no warning, and simply vanishes. The resulting
    `Provider` is indistinguishable from one where the key was never
    written."""
    raw = {"p": {"bin": "x", "totally_unknown_key": "surprise",
                "another_bogus_key": [1, 2, 3]}}
    providers = h.load_providers(raw)
    p = providers["p"]
    assert not hasattr(p, "totally_unknown_key")
    assert not hasattr(p, "another_bogus_key")


def test_families_groups_by_explicit_family_over_extends_over_name():
    raw = {
        "claude": {"bin": "claude"},
        "claude-work": {"extends": "claude"},               # family := extends
        "claude-personal": {"extends": "claude", "family": "claude-personal"},
        "opencode": {"bin": "opencode"},                     # family := name
    }
    providers = h.load_providers(raw)
    families = h.families(providers)
    assert families["claude"] == ["claude", "claude-work"]
    assert families["claude-personal"] == ["claude-personal"]
    assert families["opencode"] == ["opencode"]


def test_families_sorts_provider_names_within_each_group():
    raw = {
        "z-instance": {"bin": "x", "family": "fam"},
        "a-instance": {"bin": "x", "family": "fam"},
    }
    providers = h.load_providers(raw)
    assert h.families(providers)["fam"] == ["a-instance", "z-instance"]


def test_shipped_providers_yaml_folds_into_six_independent_families():
    # CX-D1: codex ships in its own family; ZA-R1: so does opencode-zai, which
    # extends opencode but is NOT folded into opencode's family.
    providers = h.shipped_providers()
    families = h.families(providers)
    assert families == {"claude": ["claude"], "agy": ["agy"],
                        "opencode": ["opencode"], "codex": ["codex"],
                        "opencode-zai": ["opencode-zai"],
                        "agy-partner": ["agy-partner"]}
