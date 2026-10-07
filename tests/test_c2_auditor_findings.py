"""Reproduction tests for C2 auditor findings."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from multiagents.budget import _claude_token


def _write_creds(tmp_path, **oauth):
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def test_au_r4_claude_token_is_none_when_expires_at_is_non_numeric(tmp_path):
    """AU-R4 (F140), inverted: expiresAt present but not a number means the
    expiry cannot be verified, so the token is unusable, as if expired."""
    _write_creds(tmp_path, accessToken="test-token-should-not-be-returned",
                 expiresAt="not-a-number")
    assert _claude_token(config_dir=tmp_path) is None


@pytest.mark.parametrize("bad", [
    "not-a-number",
    "9999999999999",                            # numeric string, even a far-future one
    "",
    True,
    False,
    None,
    [],
    {},
    [9999999999999],
])
def test_au_r4_claude_token_is_none_for_any_expires_at_that_is_not_a_number(tmp_path, bad):
    _write_creds(tmp_path, accessToken="tok-au-r4", expiresAt=bad)
    assert _claude_token(config_dir=tmp_path) is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_au_r4_claude_token_is_none_for_non_finite_expires_at(tmp_path, literal):
    (tmp_path / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "tok-au-r4", "expiresAt": %s}}' % literal)
    assert _claude_token(config_dir=tmp_path) is None


def test_au_r4_claude_token_is_none_when_expires_at_is_null(tmp_path):
    """AU-R4: null is a "null-like value" — present but not a finite number —
    so it is unusable (the old control asserted the token was returned)."""
    _write_creds(tmp_path, accessToken="null-expiry-token", expiresAt=None)
    assert _claude_token(config_dir=tmp_path) is None


def test_claude_token_returns_none_when_expires_at_is_numeric_and_expired(tmp_path):
    """Control: numeric past expiry correctly returns None."""
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "expired-token",
            "expiresAt": int((time.time() - 3600) * 1000),  # 1 hour ago
        }
    }))

    token = _claude_token(config_dir=tmp_path)
    assert token is None, "expired token should return None"


def test_claude_token_returns_token_when_expires_at_is_numeric_and_future(tmp_path):
    """Control: numeric future expiry correctly returns the token."""
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "valid-token",
            "expiresAt": int((time.time() + 3600) * 1000),  # 1 hour from now
        }
    }))

    token = _claude_token(config_dir=tmp_path)
    assert token == "valid-token", "valid token should be returned"


def test_claude_token_returns_token_when_no_expires_at(tmp_path):
    """Control: missing expiresAt returns the token (no expiry to check)."""
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "no-expiry-token",
        }
    }))

    token = _claude_token(config_dir=tmp_path)
    assert token == "no-expiry-token", "token without expiresAt should be returned"


def test_au_r4_a_usable_token_read_from_disk_is_registered_for_redaction(tmp_path):
    from multiagents.redact import scrub
    secret = "sk-ant-" + "api03-" + "x" * 24
    _write_creds(tmp_path, accessToken=secret,
                 expiresAt=int((time.time() + 3600) * 1000))
    assert _claude_token(config_dir=tmp_path) == secret
    assert secret not in scrub(f"leaked {secret} here")
