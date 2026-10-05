"""M5 live checks with persisted provider config and a write-before-park agent."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.clock import ALL_DAYS, ClockWorld, local

WIN = {"days": ALL_DAYS, "ranges": ["09:00-17:00"]}
NOON = local(2026, 10, 5, 12)
CLOSE = local(2026, 10, 5, 17)
OPEN = local(2026, 10, 6, 9)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=NOON)
    yield world
    world.close()


def test_nc_r40_provider_slot_is_released_and_readmission_waits_for_capacity(w):
    provider = w.provider("pcfx", max_concurrent=1)
    w.agent("pcworker", "pcfx")
    w.start_scheduler()
    a = w.simple("A", "pcworker", window=WIN, fx={"hang": True})
    w.wait_running(a)
    original = w.until(lambda: provider.by_tag("A"))[-1]
    b = w.simple("B", "pcworker", fx={"gate": "b"})
    w.until(lambda: "admission:provider_concurrency" in w.codes(b))
    w.set_clock(CLOSE + timedelta(seconds=10))
    w.wait_state(a, "suspended")
    w.wait_running(b)
    w.set_clock(OPEN)
    w.until(lambda: "admission:provider_concurrency" in w.codes(a))
    assert w.get(a)["state"] == "suspended"
    assert not [c for c in provider.by_tag("A") if c["resume"]]
    provider.open_gate("b")
    resumed = w.until(lambda: [c for c in provider.by_tag("A") if c["resume"]])[-1]
    assert resumed["resume"] == original["session"]
    w.until(lambda: w.transitions(a).count("resumed") == 1)


def test_nc_r62_dirty_work_is_resumed_in_place_after_scheduler_restart(w):
    w.start_scheduler()
    node = w.simple("WRITER", window=WIN, fx={"write": {"partial.txt": "interrupted"}, "gate_after": "finish"})
    first = w.wait_spawn("WRITER")
    path = Path(first["cwd"]) / "partial.txt"
    w.until(path.exists)
    assert path.read_text() == "interrupted"
    w.set_clock(CLOSE)
    w.wait_state(node, "suspended")
    # This leftover was never part of the result or input; reseating would
    # discard it or hold dirty_worktree. Resumption must preserve it as-is.
    leftover = Path(first["cwd"]) / "untracked.txt"
    leftover.write_text("still working")
    w.restart_scheduler()
    assert w.get(node)["state"] == "suspended"
    w.set_clock(OPEN)
    second = w.until(lambda: [c for c in w.fx.by_tag("WRITER") if c["resume"]])[-1]
    assert second["resume"] == first["session"] and second["cwd"] == first["cwd"]
    assert leftover.read_text() == "still working"
    w.until(lambda: w.transitions(node).count("resumed") == 1)
    w.gate("finish")
    assert w.wait_state(node, "done")["outcome"] == "completed"


def test_nc_r20_operator_stop_while_scheduler_is_down_survives_restart(w):
    from nc_fixture.world import call_tool
    w.start_scheduler()
    node = w.simple("STOP", window=WIN, fx={"hang": True})
    w.wait_running(node)
    run_id = w.get(node)["active_run"]
    w.set_clock(CLOSE)
    w.wait_state(node, "suspended")
    w.stop_scheduler()
    call_tool(w, "stop_agent", run_id)
    w.start_scheduler()
    assert w.wait_state(node, "held")["hold"]["reason"] == "stopped_by_orchestrator"
    w.set_clock(OPEN)
    assert w.get(node)["state"] == "held"
    assert not [c for c in w.fx.by_tag("STOP") if c["resume"]]
