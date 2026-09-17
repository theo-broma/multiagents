"""Reproduction tests for C2 auditor findings."""

import json
import time
from pathlib import Path
from unittest.mock import patch

from multiagents.budget import _claude_token


def test_claude_token_returns_token_when_expires_at_is_non_numeric(tmp_path):
    """F140: _claude_token returns a token when expiresAt is present but not a number.

    The isinstance check on line 183 (isinstance(expires_ms, (int, float)))
    skips the expiry check entirely when expiresAt is a string, boolean, or
    other non-numeric type. The token is returned as valid even though we
    could not verify it hasn't expired.
    """
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "test-token-should-not-be-returned",
            "expiresAt": "not-a-number",
        }
    }))

    # With a non-numeric expiresAt, the isinstance check fails and the
    # expiry check is skipped — the token is returned as valid.
    token = _claude_token(config_dir=tmp_path)
    assert token == "test-token-should-not-be-returned", (
        "token returned when expiresAt is non-numeric — expiry check was bypassed"
    )


def test_claude_token_returns_none_when_expires_at_is_null(tmp_path):
    """Control: null expiresAt returns the token (same as missing)."""
    creds_file = tmp_path / ".credentials.json"
    creds_file.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "null-expiry-token",
            "expiresAt": None,
        }
    }))

    token = _claude_token(config_dir=tmp_path)
    assert token == "null-expiry-token", "token with null expiresAt should be returned"


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
