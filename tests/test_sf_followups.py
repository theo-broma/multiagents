"""SF-R1..R3: spend-cap and concurrency follow-ups. Contract:
`context/specs/sc-pc-followups.md` (including its revision section).

SF-R1/R2 are observed the way a user sees them: after a stop whose ledger
record never landed and a server restart, the recovered `spend_cap` event for
a crossing names every node that crossing stopped, whatever happened to the
node since. SF-R3 is observed on the provider's startup claim
(`runner.startup.availability`) and the provider's slot count
(`runner.provider_slots()`), in the same process.

The one seam used for SF-R3 is wrapping `runner.stop` so that something
happens while steer() is stopping its predecessor: that is where the contract
places the event, and there is no other way to land inside that window.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import spendcap  # noqa: E402

LONG = [{"cost": 0.0, "sleep": 0.3}] * 40


def day_cap(usd):
    return [spendcap.Cap("acme", "", usd, "day")]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    for g in list(world.fakes.values()):
        if hasattr(g, "open"):
            g.open()
    world.down()


def foreign(w, usd, caps=(), node="ag-other1", tag=""):
    return spendcap.Ledger(w.runner.ledger.path).charge(
        key=json.dumps(["foreign", node, usd, tag]), provider="acme", model="acme/m1",
        agent="other", node=node, usd=usd, caps=list(caps))


def never_recorded(w, monkeypatch):
    def never(ident, node):
        raise spendcap.LedgerError("No space left on device")
    monkeypatch.setattr(w.runner.ledger, "record_stop", never)


def capped_world(w, monkeypatch, usd=10.0):
    acme = w.provider("acme", spend_cap={"usd": usd})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    monkeypatch.setattr(w.runner, "SPEND_CAP_POLL_SECONDS", 0.2)
    acme.script(steps=LONG)
    never_recorded(w, monkeypatch)
    return acme


def recovered(w, monkeypatch, ident):
    """After a restart: the recovered event(s) of one crossing."""
    w.restart_server()
    fresh = w.runner
    monkeypatch.setattr(fresh, "ANNOUNCE_GRACE_SECONDS", 0.0)
    return fresh


def events_of(w, ident):
    return [e for e in w.p.event_records() if e.get("crossing_id") == ident]


# --------------------------------------------------------------- SF-R1 ----

def test_sf_r1_a_stop_is_recorded_under_the_crossing_that_caused_it_not_the_current_cap(w, monkeypatch):
    capped_world(w, monkeypatch, usd=10.0)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        new, _ = foreign(w, 1.5, caps=day_cap(1.0))     # crossed at $1 ...
        w.p.cap("acme", {"usd": 0.5})                    # ... lowered before the sibling polls
        w.reload()
        states = await w.until(aid, timeout=15)
        await w.settle()
        return aid, states[aid], new[0]["id"]
    aid, state, ident = asyncio.run(go())
    assert state == "limited"
    fresh = recovered(w, monkeypatch, ident)
    fresh._announce_pending()
    found = events_of(w, ident)
    assert len(found) == 1 and found[0].get("recovered") is True, found
    assert aid in found[0]["agents"], found[0]


def test_sf_r1_the_node_carries_the_id_of_the_crossing_that_stopped_it(w, monkeypatch):
    capped_world(w, monkeypatch, usd=10.0)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        new, _ = foreign(w, 1.5, caps=day_cap(1.0))
        w.p.cap("acme", {"usd": 0.5})
        w.reload()
        await w.until(aid, timeout=15)
        await w.settle()
        return aid, new[0]["id"]
    aid, ident = asyncio.run(go())
    assert ident in w.node(aid).spend_cap_crossings, w.node(aid).spend_cap_crossings


# --------------------------------------------------------------- SF-R2 ----

def stopped_with_lost_record(w, monkeypatch):
    capped_world(w, monkeypatch)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        new, _ = foreign(w, 1.5, caps=day_cap(1.0))
        await w.until(aid, timeout=15)
        await w.settle()
        return aid, new[0]["id"]
    aid, ident = asyncio.run(go())
    assert w.status(aid) == "limited"
    return aid, ident


def test_sf_r2_a_node_cancelled_after_the_restart_is_still_named(w, monkeypatch):
    aid, ident = stopped_with_lost_record(w, monkeypatch)
    fresh = recovered(w, monkeypatch, ident)

    async def go():
        await w.server.stop_agent(aid)
        await w.settle()
    asyncio.run(go())
    assert w.status(aid) == "cancelled"
    fresh._announce_pending()
    found = events_of(w, ident)
    assert len(found) == 1 and aid in found[0]["agents"], found


def test_sf_r2_a_node_resumed_after_a_cap_raise_is_still_named(w, monkeypatch):
    aid, ident = stopped_with_lost_record(w, monkeypatch)
    fresh = recovered(w, monkeypatch, ident)
    w.p.cap("acme", {"usd": 100.0})
    w.reload()
    w.fakes["acme"].script(steps=[], text="resumed")

    async def go():
        r = await w.server.steer_agent(aid, "carry on")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert w.status(aid) not in ("limited",), (r, w.status(aid))
    fresh._announce_pending()
    found = events_of(w, ident)
    assert len(found) == 1 and aid in found[0]["agents"], found


def test_sf_r2_a_node_that_finished_after_the_restart_is_still_named(w, monkeypatch):
    aid, ident = stopped_with_lost_record(w, monkeypatch)
    fresh = recovered(w, monkeypatch, ident)
    w.p.cap("acme", {"usd": 100.0})
    w.reload()
    w.fakes["acme"].script(steps=[], text="all done")

    async def go():
        await w.server.steer_agent(aid, "finish up")
        await w.until(aid, timeout=20, states=("done", "idle", "failed", "cancelled"))
        await w.settle()
    asyncio.run(go())
    fresh._announce_pending()
    found = events_of(w, ident)
    assert len(found) == 1 and aid in found[0]["agents"], found


# ------------------------------------------- SF-R1/R2 revision: union ----

def test_sf_r1_r2_evidence_accumulates_across_stop_resume_stop(w, monkeypatch):
    capped_world(w, monkeypatch)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        x, _ = foreign(w, 1.5, caps=day_cap(1.0), tag="x")      # stopped by X
        await w.until(aid, timeout=15)
        await w.settle()
        assert w.status(aid) == "limited"
        w.p.cap("acme", {"usd": 100.0})
        w.reload()
        w.fakes["acme"].script(steps=LONG)
        r = await w.server.steer_agent(aid, "resume")           # resumed
        assert r.get("steered") is True, r
        await asyncio.sleep(0.5)
        y, _ = foreign(w, 1.0, caps=day_cap(2.0), tag="y")      # stopped by Y
        assert y, "the second crossing was not claimed"
        await w.until(aid, timeout=20, states=("limited",))
        await w.settle()
        return aid, x[0]["id"], y[0]["id"]
    aid, x, y = asyncio.run(go())
    assert x != y
    assert {x, y} <= set(w.node(aid).spend_cap_crossings), w.node(aid).spend_cap_crossings
    fresh = recovered(w, monkeypatch, x)
    fresh._announce_pending()
    for ident in (x, y):
        found = events_of(w, ident)
        assert len(found) == 1 and aid in found[0]["agents"], (ident, found)


# --------------------------------------------------------------- SF-R3 ----

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


def assert_steer_released_its_own(w, g, aid, first_pid):
    runner = w.runner
    assert g.spawns() == 1, "a replacement was spawned"
    assert runner.startup.availability("acme") is None, (
        "the refused steer left the half-open provider's only probe claimed: "
        f"{runner.startup.availability('acme')}")

    async def drained():
        assert await pc.await_until(lambda: not pc.alive(first_pid), 20), \
            "the predecessor was left running"
        # no phantom slot: the provider's count equals its live runs (none)
        return await pc.await_until(
            lambda: runner.provider_slots()["acme"]["in_use"] == 0, 20)
    assert asyncio.run(drained()), (
        "a slot is still counted with no live run: " f"{runner.provider_slots()}")


def test_sf_r3_shutdown_during_steer_releases_the_probe_claim_and_leaves_no_phantom_slot(w, monkeypatch):
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
    assert r.get("steered") is not True, r
    assert_steer_released_its_own(w, g, aid, first_pid)


def test_sf_r3_a_cancelled_steer_releases_the_probe_claim_and_leaves_no_phantom_slot(w, monkeypatch):
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
    assert_steer_released_its_own(w, g, aid, first_pid)


def test_sf_r3_a_pre_spawn_refusal_releases_the_probe_claim_and_leaves_no_phantom_slot(w, monkeypatch):
    g = steer_world(w, spend_cap={"usd": 1.0})

    async def go():
        aid = await running_with_session(w, g)
        half_open(w.runner, "acme")

        async def crossing_lands():
            # a cap crossing lands after the check, before the respawn
            foreign(w, 1.5, caps=day_cap(1.0))
        on_predecessor_stop(w, monkeypatch, crossing_lands)
        first_pid = g.pids()[0]
        r = await w.server.steer_agent(aid, "steerhold: go")
        return aid, first_pid, r
    aid, first_pid, r = asyncio.run(go())
    assert r.get("steered") is not True, r
    assert_steer_released_its_own(w, g, aid, first_pid)


def test_sf_r3_a_refused_queued_resume_keeps_its_place_in_the_queue(w, monkeypatch):
    g = steer_world(w)
    g.hold("holder")

    async def go():
        a = await w.started("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        await pc.await_until(lambda: bool(w.node(a).session_id), 20)
        g.open()
        await w.until(a)                                   # finished: holds no slot
        g.close()
        g.hold("holder")
        await w.started("worker", "holder b")              # takes the only slot
        await pc.await_until(lambda: g.spawns() == 2)
        r = await w.server.steer_agent(a, "resume me")     # queued on its own provider
        assert r.get("queued") or r.get("deferred"), r
        queued_id = r["deferred_id"]

        async def shutdown_now():
            w.runner.__dict__["_pc_shutting_down"] = True  # what shutdown() sets first
        on_predecessor_stop(w, monkeypatch, shutdown_now)
        g.release("holder")                                # the drain now dispatches the resume
        await pc.await_until(lambda: not [
            d for d in w.deferred() if d.get("id") == queued_id
            and d.get("status") == "restarting"], 20)
        return queued_id
    queued_id = asyncio.run(go())
    entry = [d for d in w.deferred() if d.get("id") == queued_id]
    assert entry and entry[0].get("status", "waiting") == "waiting", entry
