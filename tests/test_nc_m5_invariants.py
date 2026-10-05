"""Deterministic M5 replay, confirmation and admission invariants."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness
from multiagents.scheduler import sessions, suspension, windows
from multiagents.scheduler.engine import save_attempt


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def interrupted(h, *, verdict=False):
    if verdict:
        parent, children = h.tree("loop", 2)
        parent["loop"] = {"max_rounds": 3, "rounds_rejected": 0, "verdict_child": children[-1]["id"]}
        node = children[-1]
    else:
        parent, node = None, h.record(locks=["exclusive"])
    h.save(node)
    attempt, run = h.launch(node["id"])
    run.status = "running"
    run.session_id = "interrupted-session"
    h.engine.runner.tree.update(run.id, status="running", session_id=run.session_id)
    attempt.update(window_stop=True, locks=["exclusive"], lock_owners={"exclusive": node["id"]})
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    return h.nodes()[node["id"]], attempt, run, parent


class Stopper:
    def __init__(self, tree, result):
        self.tree, self.result = tree, result
        self.stops = 0

    async def suspend(self, run_id):
        self.stops += 1
        if self.result.get("predecessor_death_confirmed"):
            self.tree.update(run_id, status="idle", reason="window suspended")
        return self.result

    def _steer_predecessor(self, run_id):
        return object()

    async def _steer_predecessor_dead(self, predecessor):
        return True


def test_nc_r69_unknown_death_keeps_locks_and_does_not_settle_a_late_verdict(h):
    node, attempt, run, parent = interrupted(h, verdict=True)
    parent["pending_verdict"] = {"attempt_id": attempt["attempt_id"], "verdict": "rejected", "findings": []}
    h.save(parent)
    stopper = Stopper(h.engine.runner.tree, {"predecessor_death_confirmed": False})
    asyncio.run(suspension.command(h.service.store, stopper, attempt))
    stored = h.nodes()[node["id"]]
    assert stored["state"] == "held" and stored["hold"]["reason"] == "termination_unconfirmed"
    assert h.journal()[attempt["attempt_id"]]["state"] == "launched"
    outside = h.record(locks=["exclusive"])
    assert h.engine.lock_blockers(outside, h.nodes(), h.journal())
    h.engine.composites()
    assert h.nodes()[parent["id"]]["loop"]["rounds_rejected"] == 0
    assert h.nodes()[parent["id"]]["pending_verdict"]["verdict"] == "rejected"


def test_nc_r40_retry_after_unknown_death_suspends_only_the_recovery_hold(h):
    node, attempt, _, _ = interrupted(h)
    stopper = Stopper(h.engine.runner.tree, {"predecessor_death_confirmed": False})
    asyncio.run(suspension.command(h.service.store, stopper, attempt))
    stopper.result = {"predecessor_death_confirmed": True}
    asyncio.run(suspension.command(h.service.store, stopper, h.journal()[attempt["attempt_id"]]))
    assert h.nodes()[node["id"]]["state"] == "suspended"
    assert h.nodes()[node["id"]]["generations"] == []
    assert h.journal()[attempt["attempt_id"]]["state"] == "suspended"
    outside = h.record(locks=["exclusive"])
    assert h.engine.lock_blockers(outside, h.nodes(), h.journal()) == []


def test_nc_r69_replay_after_tree_stop_before_plan_commit_never_captures_a_result(h):
    node, attempt, run, _ = interrupted(h)
    attempt["window_stop_started"] = True
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    h.engine.runner.tree.update(run.id, status="idle", reason="window suspended")
    stopper = Stopper(h.engine.runner.tree, {"predecessor_death_confirmed": True})
    asyncio.run(suspension.command(h.service.store, stopper, attempt))
    assert stopper.stops == 1
    assert h.nodes()[node["id"]]["state"] == "suspended"
    assert h.nodes()[node["id"]]["generations"] == []
    assert "capture_intent" not in h.journal()[attempt["attempt_id"]]


def test_nc_r40_resumed_transition_replays_once_on_the_original_activation(h):
    node, attempt, run, _ = interrupted(h)
    node.update(state="suspended")
    h.save(node)
    attempt.pop("window_stop")
    attempt["window_resume"] = {"previous_turn": 0, "message": "resume work"}
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    suspension.resumed(h.service.store, run.id)
    suspension.resumed(h.service.store, run.id)
    assert h.nodes()[node["id"]]["state"] == "running"
    assert h.nodes()[node["id"]]["runs"][0]["attempt_id"] == attempt["attempt_id"]
    with h.service.store.transaction(write=False) as db:
        import json
        events = [json.loads(raw) for raw, in db.execute("SELECT record FROM notifications")]
    assert sum(e["kind"] == "resumed" for e in events) == 1


def test_nc_r40_cancel_of_a_confirmed_suspension_cancels_future_resume(h):
    node, attempt, _, _ = interrupted(h)
    node.update(state="suspended")
    attempt.update(state="suspended")
    h.save(node)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
        decided, stops = h.engine.request_cancel({node["id"]}, db)
    assert decided == {node["id"]: True} and stops == {}
    assert h.journal()[attempt["attempt_id"]]["state"] == "abandoned"


def test_nc_r40_suspension_reserves_the_alias_but_releases_the_execution_lock(h):
    parent, children = h.tree("group", 2)
    parent["template"] = {"instance": "windows"}
    for child in children:
        child["session"] = "writer"
    h.save(parent, *children)
    key = sessions.alias_id(children[0], h.nodes())
    journal = {"interrupted": {"state": "suspended", "node_id": children[0]["id"], "alias_id": key}}
    assert sessions.blockers(children[1], h.nodes(), journal, {}, h.engine.runner)[0]["code"] == "session_busy"
    assert h.engine.lock_blockers(children[1], h.nodes(), journal) == []


def test_nc_r38_gap_straddling_and_fold_windows_have_exact_next_boundaries():
    import json
    spec = {"timezone": "Europe/Paris", "days": ["sun"], "ranges": ["02:30-04:00"]}
    stamp = datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc).timestamp()
    windows.prepare("Europe/Paris", [spec])
    got = windows.evaluate(json.dumps([spec]), "Europe/Paris", stamp)
    assert got["next_open"] == "2026-03-29T01:00:00+00:00"
    spec["ranges"] = ["02:30-02:45"]
    stamp = datetime(2026, 10, 25, 0, 40, tzinfo=timezone.utc).timestamp()
    got = windows.evaluate(json.dumps([spec]), "Europe/Paris", stamp)
    assert got["open"] and got["next_close"] == "2026-10-25T00:45:00+00:00"
    assert got["next_open"] == "2026-10-25T01:30:00+00:00"


@pytest.mark.parametrize("confirmed", [False, True])
def test_nc_r40_runner_keeps_a_slot_until_the_confirmation_gate(h, monkeypatch, confirmed):
    from multiagents.runner import _occupies_slot
    _, _, run, _ = interrupted(h)
    runner = h.engine.runner
    released = []
    monkeypatch.setattr(runner, "_steer_predecessor", lambda id: SimpleNamespace(absent=False))

    async def stop(id, *, internal):
        assert internal
        assert runner.tree.get(id).status == "pending"
        assert _occupies_slot(runner.tree.get(id))

    async def death(predecessor):
        assert released == []
        assert _occupies_slot(runner.tree.get(run.id))
        return confirmed

    monkeypatch.setattr(runner, "stop", stop)
    monkeypatch.setattr(runner, "_steer_predecessor_dead", death)
    monkeypatch.setattr(runner, "_release", released.append)
    result = asyncio.run(runner.suspend(run.id))
    assert result["predecessor_death_confirmed"] is confirmed
    assert released == ([run.id] if confirmed else [])
    assert _occupies_slot(runner.tree.get(run.id)) is (not confirmed)


def test_nc_r69_spawn_evidence_on_resume_replay_prevents_a_second_live_invocation(h):
    node, attempt, run, _ = interrupted(h)
    node.update(state="suspended")
    h.save(node)
    attempt.pop("window_stop")
    attempt.update(window_resume={"previous_turn": 100, "previous_pid": 11, "message": "resume work"},
                   launch_evidence={"pid": 22, "pid_start": "new", "executor": {"kind": "local"}})
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    stopper = Stopper(h.engine.runner.tree, {"predecessor_death_confirmed": True})
    stopper.config = h.engine.runner.config
    asyncio.run(suspension.command(h.service.store, stopper, attempt))
    current = h.journal()[attempt["attempt_id"]]
    assert current["state"] == "launched" and current["window_stop"]
    assert h.nodes()[node["id"]]["hold"]["reason"] == "termination_unconfirmed"
    assert stopper.stops == 0
    asyncio.run(suspension.command(h.service.store, stopper, current))
    assert stopper.stops == 1
    assert h.nodes()[node["id"]]["state"] == "suspended"


def test_nc_r40_cancellation_before_resume_launch_finishes_without_a_new_invocation(h):
    node, attempt, run, _ = interrupted(h)
    node.update(state="held", hold={"reason": "termination_unconfirmed"})
    h.save(node)
    attempt.pop("window_stop")
    attempt.update(window_resume={"previous_turn": 100, "previous_pid": 11, "message": "resume work"},
                   cancel_requested=True, cancel_kind="node", launch_in_progress=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    stopper = Stopper(h.engine.runner.tree, {"predecessor_death_confirmed": True})
    stopper.config = h.engine.runner.config
    asyncio.run(suspension.command(h.service.store, stopper, attempt))
    current = h.journal()[attempt["attempt_id"]]
    assert current["state"] == "abandoned" and current["cancel_confirmed"]
    assert not current.get("launch_in_progress")
    assert h.nodes()[node["id"]]["state"] == "cancelled" and stopper.stops == 0


@pytest.mark.parametrize("resuming", [False, True])
def test_nc_r69_final_launch_gate_reads_the_clock_after_admission(h, resuming):
    node, attempt, run, _ = interrupted(h)
    node.update(window={"timezone": "UTC", "days": ["mon"], "ranges": ["09:00-10:00"]},
                state="suspended" if resuming else "running")
    h.save(node)
    attempt.pop("window_stop")
    if resuming:
        attempt["window_resume"] = {"previous_turn": 0, "message": "resume work"}
    clock = h.world.root / "launch-clock"
    clock.write_text("2026-10-05T09:30:00Z")
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
        h.service.store.set_meta(db, "clock_file", str(clock))
    h.engine.runner._check_node_launch(run.id, window_resume=resuming)
    clock.write_text("2026-10-05T10:00:00Z")
    with pytest.raises(RuntimeError, match="window closed before launch"):
        h.engine.runner._check_node_launch(run.id, window_resume=resuming)
    assert h.journal()[attempt["attempt_id"]]["state"] == "launched"
