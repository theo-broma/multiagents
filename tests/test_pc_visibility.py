"""PC-R4 — visibility: `budget_status`, `wait_for_agents.capacity`, events.
Contract: `context/specs/provider-concurrency.md`.

Field names are not fixed by the contract, so values are located loosely with
`sc_harness` helpers: a number about the provider on a path that says
`limit`/`max_concurrent`, agent ids anywhere under the provider's subtree,
the queue length as a number on a path that says `queue`/`queued`/`waiting`.
Events: `provider_concurrency`, recorded when a task is queued and when it is
released from the queue; the two are told apart by any field/value that says
`queued` vs `released`/`launched`/`started`/`dequeued`.
"""
from __future__ import annotations

import asyncio
import json
import sys
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


def build(w, limit=2):
    g = pc.gated(w, "acme", max_concurrent=limit)
    free = pc.gated(w, "free", max_concurrent=None)
    free.open()
    w.agent("worker", "acme", "acme/m1")
    w.agent("freeworker", "free", "free/m1")
    g.close()
    w.up()
    return g


def nums(status, scope, hint):
    return [v for path, v in sc.numbers_about(status, scope)
            if any(hint in p.lower() for p in path)]


async def pump(w, pred, rounds=15):
    for _ in range(rounds):
        if pred():
            return True
        await w.server.wait_for_agents(timeout=2)
    return pred()


def test_r4_budget_status_shows_limit_slot_holders_and_queue_length(w):
    g = build(w, 2)

    async def go():
        ids = [await w.started("worker", f"h{i}") for i in range(2)]
        await pc.await_until(lambda: g.spawns() == 2)
        await w.start("worker", "q1")
        await w.start("worker", "q2")
        await w.start("worker", "q3")
        return ids, w.budget()
    ids, status = asyncio.run(go())
    assert 2 in nums(status, "acme", "limit") + nums(status, "acme", "max_concurrent"), status
    for i in ids:
        assert any(i in s for sub in sc.scope_subtrees(status, "acme") for s in sc.strings(sub)), \
            f"holder {i} not shown under acme: {status}"
    assert 3 in nums(status, "acme", "queue") + nums(status, "acme", "queued") \
        + nums(status, "acme", "waiting"), status


def test_r4_slots_in_use_are_shown_as_a_count_too(w):
    g = build(w, 2)

    async def go():
        await w.started("worker", "h0")
        await pc.await_until(lambda: g.spawns() == 1)
        return w.budget()
    status = asyncio.run(go())
    assert 1 in nums(status, "acme", "use") + nums(status, "acme", "slots") + nums(status, "acme", "running"), status


def test_r4_an_unlimited_provider_shows_no_limit_and_no_queue(w):
    build(w, 1)
    status = w.budget()
    assert not nums(status, "free", "limit") and not nums(status, "free", "max_concurrent"), status


def test_r4_the_slot_list_and_queue_empty_out_when_work_finishes(w):
    g = build(w, 1)

    async def go():
        a = await w.started("worker", "h")
        await pc.await_until(lambda: g.spawns() == 1)
        await w.start("worker", "q")
        g.open()
        await w.until(a)
        await pump(w, lambda: g.spawns() == 2)
        await w.settle()
        return a, w.budget()
    a, status = asyncio.run(go())
    assert not any(a in s for sub in sc.scope_subtrees(status, "acme") for s in sc.strings(sub)), status
    assert not [v for v in nums(status, "acme", "queue") + nums(status, "acme", "queued") if v], status


def test_r4_wait_for_agents_capacity_names_the_full_providers(w):
    g = build(w, 1)

    async def go():
        a = await w.started("worker", "h")
        await pc.await_until(lambda: g.spawns() == 1)
        return await w.server.wait_for_agents([a], timeout=1)
    r = asyncio.run(go())
    cap = sc.find_key(r, "capacity")
    assert cap, r
    assert "acme" in " ".join(sc.strings(cap)), cap
    assert "free" not in " ".join(sc.strings(cap)).replace("free_slots", ""), cap


def test_r4_capacity_does_not_name_a_provider_with_a_free_slot(w):
    g = build(w, 2)

    async def go():
        a = await w.started("worker", "h")
        await pc.await_until(lambda: g.spawns() == 1)
        return await w.server.wait_for_agents([a], timeout=1)
    r = asyncio.run(go())
    cap = sc.find_key(r, "capacity")
    assert cap and "acme" not in " ".join(sc.strings(cap)), cap


def test_r4_an_event_is_recorded_when_a_task_is_queued_and_again_when_released(w):
    g = build(w, 1)

    async def go():
        a = await w.started("worker", "h")
        await pc.await_until(lambda: g.spawns() == 1)
        await w.start("worker", "q")
        queued = list(pc.pc_events(w))
        g.open()
        await w.until(a)
        await pump(w, lambda: g.spawns() == 2)
        await w.settle()
        return queued, pc.pc_events(w)
    queued, after = asyncio.run(go())
    assert len(queued) == 1, queued
    assert "acme" in json.dumps(queued[0]), queued[0]
    assert len(after) == 2, after
    assert after[0] != after[1]
    released = json.dumps(after[1]).lower()
    assert any(k in released for k in ("releas", "launch", "start", "dequeue")), after[1]


def test_r4_no_event_when_nothing_is_ever_queued(w):
    g = build(w, 3)

    async def go():
        for i in range(2):
            await w.start("worker", f"t{i}")
        await pc.await_until(lambda: g.spawns() == 2)
        g.open()
        await w.settle()
    asyncio.run(go())
    assert pc.pc_events(w) == []
