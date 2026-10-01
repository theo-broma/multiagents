"""PC-R3 (steer/resume/consult) and PC-R3b — steering keeps its own slot,
resumes of slot-less runs queue on their own provider, consults wait under one
deadline. Contract: `context/specs/provider-concurrency.md`.

Assumptions (deliberately loose):
- A steer or consult that is queued/refused is recognised by
  `provider_concurrency` (or the provider name, limit and holder ids) anywhere
  in the result; no field name is fixed. `steer_agent` answers `steered: true`
  only for a steer that took effect now.
- The consult error is the result's `error` text. It must name the provider,
  its limit (as a number) and every holder's agent id. "The caller holds a slot
  itself" is checked as a wording class (self/own/caller/you), the contract
  fixing no words.
- The caller of a consult is made an agent with `as_subagent` (its own id in
  the environment) so that "the caller holds a slot" is a real state.
"""
from __future__ import annotations

import asyncio
import re
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


def build(w, limit=1, fallbacks=None):
    g = pc.gated(w, "acme", max_concurrent=limit)
    w.agent("worker", "acme", "acme/m1", **({"models": fallbacks} if fallbacks else {}))
    w.agent("advisor", "acme", "acme/m1", conversational=True, can_spawn=False)
    g.close()
    w.up()
    return g


async def pump(w, pred, rounds=15):
    for _ in range(rounds):
        if pred():
            return True
        await w.server.wait_for_agents(timeout=2)
    return pred()


async def has_session(w, agent_id):
    ok = await pc.await_until(lambda: bool(w.node(agent_id).session_id), 20)
    assert ok, "the run never reported a session id"


def text_of(r):
    return " ".join(sc.strings(r))


# ---------------------------------------------------- steering a live run --

def test_r3b_steering_a_live_run_with_a_limit_of_one_never_waits_for_its_own_slot(w):
    g = build(w, 1)
    g.hold("job")
    g.hold("steerhold")

    async def go():
        a = await w.started("worker", "job one")
        await pc.await_until(lambda: g.spawns() == 1)
        await has_session(w, a)
        started = time.monotonic()
        r = await w.server.steer_agent(a, "steerhold: change course")
        ok = await pc.await_until(lambda: g.spawns() == 2, 20)
        return a, r, ok, time.monotonic() - started
    a, r, ok, took = asyncio.run(go())
    assert r.get("steered") is True and r.get("agent_id") == a, r
    assert ok and took < 20
    assert "provider_concurrency" not in text_of(r)
    assert not [d for d in w.deferred() if "provider_concurrency" in str(d.get("reason"))]
    assert pc.pc_events(w) == [], "a steer of a live run went through the queue"


def test_r3b_a_steered_run_holds_exactly_one_slot_across_the_handoff(w):
    g = build(w, 2)
    g.hold("job")
    g.hold("steerhold")
    g.hold("other")

    async def go():
        a = await w.started("worker", "job one")
        await pc.await_until(lambda: g.spawns() == 1)
        await has_session(w, a)
        await w.server.steer_agent(a, "steerhold: now")
        await pc.await_until(lambda: g.spawns() == 2)
        await asyncio.sleep(1.0)
        second = await w.start("worker", "other two")      # count 1 of 2: admitted
        await pc.await_until(lambda: g.spawns() == 3, 20)
        third = await w.start("worker", "third")           # count 2 of 2: queued
        return second, third
    second, third = asyncio.run(go())
    assert second.get("agent_id") and not second.get("deferred"), second
    assert pc.deferred_for_pc(third), third


def test_r3b_the_predecessor_process_ends_after_the_handoff(w):
    g = build(w, 1)
    g.hold("job")
    g.hold("steerhold")

    async def go():
        a = await w.started("worker", "job one")
        await pc.await_until(lambda: g.spawns() == 1)
        await has_session(w, a)
        first_pid = g.pids()[0]
        await w.server.steer_agent(a, "steerhold: go")
        await pc.await_until(lambda: g.spawns() == 2)
        return await pc.await_until(lambda: not pc.alive(first_pid), 20)
    assert asyncio.run(go()), "the predecessor was left running beside its replacement"


# ------------------------------------------- resuming a run with no slot --

def test_r3_resuming_a_finished_run_on_a_full_provider_queues_on_its_own_provider(w):
    b = pc.gated(w, "beta", max_concurrent=None)
    b.open()
    g = build(w, 1, fallbacks={"beta": "beta/b1"})
    w.up()

    async def go():
        g.open()
        a = await w.started("worker", "first job")
        await w.until(a)
        await w.settle()
        g.close()
        g.hold("holder")
        await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 2)
        before = g.spawns()
        r = await w.server.steer_agent(a, "resume please")
        queued = [d for d in w.deferred() if "provider_concurrency" in str(d.get("reason"))]
        return a, r, queued, before
    a, r, queued, before = asyncio.run(go())
    assert r.get("steered") is not True, r
    assert "provider_concurrency" in text_of(r) or queued, (r, queued)
    assert g.spawns() == before and b.spawns() == 0, "the resume was launched or moved"


def test_r3a_a_queued_resume_is_dispatched_as_a_resume_with_the_same_node_and_session(w):
    g = build(w, 1)

    async def go():
        g.open()
        a = await w.started("worker", "first job")
        await w.until(a)
        await w.settle()
        session = w.node(a).session_id
        g.close()
        g.hold("holder")
        holder = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 2)
        g.open()
        await w.server.steer_agent(a, "continue with the resume text")
        g.release("holder")
        await w.until(holder)
        ok = await pump(w, lambda: g.spawns() == 3)
        await w.settle()
        return a, session, ok
    a, session, ok = asyncio.run(go())
    assert ok, "the queued resume never launched"
    argv = g.calls()[-1]["argv"]
    assert session and session in argv, (session, argv)
    assert "continue with the resume text" in " ".join(argv)
    assert w.node(a).session_id == session
    assert not [n for n in w.tree().read()["nodes"].values()
                if "continue with the resume text" in str(n.get("task", ""))], \
        "a resume was dispatched as a fresh start"


# ---------------------------------------------------------------- consult --

def test_r3_a_consult_on_a_full_provider_times_out_naming_provider_limit_and_holders(w):
    g = build(w, 1)

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        started = time.monotonic()
        r = await w.server.consult("advisor", "question?", timeout=3)
        return a, r, time.monotonic() - started
    a, r, took = asyncio.run(go())
    err = str(r.get("error", ""))
    assert err, r
    assert "acme" in err and a in err, err
    assert re.search(r"\b1\b", err), f"the limit is not named: {err}"
    assert took < 15, f"the consult did not respect its deadline ({took:.0f}s)"
    assert g.spawns() == 1


def test_r3b_a_timed_out_consult_leaves_no_waiter_and_no_reservation(w):
    g = build(w, 1)
    g.hold("holder")

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        await w.server.consult("advisor", "question?", timeout=2)
        g.release("holder")
        await w.until(a)
        await w.settle()
        for _ in range(3):
            await w.server.wait_for_agents(timeout=1)
        await asyncio.sleep(1.0)
        spawned = g.spawns()
        g.open()
        after = await w.start("worker", "after")    # the slot must be free
        return spawned, after
    spawned, after = asyncio.run(go())
    assert spawned == 1, "the abandoned consult launched after its deadline"
    assert after.get("agent_id") and not after.get("deferred"), after


def test_r3b_a_consult_waits_for_a_slot_and_runs_when_one_frees_within_its_deadline(w):
    g = build(w, 1)
    g.hold("holder")

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)

        async def free_later():
            await asyncio.sleep(2)
            g.release("holder")
        t = asyncio.ensure_future(free_later())
        g.open()
        r = await w.server.consult("advisor", "question?", timeout=40)
        await t
        return r
    r = asyncio.run(go())
    assert not r.get("error"), r
    assert g.spawns() == 2


def test_r3b_consultations_are_not_exempt_from_the_count(w):
    g = build(w, 1)
    g.hold("ask")

    async def go():
        consult = asyncio.ensure_future(w.server.consult("advisor", "ask: slowly", timeout=60))
        await pc.await_until(lambda: g.spawns() == 1)
        r = await w.start("worker", "while the consult runs")
        g.release("ask")
        await consult
        return r
    r = asyncio.run(go())
    assert pc.deferred_for_pc(r), r


def test_r3_a_conversational_agent_between_turns_holds_no_slot(w):
    g = build(w, 1)

    async def go():
        g.open()
        await w.server.consult("advisor", "hello", timeout=30)
        g.close()
        return await w.start("worker", "advisor is idle")
    r = asyncio.run(go())
    assert r.get("agent_id") and not r.get("deferred"), r


def test_r3b_a_consult_by_a_caller_that_holds_the_only_slot_says_so(w, monkeypatch):
    g = build(w, 1)

    async def go():
        a = await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        sc.h.as_subagent(monkeypatch, agent_id=a, parent="", depth=1, can_spawn=True)
        r = await w.server.consult("advisor", "question?", timeout=3)
        return a, r
    a, r = asyncio.run(go())
    err = str(r.get("error", ""))
    assert "acme" in err and a in err, r
    assert re.search(r"itself|yourself|your own|own slot|the caller|caller", err, re.I), \
        f"the self-dependency is not called out: {err}"


def test_r3b_a_consult_by_an_unrelated_caller_does_not_claim_self_dependency(w):
    g = build(w, 1)

    async def go():
        await w.started("worker", "holder")
        await pc.await_until(lambda: g.spawns() == 1)
        return await w.server.consult("advisor", "question?", timeout=3)
    err = str(asyncio.run(go()).get("error", ""))
    assert err and not re.search(r"itself|yourself|your own|own slot|the caller", err, re.I), err
