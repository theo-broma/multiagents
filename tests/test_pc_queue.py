"""PC-R3 / PC-R3a — admission on a full provider, and the queue.
Contract: `context/specs/provider-concurrency.md` (amendments included).

Assumptions (deliberately loose):
- "Queued": the start result is `deferred: true` with `provider_concurrency`
  in its reason, and `list_deferred` shows an entry for the task (its `agent`,
  `task`, `model`) whose reason also names `provider_concurrency`.
- A queued entry is launched by `wait_for_agents`; tests call it in a loop
  until the CLI has been spawned (bounded), never counting calls.
- Order is read from the order of CLI spawns (argv carries the task text).
- The "blocked head" test uses a model-level spend cap (SC) as the "other
  reason"; it needs SC's `spend_cap` to be enforced as well.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    for g in list(world.fakes.values()):
        if hasattr(g, "open"):
            g.open()
    world.down()


def acme_world(w, limit=1, fallbacks=None, **agent):
    g = pc.gated(w, "acme", max_concurrent=limit)
    extra = {"models": fallbacks} if fallbacks else {}
    w.agent("worker", "acme", "acme/m1", **extra, **agent)
    g.close()
    w.up()
    return g


async def pump(w, pred, rounds=15):
    for _ in range(rounds):
        if pred():
            return True
        await w.server.wait_for_agents(timeout=2)
    return pred()


def queued_entries(w):
    return [d for d in w.deferred() if "provider_concurrency" in str(d.get("reason", ""))]


# ----------------------------------------------------- fallback vs queue --

def test_r3_a_full_provider_sends_a_fresh_start_to_the_next_provider_in_the_route(w):
    a = acme_world(w, 1, fallbacks={"beta": "beta/b1"})
    b = pc.gated(w, "beta", max_concurrent=None)
    b.open()
    w.up()

    async def go():
        await w.started("worker", "first")
        await pc.await_until(lambda: a.spawns() == 1)
        r = await w.start("worker", "second")
        await pc.await_until(lambda: b.spawns() == 1)
        return r
    r = asyncio.run(go())
    assert r.get("agent_id") and r.get("provider") == "beta" and not r.get("deferred"), r
    assert a.spawns() == 1


def test_r3_when_no_provider_in_the_route_has_a_slot_the_start_is_queued_not_failed(w):
    a = acme_world(w, 1, fallbacks={"beta": "beta/b1"})
    b = pc.gated(w, "beta", max_concurrent=1)
    b.close()
    w.up()

    async def go():
        await w.started("worker", "first")
        await w.started("worker", "second")
        await pc.await_until(lambda: a.spawns() + b.spawns() == 2)
        r = await w.start("worker", "third")
        return r, w.deferred()
    r, entries = asyncio.run(go())
    assert pc.deferred_for_pc(r), r
    assert any("provider_concurrency" in str(e.get("reason")) and "third" in e.get("task", "")
               for e in entries), entries
    assert a.spawns() + b.spawns() == 2


def test_r3a_a_start_with_no_free_slot_queues_on_the_first_provider_of_its_route(w):
    a = acme_world(w, 1, fallbacks={"beta": "beta/b1"})
    b = pc.gated(w, "beta", max_concurrent=1)
    b.close()
    w.up()

    async def go():
        await w.started("worker", "one")
        await w.started("worker", "two")
        await pc.await_until(lambda: a.spawns() + b.spawns() == 2)
        await w.start("worker", "three")
        entries = [e for e in w.deferred() if "three" in e.get("task", "")]
        # free acme only: the queued entry must launch ON acme
        a.open()
        ok = await pump(w, lambda: a.spawns() == 2)
        return entries, ok, a.spawns(), b.spawns()
    entries, ok, na, nb = asyncio.run(go())
    assert entries and str(entries[0].get("model", "")).startswith("acme/"), entries
    assert ok and nb == 1, (na, nb)


# ------------------------------------------------------------- the queue --

def test_r3_the_queued_start_launches_once_the_first_run_finishes(w):
    g = acme_world(w, 1)

    async def go():
        a = await w.started("worker", "first")
        await pc.await_until(lambda: g.spawns() == 1)
        r = await w.start("worker", "second task text")
        assert pc.deferred_for_pc(r), r
        assert g.spawns() == 1
        g.open()
        await w.until(a)
        ok = await pump(w, lambda: g.spawns() == 2)
        await w.settle()
        return ok
    assert asyncio.run(go())
    assert "second task text" in g.argv_text(1)
    assert queued_entries(w) == []


def test_r3_a_queued_task_does_not_launch_while_the_holder_is_still_running(w):
    g = acme_world(w, 1)

    async def go():
        await w.started("worker", "first")
        await w.start("worker", "second")
        for _ in range(3):
            await w.server.wait_for_agents(timeout=1)
        return g.spawns()
    assert asyncio.run(go()) == 1


def test_r3_fifo_with_three_queued_starts(w):
    # markers that cannot occur in the shipped prompt (which says "read it first")
    first, qs = "zz-h0", ("zz-q1", "zz-q2", "zz-q3")
    g = acme_world(w, 1)
    g.hold(first)
    for t in qs:
        g.hold(t)

    async def go():
        await w.started("worker", first)
        await pc.await_until(lambda: g.spawns() == 1)
        for t in qs:
            assert pc.deferred_for_pc(await w.start("worker", t))
        order = []
        for tag, nxt in ((first, 2), (qs[0], 3), (qs[1], 4)):
            g.release(tag)
            assert await pump(w, lambda: g.spawns() == nxt), f"{tag} released, nothing launched"
            order.append(g.argv_text())
        g.release(qs[2])
        await w.settle()
        return order
    order = asyncio.run(go())
    assert qs[0] in order[0] and qs[1] in order[1] and qs[2] in order[2], order


def test_r3a_a_new_arrival_never_overtakes_an_eligible_queued_entry(w):
    g = acme_world(w, 1)
    g.hold("holder")
    g.hold("early")
    g.hold("late")

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        assert pc.deferred_for_pc(await w.start("worker", "early"))
        g.release("holder")
        await w.until(a)                       # the slot is free, nobody drained yet
        late = await w.start("worker", "late")
        await pump(w, lambda: g.spawns() >= 2)
        g.release("early")
        await pump(w, lambda: g.spawns() >= 3)
        g.release("late")
        await w.settle()
        return late
    asyncio.run(go())
    texts = [" ".join(c["argv"]) + " " + c.get("prompt", "") for c in g.calls()]
    assert "early" in texts[1], texts
    assert len(texts) < 3 or "late" in texts[2], texts


def test_r3a_the_queue_survives_a_server_restart_and_keeps_the_task(w):
    g = acme_world(w, 1)

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        r = await w.start("worker", "durable task text")
        assert pc.deferred_for_pc(r)
        return a
    a = asyncio.run(go())
    w.restart_server()
    assert any("durable task text" in e.get("task", "") for e in queued_entries(w)), w.deferred()

    async def after():
        g.open()
        await w.until(a)
        return await pump(w, lambda: g.spawns() == 2)
    assert asyncio.run(after())
    assert "durable task text" in g.argv_text(1)


def test_r3a_a_queued_start_keeps_its_model_pin(w):
    g = acme_world(w, 1)

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        r = await w.start("worker", "pinned job", model="acme/m2")
        assert pc.deferred_for_pc(r), r
        g.open()
        await w.until(a)
        return await pump(w, lambda: g.spawns() == 2)
    assert asyncio.run(go())
    assert "acme/m2" in g.argv_text(1), g.argv_text(1)


def test_r3_cancelling_a_queued_entry_removes_it_and_it_never_launches(w):
    g = acme_world(w, 1)

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        await w.start("worker", "to cancel")
        (entry,) = queued_entries(w)
        w.server.cancel_deferred(entry["id"])
        g.open()
        await w.until(a)
        for _ in range(3):
            await w.server.wait_for_agents(timeout=1)
        return g.spawns()
    assert asyncio.run(go()) == 1
    assert queued_entries(w) == []


def test_r3_queue_time_counts_against_no_watchdog_of_the_queued_run(w):
    g = acme_world(w, 1, timeout=4)
    g.hold("holder")

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        r = await w.start("worker", "waits longer than its timeout")
        await asyncio.sleep(6)               # longer than the 4s wall clock
        g.release("holder")
        await w.until(a)
        ok = await pump(w, lambda: g.spawns() == 2)
        g.open()
        await w.settle()
        await asyncio.sleep(0.5)
        return r, ok
    r, ok = asyncio.run(go())
    assert pc.deferred_for_pc(r), r
    assert ok
    statuses = [n.status for n in w.tree().read_nodes()] if hasattr(w.tree(), "read_nodes") \
        else [n["status"] for n in w.tree().read()["nodes"].values()]
    assert "failed" not in statuses and "timed_out" not in statuses and "limited" not in statuses, statuses


def test_r3a_a_head_blocked_for_another_reason_is_skipped_without_blocking_the_rest(w):
    g = pc.gated(w, "acme", max_concurrent=1, spend_cap={"models": {"acme/big": {"usd": 0}}})
    w.agent("big", "acme", "acme/big")
    w.agent("small", "acme", "acme/small")
    g.close()
    w.up()

    async def go():
        a = await w.started("small", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        await w.start("big", "blocked head")
        await w.start("small", "behind it")
        g.open()
        await w.until(a)
        ok = await pump(w, lambda: g.spawns() == 2)
        return ok
    assert asyncio.run(go()), "an entry behind a head blocked by another cause never launched"
    assert "behind it" in g.argv_text(1)


def test_r3_with_no_limit_configured_nothing_is_ever_queued(w):
    g = acme_world(w, None)

    async def go():
        rs = [await w.start("worker", f"t{i}") for i in range(5)]
        await pc.await_until(lambda: g.spawns() == 5)
        g.open()
        await w.settle()
        return rs
    rs = asyncio.run(go())
    assert not any(r.get("deferred") for r in rs)
    assert w.deferred() == []
