"""OC-D5/D6/D7: failed delivery releases admission and recovery finishes it."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from multiagents import driver


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    record = dict(request_id="request", session="session", mode="in_session",
                  reason="delivery regression", status="compacted",
                  return_prompt="resume the agents", snapshot={"tokens": 0})

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
        journal=journal, explicit=record.copy(), requested=None, snapshot=None,
        committed="nonce", cli=123, granter=Mock(), safe_point=1,
        disabled=False, spent=False,
    )
    monkeypatch.setattr(driver, "_limit_number", lambda *args: 1)
    monkeypatch.setattr(driver.compact_return, "message", lambda *args: "resume the agents")
    compact = Mock(return_value=0)
    monkeypatch.setattr(driver, "_compact_session", compact)
    action = Mock(return_value=(0, "", ""))
    monkeypatch.setattr(driver.scripts, "run_action", action)
    settle = Mock(return_value=True)
    monkeypatch.setattr(driver.safepoint, "settle", settle)
    monkeypatch.setattr(driver.safepoint, "servers", lambda *args: [])
    return compaction, record, action, compact, settle


@pytest.mark.parametrize("codes", [[1, 0], [124, 124, 124], [1, 1, 1]])
def test_oc_d7_delivery_retries_are_bounded_and_release_admission(delivery, codes):
    compaction, record, action, compact, settle = delivery
    action.side_effect = [(code, "", "delivery refused") for code in codes]
    compaction._deliver_live()
    assert action.call_count == len(codes)
    assert all(call.args[3] == "compact-deliver" for call in action.call_args_list)
    assert all(call.kwargs["extra_env"]["MULTIAGENTS_COMPACTION_REQUEST_ID"] == "request"
               for call in action.call_args_list)
    compact.assert_not_called()
    settle.assert_called_once_with(compaction.paths, "session", "nonce", "settled")
    compaction.granter.release.assert_called_once()
    assert compaction.committed is None
    assert record["delivery_attempts"] == len(codes)
    assert record["status"] == ("completed" if codes[-1] == 0 else "failed")
    if codes[-1]:
        assert record["failure_detail"] == "delivery refused"


def test_oc_d6_settlement_write_failure_retries_without_repeating_delivery(delivery):
    compaction, record, action, compact, settle = delivery
    settle.side_effect = [False, True]
    compaction._deliver_live()
    action.assert_called_once()
    compact.assert_not_called()
    assert settle.call_count == 2
    compaction.granter.release.assert_called_once()
    assert record["status"] == "completed"


def test_oc_d6_permanent_settlement_failure_ends_driver_and_records_failure(delivery):
    compaction, record, action, compact, settle = delivery
    settle.return_value = False
    # PS2-R4: settlement exhaustion now ends cleanly, without RuntimeError.
    try:
        compaction._deliver_live()
    except SystemExit as exit:
        assert exit.code != 0
    assert settle.call_count == 3
    action.assert_called_once()
    compact.assert_not_called()
    compaction.granter.release.assert_called_once()
    assert record["status"] == "failed"
    assert "admission settlement failed" in record["failure_detail"]


def test_oc_d7_delivery_exception_still_settles_and_records_failure(delivery):
    compaction, record, action, compact, settle = delivery
    action.side_effect = OSError("delivery unavailable")
    compaction._deliver_live()
    compaction.granter.release.assert_called_once()
    settle.assert_called_once()
    assert record["status"] == "failed"
    assert "delivery unavailable" in record["failure_detail"]
    assert action.call_count == 3


@pytest.mark.parametrize("phase", ["executing", "compacted"])
def test_oc_d5_in_session_recovery_delivers_return_prompt(delivery, phase):
    compaction, record, action, compact, settle = delivery
    record["status"] = phase
    assert compaction.recover() == ""
    assert compact.call_count == (1 if phase == "executing" else 0)
    action.assert_called_once()
    assert action.call_args.kwargs["extra_env"]["MULTIAGENTS_RESUME_PROMPT"].endswith("resume the agents")
    assert record["status"] == "completed"
    compaction.granter.release.assert_called_once()


def test_oc_d7_recovery_preserves_delivery_retry_budget(delivery):
    compaction, record, action, compact, settle = delivery
    record.update(delivery_attempts=2, delivery_failure="prior delivery failed")
    action.return_value = (1, "", "last delivery failed")
    compaction.recover()
    action.assert_called_once()
    compact.assert_not_called()
    assert record["delivery_attempts"] == 3
    assert record["status"] == "failed"
    assert record["failure_detail"] == "last delivery failed"


def test_oc_d7_recovery_of_delivered_prompt_only_settles(delivery):
    compaction, record, action, compact, settle = delivery
    record.update(delivery_attempts=1, delivery_done=True)
    compaction.recover()
    action.assert_not_called()
    compact.assert_not_called()
    settle.assert_called_once()
    compaction.granter.release.assert_called_once()
    assert record["status"] == "completed"


def test_oc_d6_acknowledgement_timeout_writes_one_settlement(delivery):
    compaction, record, action, compact, settle = delivery
    compaction.safe_point = 0
    # PS2-R4: settlement exhaustion now ends cleanly, without RuntimeError.
    try:
        compaction._deliver_live()
    except SystemExit as exit:
        assert exit.code != 0
    settle.assert_called_once()
    action.assert_called_once()
    compaction.granter.release.assert_called_once()
    assert record["status"] == "failed"
    assert record["failure_detail"] == "admission settlement failed"
