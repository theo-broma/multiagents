"""Regression tests for the round-5 final-review findings on the SF
follow-ups (`context/specs/sc-pc-followups.md`), against commit b61a350.

The three share one rule: unknown is never death, and no failure path may
leave state without an owner that will retry it. `tests/test_sf_followups.py`
is untouched.
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
    runner = w.runner
    real = runner.stop

    async def stop(agent_id, *, internal=False):
        out = await real(agent_id, internal=internal)
        if internal:
            await after()
        return out
    monkeypatch.setattr(runner, "stop", stop)


def lock_held(runner, aid):
    """Can a second fd not acquire the node's supervision lock?"""
    lock = runner.paths.run_dir(aid) / SUPERVISOR_LOCK
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False


def predecessor_hold(owner="someone-else"):
    return {"since": time.time(), "owner_pid": os.getpid(), "owner_start": "",
            "owner": owner, "pid": None, "pid_start": "", "occupancy": "",
            "executor": {"kind": "local", "container": ""}, "then": None}


# ------------------------------------------------------------- finding 1 ----

def test_finding_1_an_uncaptured_predecessor_keeps_the_lock_with_an_owner(w, monkeypatch):
    """P2 runner.py:7164: a failed predecessor capture is unknown, never
    confirmed absence — the inherited lock must not be released while the
    pid is live, and the node must get an in-memory hold to own it."""
    g = steer_world(w)

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        w.runner.runs[aid].spec.executor = "bogus"   # get_executor raises
        with pytest.raises(ValueError):
            await w.server.steer_agent(aid, "steerhold: go")
        return aid, {
            "owned": aid in w.runner._holds,
            "lock_held": lock_held(w.runner, aid),
            "availability": w.runner.startup.availability("acme"),
            "slots": w.runner.provider_slots()["acme"]["in_use"],
        }
    aid, seen = asyncio.run(go())
    assert seen["owned"], "the failed capture left no owner"
    assert seen["lock_held"], "an uncaptured predecessor's lock was released"
    assert seen["availability"] is None, "the probe claim leaked"
    assert seen["slots"] == 1, seen


# ------------------------------------------------------------- finding 2 ----

def test_finding_2_a_failed_hold_read_still_gives_the_node_an_owner(w, monkeypatch):
    """P2 runner.py:6838: a failed hold read is not 'somebody else holds
    it' — an in-memory hold must be registered so the settle loop later
    releases the lock and settles the node, not leave it locked with no
    owner."""
    g = steer_world(w)
    state = {"fail_get": False}

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        entered = asyncio.Event()

        async def hang():
            # the storage failure lands only in the cleanup handler's read
            state["fail_get"] = True
            entered.set()
            await asyncio.sleep(60)
        on_predecessor_stop(w, monkeypatch, hang)
        real_get = w.runner.tree.get

        def flaky_get(agent_id):
            if state["fail_get"] and agent_id == aid:
                raise OSError(28, "No space left on device")
            return real_get(agent_id)
        monkeypatch.setattr(w.runner.tree, "get", flaky_get)
        first_pid = g.pids()[0]
        task = asyncio.ensure_future(w.server.steer_agent(aid, "steerhold: go"))
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        observed = {"owned": aid in w.runner._holds,
                    "lock_held": lock_held(w.runner, aid)}
        state["fail_get"] = False
        return aid, first_pid, observed
    aid, first_pid, seen = asyncio.run(go())
    runner = w.runner
    assert pc.wait_until(lambda: not pc.alive(first_pid), 20)
    assert seen["owned"], "the failed read left no owner"
    assert seen["lock_held"], "the lock was released on an unreadable hold"
    runner._settle_holds()                       # storage has recovered
    assert aid not in runner._locks, "the hold never released the lock"
    assert aid not in runner._holds
    assert not w.node(aid).cleanup_hold


# ------------------------------------------------------------- finding 3 ----

def test_finding_3_a_failed_restore_keeps_the_claimed_entry_and_reports_refusal(w, monkeypatch):
    """P2 runner.py:6833: a suppressed `restore_deferred` failure must not
    lose the claimed resume entry — it is kept for the settle loop, and
    `_pc_dispatch` reports the refusal instead of `launched`."""
    g = steer_world(w)
    state = {"fail_restore": True}

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
        real_restore = w.runner.tree.restore_deferred

        def flaky_restore(record):
            if state["fail_restore"]:
                raise OSError(28, "No space left on device")
            return real_restore(record)
        monkeypatch.setattr(w.runner.tree, "restore_deferred", flaky_restore)

        async def inject_predecessor_hold():
            w.tree().update(a, cleanup_hold=predecessor_hold())
        on_predecessor_stop(w, monkeypatch, inject_predecessor_hold)
        outcome, info = await w.runner._pc_dispatch(entry)
        return a, outcome, info, entry["id"]
    a, outcome, info, ident = asyncio.run(go())
    assert outcome != "launched", (outcome, info)
    assert outcome == "blocked", (outcome, info)
    assert ident in w.runner._steer_restores, "the claimed entry was lost"

    state["fail_restore"] = False
    w.runner._settle_holds()                    # storage has recovered
    assert any(d.get("id") == ident for d in w.deferred()), w.deferred()
    assert ident not in w.runner._steer_restores
