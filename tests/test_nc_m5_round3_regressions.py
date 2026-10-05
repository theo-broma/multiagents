"""Natural completion during window stops and configuration lock scope."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness
from multiagents.scheduler import rpc, suspension
from multiagents.scheduler.engine import attempts, save_attempt


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def completed(h):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    h.engine.runner.tree.update(run.id, status="idle", session_id="finished-session", turn_started_at=10)
    directory = h.world.paths.run_dir(run.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(json.dumps({"status": "done", "exit_code": 0,
                                                     "session_id": "finished-session", "turn_started_at": 10}))
    (directory / "exit_status").write_text("0\n")
    return node, attempt, h.engine.runner.tree.get(run.id)


def test_nc_r40_own_result_after_stop_intent_is_captured_and_advances_the_node(h):
    node, attempt, run = completed(h)
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    h.engine.finished(attempt, run)
    stored = h.nodes()[node["id"]]
    assert stored["state"] == "done" and stored["outcome"] == "completed"
    assert len(stored["generations"]) == 1
    assert h.journal()[attempt["attempt_id"]]["state"] == "recorded"
    assert not h.journal()[attempt["attempt_id"]].get("window_stop")


def test_nc_r40_result_written_during_stop_wins_over_later_death_confirmation(h, monkeypatch):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    runner = h.engine.runner
    runner.tree.update(run.id, status="running", session_id="finished-session", turn_started_at=10)

    async def stop(id, *, internal):
        directory = h.world.paths.run_dir(id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps({"status": "done", "exit_code": 0,
                                                         "session_id": "finished-session", "turn_started_at": 10}))
        (directory / "exit_status").write_text("0\n")
        runner.tree.set_status(id, "idle")

    async def dead(predecessor):
        return True

    monkeypatch.setattr(runner, "stop", stop)
    monkeypatch.setattr(runner, "_steer_predecessor", lambda id: SimpleNamespace(absent=False))
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    result = asyncio.run(runner.suspend(run.id))
    assert result.get("completed"), result
    assert h.journal()[attempt["attempt_id"]]["state"] == "launched"
    h.engine.finished(h.journal()[attempt["attempt_id"]], runner.tree.get(run.id))
    assert h.nodes()[node["id"]]["state"] == "done"


@pytest.mark.parametrize("op", ["create_node", "start_agent", "instantiate_template", "relaunch_node", "admit_run", "capture"])
def test_configuration_is_resolved_before_exclusive_transaction(h, monkeypatch, op):
    store = h.service.store
    args = {"kind": "simple", "agent": "coder", "task": "work", "plan_revision": 0}
    if op == "start_agent":
        args = {"agent": "coder", "task": "work"}
    elif op == "instantiate_template":
        definition = {"template": "plain", "version": 1, "params": {},
                      "root": {"key": "job", "kind": "simple", "agent": "coder", "task": "work"}}
        with store.transaction() as db:
            db.execute("INSERT INTO templates VALUES (?, ?)", ("plain", json.dumps(definition)))
        args = {"name": "plain", "params": {}}
    elif op == "relaunch_node":
        node = h.record()
        node.update(state="held", hold={"reason": "manual"})
        h.save(node)
        args = {"id": node["id"], "revision": node["revision"]}
    elif op in {"admit_run", "capture"}:
        node, attempt, run = completed(h)
        args = {"run_id": run.id}
    with store.transaction(write=False) as db:
        token = store.meta(db, "root_token")
    transaction, version = store.transaction, rpc.source_version
    depth = 0
    lookups = []

    @contextmanager
    def tracked(*, write=True):
        nonlocal depth
        with transaction(write=write) as db:
            depth += int(write)
            try:
                yield db
            finally:
                depth -= int(write)

    def source_version(paths):
        assert depth == 0, "configuration filesystem lookup inside exclusive transaction"
        lookups.append(paths)
        return version(paths)

    monkeypatch.setattr(store, "transaction", tracked)
    monkeypatch.setattr(rpc, "source_version", source_version)
    if op == "capture":
        h.engine.finished(attempt, run)
        assert h.nodes()[node["id"]]["state"] == "done"
    else:
        reply = h.service.request({"op": op, "args": args, "token": token, "request_id": "config-scope"})
        assert reply["ok"], reply
    assert lookups

@pytest.mark.parametrize("natural", [True, False])
def test_nc_r40_wrapper_records_whether_stop_actually_interrupted_the_agent(tmp_path, monkeypatch, natural):
    from multiagents import agentwrap
    import os
    import signal
    state = {"exited": False}
    handlers = {}
    monkeypatch.setattr(agentwrap.os, "getsid", lambda _: os.getpid())
    monkeypatch.setattr(agentwrap, "_pid_namespace", lambda: "test")
    monkeypatch.setattr(agentwrap, "prompt_stdin", lambda _: agentwrap.subprocess.DEVNULL)
    monkeypatch.setattr(agentwrap.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(pid=123, wait=lambda: 0))
    monkeypatch.setattr(agentwrap.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(agentwrap, "_exited", lambda _: state["exited"])

    def kill(_, sig):
        # Model a TERM handler returning zero; the code alone cannot prove a
        # natural completion. No process or wall-clock wait is needed.
        if sig == signal.SIGTERM:
            state["exited"] = True

    def poll(_):
        state["exited"] = natural
        handlers[signal.SIGTERM](None, None)

    monkeypatch.setattr(agentwrap, "_killpg", kill)
    monkeypatch.setattr(agentwrap.time, "sleep", poll)
    assert agentwrap.main([str(tmp_path), "0", str(tmp_path / "agent.pid"), "--", "fake"]) == 0
    evidence = json.loads((tmp_path / "exit_reason.json").read_text())
    assert evidence["cause"] == ("natural" if natural else "stop")
    assert evidence["exit_code"] == 0


@pytest.mark.parametrize("cause, expected", [("natural", "done"), ("stop", "running")])
def test_nc_r40_restart_uses_wrapper_cause_instead_of_stop_intent_or_zero_exit(h, cause, expected):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    runner = h.engine.runner
    runner.tree.update(run.id, status="idle", reason="window stopping", turn_started_at=10)
    directory = h.world.paths.run_dir(run.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit_status").write_text("0\n")
    (directory / "exit_reason.json").write_text(json.dumps({"cause": cause, "exit_code": 0, "started_at": 11}))
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    h.engine.finished(attempt, runner.tree.get(run.id))
    assert h.nodes()[node["id"]]["state"] == expected
    assert len(h.nodes()[node["id"]]["generations"]) == int(cause == "natural")


def test_nc_r40_previous_turn_result_does_not_turn_an_interruption_into_completion(h):
    node, attempt, run = completed(h)
    h.engine.runner.tree.update(run.id, status="idle", reason="window suspended", turn_started_at=20)
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    h.engine.finished(attempt, h.engine.runner.tree.get(run.id))
    assert h.nodes()[node["id"]]["generations"] == []
    assert "capture_intent" not in h.journal()[attempt["attempt_id"]]


def test_nc_r40_reconcile_captures_natural_completion_even_after_stop_started(h, monkeypatch):
    node, attempt, run = completed(h)
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)

    async def dead(_):
        return True

    monkeypatch.setattr(h.engine.runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
    monkeypatch.setattr(h.engine.runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(h.engine, "spawn", lambda _: pytest.fail("completed turn was sent back to suspension"))
    asyncio.run(h.engine.reconcile())
    assert h.nodes()[node["id"]]["state"] == "done"
    assert len(h.nodes()[node["id"]]["generations"]) == 1


def test_nc_r40_natural_exit_without_death_proof_keeps_the_window_stop_and_lock(h, monkeypatch):
    node, attempt, run = completed(h)
    attempt.update(window_stop=True, window_stop_started=True, locks=["occupied"])
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)

    async def dead(_):
        return False

    spawned = []
    monkeypatch.setattr(h.engine.runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
    monkeypatch.setattr(h.engine.runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(h.engine, "spawn", spawned.append)
    asyncio.run(h.engine.reconcile())
    assert spawned and h.nodes()[node["id"]]["state"] == "running"
    current = h.journal()[attempt["attempt_id"]]
    assert current["window_stop"] and current["locks"] == ["occupied"]
    assert "capture_intent" not in current


def test_nc_r40_stop_command_cannot_restore_intent_after_concurrent_capture(h, monkeypatch):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    attempt.update(window_stop=True)
    runner = h.engine.runner
    runner.tree.update(run.id, status="running")
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)

    def concurrent_completion(paths, run):
        with h.service.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            current.pop("window_stop")
            current.update(capture_intent={"id": run.id, "status": "done", "session_id": run.session_id})
            save_attempt(db, current)
        return None

    async def suspend(_):
        pytest.fail("stale stop command interrupted a captured turn")

    monkeypatch.setattr(suspension, "natural_result", concurrent_completion)
    monkeypatch.setattr(runner, "suspend", suspend)
    assert asyncio.run(suspension.command(h.service.store, runner, attempt))
    current = h.journal()[attempt["attempt_id"]]
    assert "window_stop_started" not in current and "capture_intent" in current


def test_nc_r40_journaled_natural_completion_survives_a_scheduler_restart(h, monkeypatch):
    node, attempt, run = completed(h)
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    assert suspension.completed(h.service.store, run.id, suspension.natural_result(h.world.paths, run))
    # After journaling, the supervisor can disappear before publishing the
    # terminal tree entry. Reconcile must replay the durable completion.
    h.engine.runner.tree.update(run.id, status="pending", reason="window stopping")
    monkeypatch.setattr(h.engine, "spawn", lambda _: pytest.fail("completed turn was relaunched"))
    asyncio.run(h.engine.reconcile())
    assert h.nodes()[node["id"]]["state"] == "done"
    assert h.engine.runner.tree.get(run.id).status == "done"
    assert len(h.nodes()[node["id"]]["generations"]) == 1
    asyncio.run(h.engine.reconcile())
    assert len(h.nodes()[node["id"]]["generations"]) == 1


def test_nc_r40_stale_run_snapshot_cannot_complete_a_newer_journaled_turn(h):
    node, attempt, old_run = completed(h)
    h.engine.runner.tree.update(old_run.id, status="pending", reason="window stopping", turn_started_at=20)
    # Reconcile has the fresh attempt but retained a run snapshot from before
    # resumption. The old result matches that snapshot, not this activation.
    attempt.update(window_stop=True, window_stop_started=True, window_resumed_at=30, turn_started_at=20)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    h.engine.finished(attempt, old_run)
    assert h.nodes()[node["id"]]["generations"] == []
    assert h.journal()[attempt["attempt_id"]]["window_stop"]


def test_nc_r40_dead_tree_idle_with_confirmed_stop_evidence_is_still_an_interruption(h, monkeypatch):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    runner = h.engine.runner
    runner.tree.update(run.id, status="idle", reason="window stopping", turn_started_at=10)
    directory = h.world.paths.run_dir(run.id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit_reason.json").write_text(json.dumps({"cause": "stop", "exit_code": 0, "started_at": 11}))
    attempt.update(window_stop=True, window_stop_started=True)
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)

    async def dead(_):
        return True

    async def stop(_, *, internal):
        assert internal

    monkeypatch.setattr(runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(runner, "stop", stop)
    result = asyncio.run(runner.suspend(run.id))
    assert not result.get("completed"), result
    assert h.journal()[attempt["attempt_id"]]["state"] == "suspended"
    assert h.nodes()[node["id"]]["generations"] == []


def test_nc_r40_completion_cas_refusal_preserves_the_new_turn_and_its_slot(h, monkeypatch):
    node, attempt, run = completed(h)
    runner = h.engine.runner
    natural = suspension.natural_result
    released = []

    def completion_then_resume(paths, old_run):
        result = natural(paths, old_run)
        with h.service.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            current.update(turn_started_at=20, window_resumed_at=30)
            save_attempt(db, current)
        runner.tree.update(run.id, status="running", reason="", turn_started_at=20)
        return result

    async def dead(_):
        return True

    monkeypatch.setattr(suspension, "natural_result", completion_then_resume)
    monkeypatch.setattr(runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(runner, "_release", released.append)
    result = asyncio.run(runner.suspend(run.id))
    assert not result.get("completed"), result
    assert runner.tree.get(run.id).status == "running"
    assert runner.tree.get(run.id).turn_started_at == 20
    assert released == []
