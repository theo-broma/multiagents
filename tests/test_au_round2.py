"""AU-R1/F130 and AU-R4/F140 regressions from the second AU review."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from multiagents.budget import _claude_token
from multiagents import redact

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c2_harness as h  # noqa: E402


@pytest.mark.parametrize("branch", ["profile", "vault"])
@pytest.mark.parametrize("expiry, code, reason", [
    (0, 10, "expired"),
    ("", 20, "unparseable"),
    (None, 20, "no expiry"),
])
def test_au_r1_falsy_expiry_is_never_reported_as_logged_in(
        tmp_path, branch, expiry, code, reason):
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {
        "MULTIAGENTS_EXECUTOR": "docker",
        "MULTIAGENTS_PRIVATE_BACKING": str(profile),
        "MULTIAGENTS_BIN": "true",
    }
    source = profile
    if branch == "vault":
        source = tmp_path / "vault"
        source.mkdir()
        env["MULTIAGENTS_PRIVATE_VAULT"] = str(source)
    (source / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "abc", "expiresAt": expiry},
    }))

    result = h.run_shipped_script("claude", "check", env)

    assert result.returncode == code, result.stdout + result.stderr
    assert reason in result.stdout.lower()
    assert "logged in" not in result.stdout.lower()
    assert "; authenticated" not in result.stdout.lower()
    if code == 20:
        assert "unknown" in result.stdout.lower()


@pytest.mark.parametrize("expiry", [
    0, "", None, "not-a-number", True, float("nan"), float("inf"),
    "9999999999999",
])
def test_au_r4_rejected_token_is_still_registered_for_redaction(
        tmp_path, monkeypatch, expiry):
    monkeypatch.setattr(redact, "_literals", set())
    # An opaque value avoids shape matching: only registration can mask it.
    token = "round2-opaque-" + "x" * 24
    leaked = f"leaked {token} here"
    assert redact.scrub(leaked) == leaked
    (tmp_path / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": token, "expiresAt": expiry},
    }))

    assert _claude_token(config_dir=tmp_path) is None
    assert redact.scrub(leaked) == f"leaked {redact.MASK} here"
