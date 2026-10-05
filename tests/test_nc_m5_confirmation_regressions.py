"""NC-R38 invalid windows and NC-R40's confirmed suspension boundary."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nc_fixture.m3_adv import Harness
from multiagents.scheduler import suspension, windows
from multiagents.scheduler.engine import save_attempt
from multiagents.scheduler_config import SchedulerConfigError, validate_setting


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


@pytest.mark.parametrize("helper", [windows.contains, windows.boundaries])
def test_nc_r38_missing_zone_never_uses_host_local_time(helper):
    spec = {"days": list(windows.DAYS), "ranges": ["00:00-24:00"]}
    with pytest.raises(ValueError, match="timezone"):
        if helper is windows.contains:
            helper(spec, None, 0)
        else:
            list(helper(spec, None, 0, 86400))


def test_nc_r38_invalid_default_zone_is_refused_during_evaluation():
    windows.prepare("Unknown/Default")
    spec = {"days": list(windows.DAYS), "ranges": ["00:00-24:00"]}
    with pytest.raises(ValueError, match="Unknown/Default"):
        windows.evaluate(json.dumps([spec]), "Unknown/Default", 0)


def test_nc_r38_zero_length_range_closes_the_entire_invalid_spec():
    windows.prepare("UTC")
    spec = {"days": list(windows.DAYS), "ranges": ["00:00-24:00", "10:00-10:00"]}
    assert not windows.contains(spec, windows.ZONES["UTC"], 0)
    result = windows.evaluate(json.dumps([spec]), "UTC", 0)
    assert not result["open"] and result["empty"]


@pytest.mark.parametrize("window", [
    {"timezone": "Unknown/Template", "days": ["mon"], "ranges": ["09:00-17:00"]},
    {"days": ["mon"], "ranges": ["10:00-10:00"]},
])
def test_nc_r38_templates_refuse_invalid_windows(h, window):
    definition = {"template": "invalid-window", "version": 1, "params": {},
                  "root": {"key": "job", "kind": "simple", "agent": "coder", "task": "work",
                           "window": window}}
    with h.service.store.transaction() as db:
        db.execute("INSERT INTO templates VALUES (?, ?)", ("invalid-window", json.dumps(definition)))
        token = h.service.store.meta(db, "root_token")
    reply = h.service.request({"op": "instantiate_template", "args": {"name": "invalid-window", "params": {}},
                               "token": token, "request_id": "invalid-window"})
    assert not reply["ok"] and reply["error"]["error"] == "invalid", reply
    assert h.nodes() == {}


def test_nc_r2_invalid_scheduler_timezone_is_a_config_error():
    with pytest.raises(SchedulerConfigError, match="scheduler.timezone"):
        validate_setting("timezone", "Unknown/Scheduler")


def suspended(h):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    h.engine.runner.tree.update(run.id, status="idle", reason="window suspended",
                                session_id="same-session", turn_started_at=10)
    attempt.update(window_stop=True, window_stop_started=True, turn_started_at=10)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    assert suspension.confirmed(h.service.store, run.id, h.engine.runner.tree.get(run.id))
    directory = h.world.paths.run_dir(run.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps({"status": "done", "turn_started_at": 10,
                                                     "session_id": "same-session"}))
    return node, attempt, h.engine.runner.tree.get(run.id)


def test_nc_r40_late_result_cannot_capture_a_confirmed_suspension(h):
    node, attempt, run = suspended(h)
    h.engine.finished(attempt, run)
    assert h.nodes()[node["id"]]["state"] == "suspended"
    assert h.nodes()[node["id"]]["generations"] == []
    current = h.journal()[attempt["attempt_id"]]
    assert current["state"] == "suspended" and "capture_intent" not in current


def test_nc_r40_runner_cannot_publish_a_late_result_after_confirmation(h, monkeypatch):
    node, attempt, run = suspended(h)
    runner = h.engine.runner
    released = []

    async def dead(_):
        return True

    monkeypatch.setattr(runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(runner, "_release", released.append)
    result = asyncio.run(runner.suspend(run.id))
    assert not result.get("completed"), result
    assert h.journal()[attempt["attempt_id"]]["state"] == "suspended"
    assert h.nodes()[node["id"]]["state"] == "suspended"
    assert runner.tree.get(run.id).status == "idle"
    assert runner.tree.get(run.id).reason == "window suspended"
    assert released == []
