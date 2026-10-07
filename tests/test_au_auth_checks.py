"""AU: connection checks say "unknown" (exit 20), never "logged in", when they
cannot tell. Contract: context/specs/auth-checks-uncertain.md.

Inverted characterization tests live in test_c2_auth_characterization.py
(AU-R1 profile branch, AU-R2, AU-R3) and test_c2_auditor_findings.py (AU-R4).
Everything here uses synthetic files under tmp_path.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c2_harness as h  # noqa: E402


def _future_ms() -> int:
    return int((time.time() + 100_000) * 1000)


def _past_ms() -> int:
    return int((time.time() - 100_000) * 1000)


def _profile_check(tmp_path, creds: str | None):
    profile = tmp_path / "profile"
    profile.mkdir()
    if creds is not None:
        (profile / ".credentials.json").write_text(creds)
    return h.run_shipped_script("claude", "check", {
        "MULTIAGENTS_EXECUTOR": "docker",
        "MULTIAGENTS_PRIVATE_BACKING": str(profile),
        "MULTIAGENTS_BIN": "true"})


def _vault_check(tmp_path, creds: str | None):
    profile = tmp_path / "profile"
    vault = tmp_path / "vault"
    profile.mkdir()
    vault.mkdir()
    if creds is not None:
        (vault / ".credentials.json").write_text(creds)
    return h.run_shipped_script("claude", "check", {
        "MULTIAGENTS_EXECUTOR": "docker",
        "MULTIAGENTS_PRIVATE_BACKING": str(profile),
        "MULTIAGENTS_PRIVATE_VAULT": str(vault),
        "MULTIAGENTS_BIN": "true"})


# ---------------------------------------------------------------- AU-R1

def test_au_r1_profile_branch_valid_unexpired_file_still_reports_logged_in(tmp_path):
    result = _profile_check(tmp_path, json.dumps(
        {"claudeAiOauth": {"accessToken": "abc", "expiresAt": _future_ms()}}))
    assert result.returncode == 0
    assert "logged in" in result.stdout


def test_au_r1_profile_branch_expired_clock_keeps_its_outcome(tmp_path):
    result = _profile_check(tmp_path, json.dumps(
        {"claudeAiOauth": {"accessToken": "abc", "expiresAt": _past_ms()}}))
    assert result.returncode == 10


def test_au_r1_profile_branch_missing_file_keeps_its_outcome(tmp_path):
    assert _profile_check(tmp_path, None).returncode == 10


def test_au_r1_profile_branch_empty_oauth_block_keeps_its_outcome(tmp_path):
    result = _profile_check(tmp_path, '{"claudeAiOauth": {}}')
    assert result.returncode == 0
    assert "container profile is logged in" in result.stdout


def test_au_r1_vault_branch_valid_unexpired_file_still_reports_authenticated(tmp_path):
    result = _vault_check(tmp_path, json.dumps(
        {"claudeAiOauth": {"accessToken": "abc", "expiresAt": _future_ms()}}))
    assert result.returncode == 0
    assert "authenticated" in result.stdout


def test_au_r1_vault_branch_expired_clock_keeps_its_outcome(tmp_path):
    result = _vault_check(tmp_path, json.dumps(
        {"claudeAiOauth": {"accessToken": "abc", "expiresAt": _past_ms()}}))
    assert result.returncode == 10


def test_au_r1_vault_branch_unparseable_json_is_unknown(tmp_path):
    result = _vault_check(tmp_path, "not json at all {{{")
    assert result.returncode == 20
    assert "unknown" in result.stdout.lower()
    assert "unparseable" in result.stdout.lower()
    assert "authenticated" not in result.stdout.replace("not authenticated", "")


def test_au_r1_vault_branch_access_token_with_no_expiresat_is_unknown(tmp_path):
    result = _vault_check(tmp_path, json.dumps({"claudeAiOauth": {"accessToken": "abc"}}))
    assert result.returncode == 20
    assert "unknown" in result.stdout.lower()
    assert "no expiry" in result.stdout.lower()


# ---------------------------------------------------------------- AU-R2

def _opencode(tmp_path, body: str):
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text("#!/bin/sh\n" + body + "\n")
    fake_bin.chmod(0o755)
    return h.run_shipped_script("opencode", "check", {"MULTIAGENTS_BIN": str(fake_bin)})


@pytest.mark.parametrize("output", [
    "no providers configured",
    "Credentials: none",          # names credentials, but neither pattern
    "   ",                        # whitespace only
    "error: something odd happened",
])
def test_au_r2_opencode_sh_check_unrecognised_wording_is_unknown(tmp_path, output):
    result = _opencode(tmp_path, f'echo "{output}"')
    assert result.returncode == 20
    assert "unknown" in result.stdout.lower()
    assert "stored credential(s)" not in result.stdout


def test_au_r2_opencode_sh_check_unrecognised_wording_on_stderr_only_is_unknown(tmp_path):
    result = _opencode(tmp_path, 'echo "something" >&2')
    assert result.returncode == 20
    assert "stored credential(s)" not in result.stdout


def test_au_r2_opencode_sh_check_a_parsed_positive_count_keeps_its_outcome(tmp_path):
    result = _opencode(tmp_path, 'echo "found 2 credentials stored"')
    assert result.returncode == 0
    assert "2 stored credential(s)" in result.stdout


def test_au_r2_opencode_sh_check_zero_credentials_keeps_its_outcome(tmp_path):
    result = _opencode(tmp_path, 'echo "0 credentials"')
    assert result.returncode == 10
    assert "no stored credentials" in result.stdout


# ---------------------------------------------------------------- AU-R3

def _agy_env(tmp_path, content: str | None):
    gemini = tmp_path / "profile" / ".gemini"
    (gemini / "antigravity-cli").mkdir(parents=True)
    if content is not None:
        (gemini / "antigravity-cli" / "antigravity-oauth-token").write_text(content)
    return h.run_shipped_script("agy", "check", {
        "MULTIAGENTS_EXECUTOR": "docker", "MULTIAGENTS_PRIVATE_BACKING": str(gemini),
        "MULTIAGENTS_BIN": "true"})


def test_au_r3_agy_sh_check_present_token_says_not_verified_and_exits_0(tmp_path):
    result = _agy_env(tmp_path, "synthetic-token-content")
    assert result.returncode == 0
    assert "not verified" in result.stdout


def test_au_r3_agy_sh_check_empty_token_file_is_still_not_logged_in(tmp_path):
    result = _agy_env(tmp_path, "")
    assert result.returncode == 10
    assert "not verified" not in result.stdout
