"""Regression tests for the final-review findings on the SF follow-ups.

`context/specs/sc-pc-followups.md`; each test is named after the finding it
covers and reproduces the scenario that finding reported against commit
818e401. `tests/test_sf_followups.py` is untouched.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents.runner import SUPERVISOR_LOCK  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    for g in list(world.fakes.values()):
        if hasattr(g, "open"):
            g.open()
    world.down()


def half_open(runner, name):
    """`name` tripped startup_down; its cooldown has lapsed, so exactly one
    start may claim it as the probe."""
    startup = runner.startup
    with startup._lock():
        records = startup._read()
        records[name] = {"generation": "g0", "count": 3, "down": True,
                         "until": time.time() - 5, "probe": None, "runs": {}}
        startup._write(records)
    assert startup.availability(name) is None


def steer_world(w, **extra):
    g = pc.gated(w, "acme", max_concurrent=1, **extra)
    w.agent("worker", "acme", "acme/m1")
    g.close()
    w.up()
    return g


async def running_with_session(w, g):
    aid = await w.started("worker", "job")
    assert await pc.await_until(lambda: g.spawns() == 1)
    assert await pc.await_until(lambda: bool(w.node(aid).session_id), 20)
    return aid


def on_predecessor_stop(w, monkeypatch, after):
    """Run `after()` once steer()'s own stop of the predecessor has finished."""
    runner = w.runner
    real = runner.stop

    async def stop(agent_id, *, internal=False):
        out = await real(agent_id, internal=internal)
        if internal:
            await after()
        return out
    monkeypatch.setattr(runner, "stop", stop)


async def drained(runner, first_pid):
    assert await pc.await_until(lambda: not pc.alive(first_pid), 20), \
        "the predecessor was left running"
    return await pc.await_until(
        lambda: runner.provider_slots()["acme"]["in_use"] == 0, 20)


def assert_lock_free(runner, aid):
    assert aid not in runner._locks, "the supervision lock is still held"
    lock = runner.paths.run_dir(aid) / SUPERVISOR_LOCK
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ------------------------------------------------------------- finding 1 ----

def test_finding_1_a_non_runtime_error_before_spawn_releases_the_steers_own(w, monkeypatch):
    """P2 runner.py ~6979: `executor.start()` raising something that is not a
    RuntimeError (here the provider executable disappears before the spawn)
    must not leave a phantom PC reservation or a held supervision lock."""
    g = steer_world(w)

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        first_pid = g.pids()[0]
        Path(g.entry["bin"]).unlink()
        with pytest.raises(FileNotFoundError):
            await w.server.steer_agent(aid, "steerhold: go")
        return aid, first_pid
    aid, first_pid = asyncio.run(go())
    runner = w.runner
    assert g.spawns() == 1, "a replacement was spawned"
    assert runner.startup.availability("acme") is None, "the probe claim leaked"
    assert asyncio.run(drained(runner, first_pid)), runner.provider_slots()
    assert_lock_free(runner, aid)


# ------------------------------------------------------------- finding 2 ----

def test_finding_2_a_predecessors_hold_does_not_block_the_steers_own_release(w, monkeypatch):
    """P2 runner.py ~6988: a predecessor's unconfirmed cleanup hold (a
    different token) must survive, but the caller's PC reservation and its
    claimed queue entry must still be given back."""
    g = steer_world(w)

    async def go():
        a = await w.started("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        await pc.await_until(lambda: bool(w.node(a).session_id), 20)
        g.open()
        await w.until(a)                        # finished: holds no slot
        g.close()
        entry = w.tree().enqueue(
            "acme",
            {"op": "resume", "node_id": a, "agent": "worker",
             "session_id": w.node(a).session_id, "model": "acme/m1",
             "pinned": False, "effort": "", "message": "resume me",
             "task": "resume me"},
            "test", deferred_by="test", dispatcher=w.runner._owner_fields())

        async def inject_predecessor_hold():
            # An unconfirmed cleanup hold from an earlier launch of this
            # node, not this steer's own — a different token.
            w.tree().update(a, cleanup_hold={
                "since": time.time(), "owner_pid": os.getpid(),
                "owner_start": "", "owner": "someone-else", "pid": None,
                "pid_start": "", "occupancy": "",
                "executor": {"kind": "local", "container": ""}, "then": None})
        on_predecessor_stop(w, monkeypatch, inject_predecessor_hold)
        r = await w.runner.steer(a, "resume me", queued=entry)
        return a, r, entry["id"]
    a, r, ident = asyncio.run(go())
    assert r.get("steered") is not True, r
    deferred = [d for d in w.deferred() if d.get("id") == ident]
    assert deferred and deferred[0].get("status", "waiting") == "waiting", deferred
    assert w.node(a).cleanup_hold, "the predecessor's hold was cleared"
    assert w.status(a) != "pending", w.status(a)


# ------------------------------------------------------------- finding 3 ----

def test_finding_3_an_early_refusal_releases_the_inherited_supervision_lock(w, monkeypatch):
    """P2 runner.py ~6986: a shutdown refusal before `_launch`'s cleanup
    guard must release the supervision lock the predecessor's turn left for
    the relaunch, or another server can never adopt the node."""
    g = steer_world(w)
    box = {}

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")

        async def begin_shutdown():
            box["task"] = asyncio.ensure_future(w.runner.shutdown(detach=True))
            await asyncio.sleep(0)
        on_predecessor_stop(w, monkeypatch, begin_shutdown)
        first_pid = g.pids()[0]
        r = await w.server.steer_agent(aid, "steerhold: go")
        await box["task"]
        return aid, first_pid, r
    aid, first_pid, r = asyncio.run(go())
    runner = w.runner
    assert r.get("steered") is not True, r
    assert_lock_free(runner, aid)
    assert runner.startup.availability("acme") is None
    assert asyncio.run(drained(runner, first_pid)), runner.provider_slots()


# ------------------------------------------------------------- finding 4 ----

def test_finding_4_a_failed_neutral_release_is_retried_by_reconciliation(w, monkeypatch):
    """P2 runner.py ~3928: a neutral release whose state write failed needs
    an owner that retries it, staying neutral — the half-open probe goes
    free, the cooldown is neither re-armed nor cleared."""
    g = steer_world(w)
    state = {"failed": False}

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        real_release = w.runner.startup.release

        def flaky(provider, node_id, token):
            if not state["failed"]:
                state["failed"] = True
                return False
            return real_release(provider, node_id, token)
        monkeypatch.setattr(w.runner.startup, "release", flaky)

        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.sleep(60)
        on_predecessor_stop(w, monkeypatch, hang)
        task = asyncio.ensure_future(w.server.steer_agent(aid, "steerhold: go"))
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return aid
    asyncio.run(go())
    runner = w.runner
    blocked = runner.startup.availability("acme")
    assert blocked is not None and blocked.get("reason") == "startup_down", blocked
    runner._settle_holds()                       # the reconciliation path
    assert runner.startup.availability("acme") is None, "the release was not retried"
    with runner.startup._lock():
        record = runner.startup._read()["acme"]
    assert record["down"] is True and record["probe"] is None, record


# ------------------------------------------------------------- finding 5 ----

def test_finding_5_tree_evidence_is_retried_until_it_lands(w, monkeypatch):
    """P3 runner.py ~1880: a stop whose ledger write lands but whose tree
    evidence write failed must keep retrying the evidence, not discard the
    pair on the ledger's success."""
    w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()

    async def go():
        aid = await w.started("worker", "job")
        await w.until(aid, states=("done", "idle", "failed", "cancelled"))
        return aid
    aid = asyncio.run(go())
    runner = w.runner
    ident = "crossing-review-r1"
    runner._unrecorded_stops.add((ident, aid))
    real_update = runner.tree.update
    state = {"failed": False}

    def flaky(agent_id, **fields):
        if "spend_cap_crossings" in fields and not state["failed"]:
            state["failed"] = True
            raise OSError("transient tree write")
        return real_update(agent_id, **fields)
    monkeypatch.setattr(runner.tree, "update", flaky)

    runner._record_stops()
    assert runner.ledger.stops.get(ident) == [aid], "the ledger write did not land"
    assert (ident, aid) in runner._unrecorded_stops, "the pair was discarded early"
    assert ident not in w.node(aid).spend_cap_crossings

    runner._record_stops()
    assert ident in w.node(aid).spend_cap_crossings
    assert (ident, aid) not in runner._unrecorded_stops
