"""OC-D6: each in-session request settles the surviving admission barrier once."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from multiagents import driver


@pytest.mark.parametrize("recovering", [False, True], ids=["live", "recovery"])
@pytest.mark.parametrize("code", [0, 1, 124], ids=["success", "failure", "timeout"])
def test_oc_d6_in_session_settles_once(tmp_path, monkeypatch, recovering, code):
    record = {
        "request_id": "request", "session": "session", "mode": "in_session",
        "reason": "test settlement", "status": "executing" if recovering else "pending",
        "snapshot": {"tokens": 0},
    }

    def update(request_id, status, **fields):
        assert request_id == record["request_id"]
        record.update(status=status, **fields)
        return record.copy()

    journal = Mock(directory=tmp_path)
    journal.status.side_effect = lambda *args: record.copy()
    journal.update.side_effect = update
    compaction = driver._AttachedCompaction.__new__(driver._AttachedCompaction)
    compaction.__dict__.update(
        paths=SimpleNamespace(root=tmp_path, config=tmp_path), config=Mock(),
        spec=SimpleNamespace(name="orchestrator", provider="fixture"),
        provider=Mock(), executor=Mock(), tree=Mock(),
        context={"MULTIAGENTS_ROLE": "orchestrator", "MULTIAGENTS_SESSION_ID": "session"},
        journal=journal, explicit=record.copy(), requested=0, snapshot=None,
        committed="nonce", cli=123, granter=Mock(), safe_point=1, grace=0,
        scheduled=("closed", 0, 0), disabled=False, spent=False,
        # The post-recovery poll must only check explicit-request settlement.
        threshold=0,
    )
    monkeypatch.setattr(compaction, "_state", lambda: "closed")
    monkeypatch.setattr(compaction, "_reach_safe_point", lambda state: True)
    monkeypatch.setattr(driver.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(driver, "_limit_number", lambda *args: 1)
    monkeypatch.setattr(driver.compact_return, "snapshot", lambda *args: {"tokens": 0})
    monkeypatch.setattr(driver.compact_return, "message", lambda *args: "return prompt")
    compact = Mock(return_value=code)
    monkeypatch.setattr(driver, "_compact_session", compact)
    delivery = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(driver.scripts, "run_action", delivery)
    settle = Mock(return_value=True)
    monkeypatch.setattr(driver.safepoint, "settle", settle)
    monkeypatch.setattr(driver.safepoint, "servers", lambda *args: [])

    if recovering:
        assert compaction.recover() == ""
        if code == 0:
            assert compaction.due() is False
    else:
        assert compaction.due() is False

    compact.assert_called_once()
    settle.assert_called_once_with(compaction.paths, "session", "nonce", "settled")
    compaction.granter.release.assert_called_once()
    assert record["status"] == ("completed" if code == 0 else "failed")
    assert delivery.call_count == (1 if code == 0 else 0)
    assert compaction.committed is None
