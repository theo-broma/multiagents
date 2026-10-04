"""NC-R16/R26/R57/R58/R61: launch ownership survives cancellation and shutdown."""
import asyncio
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_nc_m2_guard_recovery import local, deposit, claim, read, revoked
from multiagents.scheduler.engine import save_attempt
from multiagents.scheduler.store import issue_run_capability
from multiagents import procs


@pytest.mark.parametrize("tree_written", [False, True])
def test_nc_r26_cancellation_during_start_stops_the_handle_that_arrives_later(local, monkeypatch, tree_written):
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)

    async def scenario():
        entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        run = SimpleNamespace(id=attempt["run_id"], status="running")
        visible = []
        stopped = []

        async def start(*args, **kwargs):
            if tree_written:
                visible.append(run)
            entered.set()
            await release.wait()
            if not visible:
                visible.append(run)
            return {"agent_id": run.id}

        async def stop(run_id):
            stopped.append(run_id)
            run.status = "cancelled"
            done.set()
            return {"predecessor_death_confirmed": True}

        runner = SimpleNamespace(tree=SimpleNamespace(get=lambda _: visible[0] if visible else None,
                                                       active=lambda: []),
                                 start=start, stop=stop, shutdown=AsyncMock(),
                                 runs={run.id: SimpleNamespace(done=done)})
        monkeypatch.setattr("multiagents.scheduler.worker.Runner", lambda *args: runner)
        from multiagents.scheduler.worker import supervise
        worker = asyncio.create_task(supervise(world.paths.root, attempt["attempt_id"]))
        try:
            await entered.wait()
            reply = await asyncio.to_thread(engine.service.request, {
                "op": "cancel_node", "request_id": uuid.uuid4().hex, "token": world.root_token(),
                "args": {"id": node["id"], "revision": node["revision"]}})
            assert reply["ok"], reply
            assert reply["result"]["state"] == "held"
            _, journal = read(engine)
            assert journal[attempt["attempt_id"]]["cancel_requested"]
            release.set()
            await asyncio.wait_for(worker, timeout=5)
            assert stopped == [run.id]
            assert run.status == "cancelled"
            _, journal = read(engine)
            assert journal[attempt["attempt_id"]]["cancel_confirmed"]
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_nc_r57_r58_shutdown_preserves_an_in_progress_launch_and_its_capability(local, monkeypatch):
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    token = issue_run_capability(world.paths.root, attempt["run_id"], node["id"], {"read"})
    monkeypatch.setattr(engine, "tick", AsyncMock())

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        run = SimpleNamespace(id=attempt["run_id"], status="done")
        visible = []

        async def start(*args, **kwargs):
            entered.set()
            await release.wait()
            assert revoked(engine, token) == 0
            visible.append(run)
            return {"agent_id": run.id}

        runner = SimpleNamespace(tree=SimpleNamespace(get=lambda _: visible[0] if visible else None,
                                                       active=lambda: []),
                                 start=start, shutdown=AsyncMock(), runs={})
        monkeypatch.setattr("multiagents.scheduler.worker.Runner", lambda *args: runner)
        from multiagents.scheduler.worker import supervise
        worker = asyncio.create_task(supervise(world.paths.root, attempt["attempt_id"]))
        try:
            await entered.wait()
            engine.start()
            await asyncio.to_thread(engine.stop)
            _, journal = read(engine)
            assert journal[attempt["attempt_id"]]["state"] == "claimed"
            assert revoked(engine, token) == 0
            release.set()
            await asyncio.wait_for(worker, timeout=5)
            assert revoked(engine, token) == 1
            nodes, journal = read(engine)
            assert nodes[node["id"]]["state"] == "running"
            assert journal[attempt["attempt_id"]]["state"] == "launched"
        finally:
            release.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_nc_r16_first_evaluation_precedes_the_tick_sleep(local, monkeypatch):
    _, engine = local
    calls = []

    async def tick():
        calls.append("tick")
        engine.stopped.set()

    async def sleep(*args):
        pytest.fail("startup slept before the first evaluation")

    monkeypatch.setattr(engine, "tick", tick)
    monkeypatch.setattr(engine.runner, "shutdown", AsyncMock())
    monkeypatch.setattr("multiagents.scheduler.engine.asyncio.sleep", sleep)
    asyncio.run(engine.evaluate_forever())
    assert calls == ["tick"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_nc_r26_r61_missing_tree_attempt_resolves_only_after_confirmed_death(local, monkeypatch, confirmed):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    attempt = claim(engine, node)
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    monkeypatch.setattr(engine.runner, "_steer_predecessor", lambda _: object())
    probe = AsyncMock(return_value=confirmed)
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", probe)
    asyncio.run(engine.reconcile())
    assert probe.await_count == 0
    asyncio.run(engine.reconcile())
    assert probe.await_count == 1
    nodes, journal = read(engine)
    # Confirmed death lifts the recovery hold: a never-launched claim is
    # eligible again; only an unconfirmed one stays held.
    assert nodes[node["id"]]["state"] == ("open" if confirmed else "held")
    assert journal[attempt["attempt_id"]]["state"] == ("abandoned" if confirmed else "claimed")
    waiter = deposit(engine, locks=["schema"])
    nodes, journal = read(engine)
    assert bool(engine.lock_blockers(waiter, nodes, journal)) == (not confirmed)


def test_nc_r61_a_missing_launch_identity_is_unknown_even_after_two_reconciliations(local):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    attempt = claim(engine, node)
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    with engine.store.transaction() as db:
        attempt["launch_evidence"] = {"pid": None, "pid_start": "", "executor": {"kind": "local"}}
        save_attempt(db, attempt)
    asyncio.run(engine.reconcile())
    asyncio.run(engine.reconcile())
    _, journal = read(engine)
    assert journal[attempt["attempt_id"]]["state"] == "claimed"


def test_nc_r58_worker_launch_exception_revokes_a_capability_without_a_tree_record(local, monkeypatch):
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    token = issue_run_capability(world.paths.root, attempt["run_id"], node["id"], {"read"})

    async def failed_start(*args, **kwargs):
        raise OSError("launch recording failed")

    runner = SimpleNamespace(tree=SimpleNamespace(get=lambda _: None), start=failed_start)
    monkeypatch.setattr("multiagents.scheduler.worker.Runner", lambda *args: runner)
    from multiagents.scheduler.worker import supervise
    with pytest.raises(OSError, match="launch recording failed"):
        asyncio.run(supervise(world.paths.root, attempt["attempt_id"]))
    assert revoked(engine, token) == 1


def test_nc_r61_missing_tree_recovery_uses_the_recorded_wrapper_identity(local):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    attempt = claim(engine, node)
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    wrapper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        attempt["launch_evidence"] = {"pid": wrapper.pid, "pid_start": procs.start_time(wrapper.pid),
                                     "executor": {"kind": "local"}}
        with engine.store.transaction() as db:
            save_attempt(db, attempt)
        from multiagents.runner import _positively_ended
        predecessor = engine.missing_predecessor(attempt)
        assert not _positively_ended(predecessor.pid, predecessor.pid_start, predecessor.probe_raw)
        asyncio.run(engine.reconcile())
        wrapper.terminate()
        wrapper.wait(timeout=5)
        asyncio.run(engine.reconcile())
        _, journal = read(engine)
        assert journal[attempt["attempt_id"]]["state"] == "abandoned"
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)


def test_nc_r61_launch_identity_is_durable_before_the_tree_write(local, monkeypatch):
    _, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    runner = engine.runner
    from multiagents.runner import _launch_context
    context = _launch_context.set(engine.context(node, attempt))
    try:
        hold = runner._new_hold(attempt["run_id"], "fx", "token", SimpleNamespace(kind="local"))
        handle = SimpleNamespace(pid=os.getpid(), pid_start=procs.start_time(os.getpid()))

        def unwritable():
            raise OSError("tree unavailable")

        monkeypatch.setattr(runner.tree, "transaction", unwritable)
        with pytest.raises(OSError, match="tree unavailable"):
            runner._record_launched(attempt["run_id"], hold, handle)
        _, journal = read(engine)
        evidence = journal[attempt["attempt_id"]]["launch_evidence"]
        assert evidence["pid"] == handle.pid
        assert evidence["pid_start"] == handle.pid_start
        assert evidence["executor"]["kind"] == "local"
    finally:
        _launch_context.reset(context)
