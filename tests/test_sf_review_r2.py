"""Regression tests for the round-2 final-review findings on the SF
follow-ups (`context/specs/sc-pc-followups.md`), against commit e4b9878.

Each test is named after the finding it covers and is red before its fix.
`tests/test_sf_followups.py` is untouched.
"""
from __future__ import annotations

import asyncio
import fcntl
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


def assert_lock_free(runner, aid):
    assert aid not in runner._locks, "the supervision lock is still held"
    lock = runner.paths.run_dir(aid) / SUPERVISOR_LOCK
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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


async def drained(runner, first_pid):
    assert await pc.await_until(lambda: not pc.alive(first_pid), 20), \
        "the predecessor was left running"
    return await pc.await_until(
        lambda: runner.provider_slots()["acme"]["in_use"] == 0, 20)


# ------------------------------------------------------------- finding 1 ----

def test_finding_1_a_live_predecessor_keeps_its_lock_and_is_not_marked_failed(w, monkeypatch):
    """P2 runner.py ~6706: a stop attempt that fails while the predecessor is
    still live must not have its inherited supervision lock released, and the
    refusal that follows must not record the live node as `failed`."""
    g = steer_world(w)
    box = {}

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        run = w.runner.runs[aid]

        async def no_stop():
            return None                     # the stop attempt fails
        monkeypatch.setattr(run.handle, "stop", no_stop)

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
    assert pc.alive(first_pid), "the predecessor was expected to stay live"
    assert aid in runner._locks, "a live predecessor's lock was released"
    assert w.status(aid) != "failed", w.status(aid)
    assert runner.startup.availability("acme") is None, "the probe claim leaked"


# ------------------------------------------------------------- finding 2 ----

def test_finding_2_a_cancellation_after_the_predecessors_death_releases_the_lock(w, monkeypatch):
    """P2 runner.py ~7022: cancellation landing after the predecessor died
    but before `stop()` returns must release the inherited lock once death
    is confirmed and no hold owns the node."""
    g = steer_world(w)

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.sleep(60)
        on_predecessor_stop(w, monkeypatch, hang)
        first_pid = g.pids()[0]
        task = asyncio.ensure_future(w.server.steer_agent(aid, "steerhold: go"))
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return aid, first_pid
    aid, first_pid = asyncio.run(go())
    runner = w.runner
    assert pc.wait_until(lambda: not pc.alive(first_pid), 20), \
        "the predecessor was left running"
    assert_lock_free(runner, aid)
    assert runner.startup.availability("acme") is None


# ------------------------------------------------------------- finding 3 ----

def test_finding_3_a_non_runtime_error_settles_the_node_before_it_propagates(w, monkeypatch):
    """P2 runner.py ~7065: an executor failure that is not a RuntimeError
    must settle the node like the refusal branch does before the exception
    propagates, not leave it reported as a live run."""
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
    assert asyncio.run(drained(runner, first_pid)), runner.provider_slots()
    assert w.status(aid) == "failed", w.status(aid)
    assert runner.startup.availability("acme") is None
    assert_lock_free(runner, aid)


# ------------------------------------------- round-3 review, finding 1 ----

def test_finding_1_launch_does_not_release_a_live_predecessors_inherited_lock(w, monkeypatch):
    """P2 runner.py ~3430: a failure inside `_launch` itself (the provider
    binary is gone) must not free the inherited supervision lock while the
    predecessor is still live; that decision belongs to `_steer_release`."""
    g = steer_world(w)

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        run = w.runner.runs[aid]

        async def no_stop():
            return None                     # the stop attempt fails
        monkeypatch.setattr(run.handle, "stop", no_stop)
        first_pid = g.pids()[0]
        Path(g.entry["bin"]).unlink()       # executor.start raises inside _launch
        with pytest.raises(FileNotFoundError):
            await w.server.steer_agent(aid, "steerhold: go")
        return aid, first_pid
    aid, first_pid = asyncio.run(go())
    runner = w.runner
    assert pc.alive(first_pid), "the predecessor was expected to stay live"
    assert aid in runner._locks, "_launch released a live predecessor's lock"
    assert w.status(aid) != "failed", w.status(aid)
    assert runner.startup.availability("acme") is None, "the probe claim leaked"


# ------------------------------------------- round-3 review, finding 2 ----

def test_finding_2_an_unconfirmed_death_is_owned_by_a_cleanup_hold(w, monkeypatch):
    """P2 runner.py ~6747: an unconfirmed predecessor (a dead pid whose
    probe answers unknown) must get the same cleanup hold `_launch` uses,
    re-checked by `_settle_holds` until death is confirmed, and then release
    the lock and settle the node — instead of holding the lock forever."""
    g = steer_world(w)
    state = {"answer": None}

    def unknown_probe():
        return state["answer"]

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        monkeypatch.setattr(w.runner, "_raw_alive_probe",
                            lambda executor, node_id: unknown_probe)
        first_pid = g.pids()[0]
        Path(g.entry["bin"]).unlink()
        with pytest.raises(FileNotFoundError):
            await w.server.steer_agent(aid, "steerhold: go")
        return aid, first_pid
    aid, first_pid = asyncio.run(go())
    runner = w.runner
    assert pc.wait_until(lambda: not pc.alive(first_pid), 20)
    assert w.node(aid).cleanup_hold, "no owner for the unconfirmed death"
    assert aid in runner._locks, "the lock was released before death was confirmed"
    assert w.status(aid) != "failed", w.status(aid)

    state["answer"] = False                 # death is now confirmable
    runner._settle_holds()
    assert aid not in runner._locks, "the unconfirmed predecessor was never re-checked"
    assert not w.node(aid).cleanup_hold
    assert w.status(aid) == "failed", w.status(aid)


# ------------------------------------------- round-4 review, finding 1 ----

def test_finding_1_a_failed_hold_write_still_leaves_an_owner(w, monkeypatch):
    """P2 runner.py ~6799: a storage error writing the cleanup_hold must not
    escape `_steer_release` or mask the original pre-spawn error; the
    in-memory hold still owns the node and `_settle_holds` retries the
    write, so the node is never left locked with no owner."""
    g = steer_world(w)
    state = {"answer": None, "allow_write": False}

    def unknown_probe():
        return state["answer"]

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        monkeypatch.setattr(w.runner, "_raw_alive_probe",
                            lambda executor, node_id: unknown_probe)
        real_update = w.runner.tree.update

        def flaky(agent_id, **fields):
            hold = fields.get("cleanup_hold")
            if (isinstance(hold, dict) and hold.get("pid")
                    and not state["allow_write"]):
                raise OSError(28, "No space left on device")
            return real_update(agent_id, **fields)
        monkeypatch.setattr(w.runner.tree, "update", flaky)
        first_pid = g.pids()[0]
        Path(g.entry["bin"]).unlink()
        with pytest.raises(FileNotFoundError):
            await w.server.steer_agent(aid, "steerhold: go")
        return aid, first_pid
    aid, first_pid = asyncio.run(go())
    runner = w.runner
    assert pc.wait_until(lambda: not pc.alive(first_pid), 20)
    assert aid in runner._holds, "the failed write left no owner"
    assert not runner._holds[aid].durable
    assert aid in runner._locks
    assert not w.node(aid).cleanup_hold       # the durable write failed
    assert w.status(aid) != "failed"

    state["allow_write"] = True
    state["answer"] = False                   # death is now confirmable
    runner._settle_holds()                    # retries the write, then ends
    assert not w.node(aid).cleanup_hold
    assert aid not in runner._locks
    assert w.status(aid) == "failed", w.status(aid)


# ------------------------------------------- round-4 review, finding 2 ----

def test_finding_2_a_failed_predecessor_capture_releases_the_steers_own(w, monkeypatch):
    """P2 runner.py ~7122: the predecessor capture runs after the startup
    claim, so its failure (an unsupported executor kind raising ValueError)
    must be inside the region that gives the claim, the PC reservation and a
    claimed queue entry back."""
    g = steer_world(w)

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")
        w.runner.runs[aid].spec.executor = "bogus"   # get_executor raises
        with pytest.raises(ValueError):
            await w.server.steer_agent(aid, "steerhold: go")
        # observed before the loop closes, while the predecessor is live
        return aid, {
            "availability": w.runner.startup.availability("acme"),
            "slots": w.runner.provider_slots()["acme"]["in_use"],
            "status": w.status(aid),
            "slot_owner": w.node(aid).slot_owner,
            "owned": aid in w.runner._holds,
            "lock_held": lock_held(w.runner, aid),
        }
    aid, seen = asyncio.run(go())
    assert seen["availability"] is None, "the probe claim leaked"
    assert seen["slots"] == 1, seen
    assert seen["slot_owner"] is None, seen
    assert seen["status"] != "failed", seen
    assert seen["owned"], "the failed capture left no owner"
    assert seen["lock_held"], "a live predecessor's lock was released"
