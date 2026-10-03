"""Additional C11-R1a/R2a coverage for precedence and latest discovery."""
from dataclasses import replace

import pytest

from test_c11_session_lost import Advisor, LOST


@pytest.mark.parametrize("message,reason", [
    ("rate limit", "quota exhausted"),
    ("unauthorized: authentication required", "is not authenticated"),
])
def test_resume_mismatch_keeps_auth_or_quota_reason(tmp_path, monkeypatch,
                                                   message, reason):
    adv = Advisor(tmp_path, monkeypatch)
    agent_id = adv.established()
    # No thread is announced, so the adapter explicitly emits a mismatch too.
    adv.fake.set(events=[{"type": "turn.failed", "error": {"message": message}}],
                 exit=1)
    adv.consult("second")
    node = adv.runner.check(agent_id)
    assert node["status"] == "failed"
    assert reason in node["reason"]
    assert "requested_session" not in node


def test_newer_ordinary_failure_does_not_revive_older_lost_conversation(
        tmp_path, monkeypatch):
    adv = Advisor(tmp_path, monkeypatch)
    old = adv.lost()
    lost = adv.runner.tree.get(old)
    newer = replace(lost, id="ag-100001", reason="ordinary crash",
                    requested_session="", created_at=lost.created_at + 1)
    adv.runner.tree.add(newer)
    assert adv.runner._find_conversation("advisor") is None
    assert adv.runner.check(old)["reason"] == LOST
