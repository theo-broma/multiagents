"""SC-R3 / SC-R3a / SC-R3b — admission, resume refusal, and the spawn guard.

Contract: `context/specs/spend-caps.md`, including the amendments, which
override the "resumed capped session is deferred" bullet of SC-R3: a session
stopped by a cap is refused on `steer_agent` with an error naming the binding
cap, the spend and the reset; it is never re-launched automatically.

Setup idiom: a run on the capped provider that costs exactly the cap puts the
provider *at* its cap (the stop that follows is SC-R4's business, and this
file only needs "the money is spent").

Assumptions where the contract is silent (each deliberately loose):
- "Deferred with the cause `spend_cap`": the start result has `deferred: true`
  and `spend_cap` appears in its reason; the queue entry (`list_deferred`)
  shows the same cause. The restart time is the result's `retry_after`
  (epoch seconds) and the queue's `retry_after` (ISO-8601).
- Refusals on steer carry an `error` (or `reason`) holding the cap, the spend
  and the reset time somewhere in their text/fields; times as epoch or ISO.
- "Not resumable" for a node with no session id is today's `steered: false`
  plus an error that talks about the session / resuming.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from sc_harness import WED, utc  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def acme_world(w, cap, models=("m1",), fallbacks=None, **prov):
    acme = w.provider("acme", spend_cap=cap, **prov)
    for i, model in enumerate(models):
        extra = {"models": fallbacks} if (fallbacks and i == 0) else {}
        w.agent("worker" if i == 0 else f"worker{i + 1}", "acme", f"acme/{model}", **extra)
    return acme


async def spend_it(w, acme, cost, agent="worker", task="spend"):
    """Run `agent` once at `cost`; the cap, if any, stops it. Returns its id."""
    acme.costs(cost)
    aid = await w.started(agent, task)
    await w.until(aid)
    await w.settle()
    return aid


def is_deferred_for_cap(result) -> bool:
    return bool(result.get("deferred")) and "spend_cap" in str(result.get("reason", ""))


# ------------------------------------------------------------- SC-R3: refusal --

@pytest.mark.parametrize("period", ["day", "week", "month"])
def test_r3_a_provider_at_its_cap_defers_a_fresh_start_until_the_period_ends(w, period):
    acme = acme_world(w, {"usd": 1.0, "period": period})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        spawned = acme.spawns()
        return spawned, await w.start("worker", "again")
    spawned, r = asyncio.run(go())
    assert is_deferred_for_cap(r), r
    end = sc.period_end(WED, period)
    assert abs(sc.as_epoch(r["retry_after"]) - end) <= 60, (r["retry_after"], end)
    assert acme.spawns() == spawned, "a refused start spawned the CLI"
    entry = [d for d in w.deferred() if d.get("id") == r.get("deferred_id")]
    assert entry and "spend_cap" in str(entry[0].get("reason")), w.deferred()
    assert abs(sc.as_epoch(entry[0]["retry_after"]) - end) <= 60


def test_r3_just_under_the_cap_is_admitted(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()

    async def go():
        await spend_it(w, acme, 0.99)
        r = await w.start("worker", "again")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert r.get("agent_id"), r


def test_r3_spend_exactly_equal_to_the_cap_is_refused(w):
    acme = acme_world(w, {"usd": 0.5})
    w.up()

    async def go():
        await spend_it(w, acme, 0.5)
        return await w.start("worker", "again")
    assert is_deferred_for_cap(asyncio.run(go()))


def test_r3_spend_counts_only_the_current_period(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        same_day = await w.start("worker", "same day")
        w.clock.set(utc(2026, 9, 17, 0, 0, 30))                  # the next UTC day
        r = await w.start("worker", "fresh day")
        await w.settle()
        return same_day, r
    same_day, r = asyncio.run(go())
    assert is_deferred_for_cap(same_day), f"the cap did not bind on the same day: {same_day}"
    assert r.get("agent_id"), f"yesterday's spend still counts: {r}"


def test_r3_refusal_falls_through_to_the_agents_fallback_provider(w):
    acme = acme_world(w, {"usd": 1.0}, fallbacks={"beta": "beta/b1"})
    w.provider("beta").costs(0.01)
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        spawned = acme.spawns()
        r = await w.start("worker", "next")
        await w.settle()
        return spawned, r
    spawned, r = asyncio.run(go())
    assert r.get("agent_id") and r.get("provider") == "beta", r
    assert acme.spawns() == spawned and w.spawns("beta") == 1


def test_r3_when_every_candidate_is_capped_the_start_is_deferred_not_failed(w):
    acme = acme_world(w, {"usd": 1.0}, fallbacks={"beta": "beta/b1"})
    beta = w.provider("beta", spend_cap={"usd": 1.0})
    w.agent("beta-worker", "beta", "beta/b1")
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        await spend_it(w, beta, 1.0, agent="beta-worker")
        return await w.start("worker", "next")
    r = asyncio.run(go())
    assert is_deferred_for_cap(r), r


# ------------------------------------------------------------ model caps (R3) --

def test_r3_a_capped_model_leaves_its_sibling_admitted_and_never_cools_the_provider(w):
    acme = acme_world(w, {"models": {"acme/big": {"usd": 1.0}}}, models=("big", "small"))
    w.up()

    async def go():
        await spend_it(w, acme, 1.0, agent="worker")
        big = await w.start("worker", "again")
        small = await w.start("worker2", "sibling")
        await w.settle()
        return big, small
    big, small = asyncio.run(go())
    assert is_deferred_for_cap(big), big
    assert small.get("agent_id") and small.get("provider") == "acme", small
    advice = " ".join(w.budget().get("advice", []))
    assert "cooling down" not in advice, advice


def test_r3_a_capped_model_falls_back_like_a_capped_provider(w):
    acme = acme_world(w, {"models": {"acme/big": {"usd": 1.0}}}, models=("big",),
                      fallbacks={"beta": "beta/b1"})
    w.provider("beta").costs(0.01)
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        r = await w.start("worker", "next")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert r.get("provider") == "beta", r


def test_r3_a_model_cap_is_applied_to_each_candidate_in_a_fallback_chain(w):
    """worker: beta (capped, spent) -> acme/big (model-capped, spent) -> gamma."""
    acme = w.provider("acme", spend_cap={"models": {"acme/big": {"usd": 1.0}}})
    beta = w.provider("beta", spend_cap={"usd": 1.0})
    w.provider("gamma").costs(0.01)
    w.agent("big-worker", "acme", "acme/big")
    w.agent("worker", "beta", "beta/b1", models={"acme": "acme/big", "gamma": "gamma/g1"})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0, agent="big-worker")
        await spend_it(w, beta, 1.0, agent="worker")
        r = await w.start("worker", "next")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert r.get("provider") == "gamma", r
    assert acme.spawns() == 1


def test_r3_the_restart_time_is_the_end_of_the_binding_caps_period(w):
    """Provider cap (day) is spent; the model's own week cap is not: the day binds."""
    acme = acme_world(w, {"usd": 1.0, "period": "day",
                          "models": {"acme/big": {"usd": 50.0, "period": "week"}}},
                      models=("small", "big"))
    w.up()

    async def go():
        await spend_it(w, acme, 1.0, agent="worker")                # on acme/small
        return await w.start("worker2", "big one")
    r = asyncio.run(go())
    assert is_deferred_for_cap(r), r
    assert abs(sc.as_epoch(r["retry_after"]) - sc.period_end(WED, "day")) <= 60


def test_r3_a_model_with_its_own_period_defers_to_that_periods_end(w):
    acme = acme_world(w, {"usd": 100, "period": "day",
                          "models": {"acme/big": {"usd": 1.0, "period": "week"}}},
                      models=("big",))
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        return await w.start("worker", "next")
    r = asyncio.run(go())
    assert is_deferred_for_cap(r), r
    assert abs(sc.as_epoch(r["retry_after"]) - sc.period_end(WED, "week")) <= 60


def test_r3_a_model_without_a_period_inherits_the_providers(w):
    acme = acme_world(w, {"usd": 100, "period": "month",
                          "models": {"acme/big": {"usd": 1.0}}}, models=("big",))
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        return await w.start("worker", "next")
    r = asyncio.run(go())
    assert abs(sc.as_epoch(r["retry_after"]) - sc.period_end(WED, "month")) <= 60, r


def test_r3_a_pinned_model_is_refused_and_never_moved_to_a_fallback(w):
    acme = acme_world(w, {"models": {"acme/big": {"usd": 1.0}}}, models=("big", "small"),
                      fallbacks={"beta": "beta/b1"})
    beta = w.provider("beta")
    beta.costs(0.01)
    w.up()

    async def go():
        await spend_it(w, acme, 1.0, agent="worker")
        return await w.start("worker", "pinned", model="acme/big")
    r = asyncio.run(go())
    assert not r.get("agent_id"), r
    assert "spend_cap" in str(r), r
    assert beta.spawns() == 0


# ---------------------------------------------- raising or removing the cap --

@pytest.mark.parametrize("edit", [{"usd": 50.0}, {"usd": None}], ids=["raised", "removed"])
def test_r3_a_deferred_start_is_restarted_by_the_next_wait_once_the_cap_allows_it(w, edit):
    acme = acme_world(w, {"usd": 1.0})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        r = await w.start("worker", "later")
        assert is_deferred_for_cap(r), r
        before = acme.spawns()
        await w.server.wait_for_agents(timeout=2)           # nothing changed: still deferred
        assert acme.spawns() == before, "restarted while the cap still binds"
        w.p.cap("acme", edit)
        acme.costs(0.01)
        await w.server.wait_for_agents(timeout=10)          # long before the stored time
        await w.settle()
        return before
    before = asyncio.run(go())
    assert acme.spawns() == before + 1, "the raised/removed cap did not release the deferral"
    assert not [d for d in w.deferred() if d.get("status", "waiting") == "waiting"]


def test_r3_a_deferred_start_runs_when_the_period_rolls_over(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        r = await w.start("worker", "later")
        assert is_deferred_for_cap(r), r
        w.clock.set(utc(2026, 9, 17, 0, 0, 30))
        acme.costs(0.01)
        await w.server.wait_for_agents(timeout=10)
        await w.settle()
    asyncio.run(go())
    assert acme.spawns() == 2


# ------------------------------------------------ SC-R3a: steer, never auto --

def stop_by_cap(w, acme, **kw):
    """A run that crosses a 1.0 cap on its third step and is stopped mid-turn."""
    acme.script(steps=[{"cost": 0.6}, {"cost": 0.6}, {"cost": 0.6, "sleep": 5.0}], **kw)


def resume_error(result) -> str:
    return " ".join(sc.strings(result))


def test_r3a_steering_a_session_stopped_by_the_cap_is_refused_naming_cap_spend_and_reset(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme)

    async def go():
        aid = await w.started("worker", "long job")
        states = await w.until(aid, timeout=20)
        assert states[aid] == "limited", states
        spawned = acme.spawns()
        r = await w.server.steer_agent(aid, "carry on")
        return aid, spawned, r
    aid, spawned, r = asyncio.run(go())
    text = resume_error(r)
    assert r.get("steered") is not True, r
    assert "spend_cap" in text, r
    assert 1.0 in [v for _, v in sc.leaves(r) if isinstance(v, (int, float))] or "1.0" in text, \
        f"the binding cap (1.0) is not named: {r}"
    assert any(abs(v - 1.2) < 0.31 for _, v in sc.leaves(r)
               if isinstance(v, (int, float)) and not isinstance(v, bool)) or "1.2" in text, \
        f"the spend is not named: {r}"
    assert sc.has_time(r, sc.period_end(WED, "day")), f"the reset time is not named: {r}"
    assert acme.spawns() == spawned, "a refused steer spawned the CLI"
    assert w.status(aid) == "limited"


def test_r3a_with_several_binding_caps_the_error_names_the_latest_reset(w):
    acme = acme_world(w, {"usd": 1.0, "period": "day",
                          "models": {"acme/m1": {"usd": 1.0, "period": "week"}}})
    w.up()
    stop_by_cap(w, acme)

    async def go():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        return await w.server.steer_agent(aid, "carry on")
    r = asyncio.run(go())
    assert "spend_cap" in resume_error(r), r
    assert sc.has_time(r, sc.period_end(WED, "week")), f"the latest reset is missing: {r}"


def test_r3a_once_the_cap_is_raised_the_same_steer_resumes_the_same_node_and_session(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme)

    async def go():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        session = w.node(aid).session_id
        assert session, "the stopped node kept no session id"
        refused = await w.server.steer_agent(aid, "too early")
        w.p.cap("acme", {"usd": 50.0})
        acme.costs(0.1)
        ok = await w.server.steer_agent(aid, "now")
        states = await w.until(aid, timeout=20)
        await w.settle()
        return aid, session, refused, ok, states
    aid, session, refused, ok, states = asyncio.run(go())
    assert refused.get("steered") is not True
    assert ok.get("steered") is True and ok.get("agent_id") == aid, ok
    assert states[aid] == "done", states
    assert "-s" in acme.argv_of_spawn(-1) and session in acme.argv_of_spawn(-1)


def test_r3a_the_resume_works_after_a_server_restart(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme)

    async def first():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        return aid, w.node(aid).session_id
    aid, session = asyncio.run(first())
    w.restart_server()
    refused = asyncio.run(w.server.steer_agent(aid, "still capped"))
    assert "spend_cap" in resume_error(refused) and refused.get("steered") is not True, refused
    w.p.cap("acme", {"usd": 50.0})
    acme.costs(0.1)

    async def second():
        ok = await w.server.steer_agent(aid, "now")
        await w.until(aid, timeout=20)
        await w.settle()
        return ok
    ok = asyncio.run(second())
    assert ok.get("steered") is True, ok
    assert session in acme.argv_of_spawn(-1)


def test_r3a_a_new_period_alone_lets_the_steer_through(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme)

    async def go():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        early = await w.server.steer_agent(aid, "same day")
        w.clock.set(utc(2026, 9, 17, 0, 0, 30))
        acme.costs(0.1)
        ok = await w.server.steer_agent(aid, "next day")
        await w.until(aid, timeout=20)
        await w.settle()
        return early, ok
    early, ok = asyncio.run(go())
    assert early.get("steered") is not True and "spend_cap" in resume_error(early), early
    assert ok.get("steered") is True, ok


def test_r3a_a_capped_node_without_a_session_id_is_reported_not_resumable(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme, no_session=True)

    async def go():
        aid = await w.started("worker", "long job")
        states = await w.until(aid, timeout=20)
        w.p.cap("acme", {"usd": 50.0})
        return states[aid], await w.server.steer_agent(aid, "resume please")
    state, r = asyncio.run(go())
    assert state == "limited", f"the run was not stopped by the cap: {state}"
    assert r.get("steered") is not True, r
    text = resume_error(r).lower()
    assert "session" in text or "resum" in text, r
    assert "spend_cap" not in text, "the cap was lifted: it must not be blamed"


def test_r3a_a_stopped_session_is_never_relaunched_by_the_system(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    stop_by_cap(w, acme)

    async def go():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        spawned = acme.spawns()
        queued = w.deferred()
        w.p.cap("acme", {"usd": 50.0})                       # now it would be allowed
        w.clock.set(utc(2026, 9, 17, 0, 0, 30))              # and a new period
        await w.server.wait_for_agents(timeout=3)
        await asyncio.sleep(1.0)
        return aid, spawned, queued
    aid, spawned, queued = asyncio.run(go())
    assert acme.spawns() == spawned, "a capped session was relaunched on its own"
    assert w.status(aid) == "limited"
    assert not queued, f"the stop queued a deferred restart: {queued}"


# --------------------------------------------- SC-R3b: every launch guarded --

def test_r3b_a_due_deferred_start_is_not_spawned_while_its_provider_is_capped(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()

    async def go():
        await spend_it(w, acme, 1.0)
        spawned = acme.spawns()
        # an entry deferred earlier for another reason, due now
        w.tree().defer({"agent": "worker", "task": "old", "timeout": None,
                        "model": None, "workdir": None}, time.time() - 5, "quota")
        await w.server.wait_for_agents(timeout=3)
        return spawned
    spawned = asyncio.run(go())
    assert acme.spawns() == spawned, "a deferred restart spawned under an exhausted cap"
    assert len(w.deferred()) == 1, "the entry was lost instead of staying queued"


def test_r3b_a_run_that_dies_alongside_a_crossing_is_not_respawned(w):
    """`DIES` exits 1 on its own with nothing to say (the runner's free retry
    looks for exactly that); `CROSS` crosses the cap at 0.4 s. Whatever happens
    to DIES, no second process of it may be launched under the spent cap."""
    acme = acme_world(w, {"usd": 1.0}, models=("m1", "m2"))
    w.up()
    acme.script(variants={
        "CROSS": {"steps": [{"cost": 1.5, "sleep": 0.4}, {"cost": 0.1, "sleep": 5.0}]},
        "DIES": {"steps": [], "tail_sleep": 1.5, "exit": 1, "silent": True},
    })

    async def go():
        dies = await w.started("worker", "DIES")
        cross = await w.started("worker2", "CROSS")
        states = await w.until([dies, cross], timeout=30)
        await w.settle()
        return states[cross]
    cross_state = asyncio.run(go())
    assert cross_state == "limited", f"the crossing run was not stopped: {cross_state}"
    launches = [k for k in range(acme.spawns()) if "DIES" in " ".join(acme.argv_of_spawn(k))]
    assert len(launches) == 1, f"DIES was launched {len(launches)} times"


def test_r3_a_start_after_an_observed_crossing_is_deferred_and_never_spawned(w):
    """A second agent on another model of the same capped provider, started
    after the first's crossing was observed: deferred, and never launched."""
    acme = acme_world(w, {"usd": 1.0}, models=("m1", "m2"))
    w.up()
    acme.script(variants={
        "FIRST": {"steps": [{"cost": 1.0}], "tail_sleep": 0.0},
        "SECOND": {"steps": [{"cost": 0.0, "sleep": 3.0}, {"cost": 0.5}]},
    })

    async def go():
        a = await w.start("worker", "FIRST")
        await asyncio.sleep(1.5)                  # FIRST's cost is observed by now
        b = await w.start("worker2", "SECOND")
        await w.settle()
        return a, b
    a, b = asyncio.run(go())
    assert is_deferred_for_cap(b), f"admitted under a spent cap: {b}"
    assert not any("SECOND" in " ".join(acme.argv_of_spawn(k)) for k in range(acme.spawns()))
