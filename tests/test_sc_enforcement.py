"""SC-R4 / SC-R4a — the run that crosses the cap, and everything drawing on it.

Contract: `context/specs/spend-caps.md` including SC-R4a. In-process: one
server, one tree, fake provider CLIs. Cross-process stops (a nested MCP server
owning the victim) are in `test_sc_cross_process.py`.

The crossing idiom: a variant of the fake CLI that emits `step_finish` parts
with a cost, then sleeps *before* its next step, so the test can tell "stopped
at the crossing" from "ran to the end": a stopped run never prints its final
line (`FakeProvider.finished()`), and the spend after the stop shows how many
steps were observed. The contract promises no bound on overshoot, so the
assertions leave room for charges in flight: they say "the crossing step is
recorded" and "the step four seconds later is not".

Assumptions where the contract is silent (each deliberately loose):
- `reason: spend_cap` and `until` are read from anywhere in what a caller can
  see of the agent: `check_agent`, `collect_agent`, the node's status/reason,
  and the agent's events in `events.jsonl`.
- The `spend_cap` event is an `events.jsonl` record with `kind: "spend_cap"`;
  which of its fields hold the provider, model, cap, spend and stopped agents
  is not fixed, so each is looked for among the event's values.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from sc_harness import WED, utc  # noqa: E402

LONG = {"steps": [{"cost": 0.0, "sleep": 0.3}] * 40}          # ~12 s, free
BRIEF = {"steps": [{"cost": 0.0, "sleep": 0.3}] * 8}          # ~2.4 s, free
CROSS = {"steps": [{"cost": 1.5, "sleep": 0.4}, {"cost": 0.1, "sleep": 4.0}]}


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def acme_world(w, cap, models=("m1",), **prov):
    acme = w.provider("acme", spend_cap=cap, **prov)
    for i, model in enumerate(models):
        w.agent("worker" if i == 0 else f"worker{i + 1}", "acme", f"acme/{model}")
    return acme


def seen_by_caller(w, agent_id):
    return w.agent_view(agent_id)


def says_spend_cap(view) -> bool:
    return sc.mentions(view, "spend_cap")


def untils(view) -> list[float]:
    return [e for e in (sc.as_epoch(v) for v in sc.find_key(view, "until")) if e is not None]


def spent_day(w, scope="acme"):
    return [v for v in sc.period_numbers(w.budget(), scope, "day") if abs(v) < 1e6]


# ----------------------------------------------------------- the stop itself --

def test_r4_a_run_whose_steps_cross_the_cap_is_stopped_as_limited_with_spend_cap(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=[{"cost": 0.4}, {"cost": 0.4}, {"cost": 0.4},
                       {"cost": 0.4, "sleep": 4.0}])

    async def go():
        aid = await w.started("worker", "spend")
        states = await w.until(aid, timeout=25)
        return aid, states[aid]
    aid, state = asyncio.run(go())
    assert state == "limited", f"expected limited, got {state}"
    view = seen_by_caller(w, aid)
    assert says_spend_cap(view), view
    assert acme.finished() == 0, "the run was allowed to finish"
    day = [v for v in spent_day(w) if v]
    assert day and 1.2 - 1e-9 <= max(day) < 1.6, f"crossing step missing or 4th step counted: {day}"


def test_r4_spend_that_reaches_the_cap_exactly_stops_the_run(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=[{"cost": 0.5}, {"cost": 0.5}, {"cost": 0.5, "sleep": 4.0}])

    async def go():
        aid = await w.started("worker", "spend")
        return (await w.until(aid, timeout=25))[aid]
    assert asyncio.run(go()) == "limited"


def test_r4_a_run_that_stays_under_the_cap_is_left_alone(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.costs(0.4, 0.4)

    async def go():
        aid = await w.started("worker", "spend")
        return (await w.until(aid, timeout=25))[aid]
    assert asyncio.run(go()) == "done"
    assert acme.finished() == 1


def test_r4_the_stopped_run_keeps_its_branch_worktree_and_session(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        states = await w.until(aid, timeout=25)
        return aid, states[aid]
    aid, state = asyncio.run(go())
    assert state == "limited"
    node = w.node(aid)
    assert node.session_id, "the session id was not kept"
    assert node.worktree and Path(node.worktree).is_dir(), "the worktree was removed"
    branches = subprocess.run(["git", "-C", str(w.p.root), "branch", "--list", node.branch],
                              capture_output=True, text=True).stdout
    assert node.branch and node.branch in branches, "the branch was removed"


def test_r4_a_cap_stop_is_not_failed_cancelled_or_discarded(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=25)
        return aid
    aid = asyncio.run(go())
    assert w.status(aid) not in {"failed", "cancelled", "discarded", "orphaned", "done"}
    assert w.status(aid) == "limited"


# --------------------------------------------------- the verdict (SC-R4a) --

def test_r4a_the_verdict_carries_the_period_end_as_until(w):
    acme = acme_world(w, {"usd": 1.0, "period": "week"})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=25)
        return aid
    aid = asyncio.run(go())
    view = seen_by_caller(w, aid)
    assert says_spend_cap(view), view
    assert any(abs(u - sc.period_end(WED, "week")) <= 60 for u in untils(view)), \
        f"until is not the week's end: {untils(view)} in {view}"


def test_r4a_until_is_the_latest_of_the_applicable_resets(w):
    acme = acme_world(w, {"usd": 1.0, "period": "day",
                          "models": {"acme/m1": {"usd": 1.0, "period": "month"}}})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=25)
        return aid
    aid = asyncio.run(go())
    view = seen_by_caller(w, aid)
    ends = untils(view)
    assert any(abs(u - sc.period_end(WED, "month")) <= 60 for u in ends), (ends, view)
    assert abs(max(ends) - sc.period_end(WED, "month")) <= 60, f"not the latest reset: {ends}"


def test_r4a_a_cap_stop_never_retries_falls_back_or_cools_the_provider(w):
    """The crossing run dies on its own right after the step (nothing to say,
    exit 1): the shape of the free retry. It must stay a cap stop."""
    acme = acme_world(w, {"usd": 1.0})
    w.p.agents["worker"]["models"] = {"beta": "beta/b1"}
    beta = w.provider("beta")
    beta.costs(0.01)
    w.p.project["limits"]["provider_failure_threshold"] = 1
    w.up()
    acme.script(steps=[{"cost": 1.5}], silent=True, exit=1)

    async def go():
        aid = await w.started("worker", "spend")
        states = await w.until(aid, timeout=25)
        await asyncio.sleep(1.5)                    # time for a retry or fallback, if one were coming
        return aid, states[aid]
    aid, state = asyncio.run(go())
    assert state == "limited", state
    assert says_spend_cap(seen_by_caller(w, aid))
    assert acme.spawns() == 1, "the cap stop was retried"
    assert beta.spawns() == 0, "the cap stop fell back to another provider"
    assert "cooling down" not in " ".join(w.budget().get("advice", []))
    # and the failure breaker was not fed: acme is routable once the cap allows
    w.p.cap("acme", {"usd": 50.0})
    acme.costs(0.01)

    async def again():
        r = await w.start("worker", "after")
        await w.settle()
        return r
    r = asyncio.run(again())
    assert r.get("provider") == "acme", f"a cap stop was counted as a provider failure: {r}"


# -------------------------------------------------------- who else is stopped --

def test_r4_every_other_active_run_on_the_capped_provider_is_stopped_too(w):
    acme = acme_world(w, {"usd": 1.0}, models=("m1", "m2"))
    w.up()
    acme.script(variants={"CROSS": CROSS, "LONG": LONG})

    async def go():
        long_run = await w.started("worker2", "LONG")
        cross = await w.started("worker", "CROSS")
        states = await w.until([long_run, cross], timeout=25)
        return long_run, cross, states
    long_run, cross, states = asyncio.run(go())
    assert states[cross] == "limited" and states[long_run] == "limited", states
    assert says_spend_cap(seen_by_caller(w, long_run))
    assert acme.finished() == 0, "a stopped run ran to its natural end"


def test_r4_a_model_cap_stops_runs_on_that_model_and_leaves_its_sibling_alone(w):
    acme = acme_world(w, {"models": {"acme/big": {"usd": 1.0}}}, models=("big", "small"))
    w.up()
    acme.script(variants={"CROSS": CROSS, "LONG": LONG, "BRIEF": BRIEF})
    w.agent("big2", "acme", "acme/big")
    w.reload()

    async def go():
        long_big = await w.started("big2", "LONG")
        brief_small = await w.started("worker2", "BRIEF")
        cross = await w.started("worker", "CROSS")
        states = await w.until([long_big, brief_small, cross], timeout=30)
        await w.settle()
        return long_big, brief_small, cross, states
    long_big, brief_small, cross, states = asyncio.run(go())
    assert states[cross] == "limited" and states[long_big] == "limited", states
    assert states[brief_small] == "done", f"the sibling model's run was stopped: {states}"


def test_r4_a_run_on_another_provider_is_not_stopped(w):
    acme = acme_world(w, {"usd": 1.0})
    beta = w.provider("beta")
    w.agent("beta-worker", "beta", "beta/b1")
    w.up()
    acme.script(steps=CROSS["steps"])
    beta.script(steps=BRIEF["steps"])

    async def go():
        other = await w.started("beta-worker", "elsewhere")
        cross = await w.started("worker", "spend")
        states = await w.until([other, cross], timeout=30)
        return states[other], states[cross]
    other, cross = asyncio.run(go())
    assert cross == "limited" and other == "done", (cross, other)


def test_r4_with_no_cap_nothing_is_ever_stopped(w):
    acme = acme_world(w, None)
    w.up()
    acme.costs(500.0, 500.0)

    async def go():
        aid = await w.started("worker", "big spender")
        return (await w.until(aid, timeout=25))[aid]
    assert asyncio.run(go()) == "done"
    assert not w.spend_events()


# ------------------------------------------------------------------- events --

def event_text(event) -> str:
    return " ".join(sc.strings(event))


def test_r4_one_spend_cap_event_per_crossing_naming_provider_cap_spend_and_agents(w):
    acme = acme_world(w, {"usd": 1.0}, models=("m1", "m2"))
    w.up()
    acme.script(variants={"CROSS": CROSS, "LONG": LONG})

    async def go():
        long_run = await w.started("worker2", "LONG")
        cross = await w.started("worker", "CROSS")
        await w.until([long_run, cross], timeout=25)
        return long_run, cross
    long_run, cross = asyncio.run(go())
    events = w.spend_events()
    assert len(events) == 1, f"expected one event for one crossing: {events}"
    e = events[0]
    text = event_text(e)
    assert "acme" in text
    nums = [v for _, v in sc.leaves(e) if isinstance(v, (int, float)) and not isinstance(v, bool)]
    assert 1.0 in nums, f"the cap is not named: {e}"
    assert any(1.5 <= v < 2.0 for v in nums), f"the spend (>= 1.5) is not named: {e}"
    assert cross in text and long_run in text, f"stopped agents not named: {e}"


def test_r4_two_runs_crossing_together_still_make_one_event(w):
    acme = acme_world(w, {"usd": 1.0}, models=("m1", "m2"))
    w.up()
    steps = [{"cost": 0.6, "sleep": 0.3}] * 2 + [{"cost": 0.6, "sleep": 4.0}]
    acme.script(variants={"AAA": {"steps": steps}, "BBB": {"steps": steps}})

    async def go():
        a = await w.started("worker", "AAA")
        b = await w.started("worker2", "BBB")
        await w.until([a, b], timeout=25)
    asyncio.run(go())
    assert len(w.spend_events()) == 1, w.spend_events()


def test_r4_a_model_cap_event_names_the_model(w):
    acme = acme_world(w, {"models": {"acme/big": {"usd": 1.0}}}, models=("big",))
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=25)
    asyncio.run(go())
    events = w.spend_events()
    assert len(events) == 1, events
    assert "acme/big" in event_text(events[0]), events[0]


def test_r4a_simultaneous_provider_and_model_crossings_give_one_event_each(w):
    acme = acme_world(w, {"usd": 1.0, "models": {"acme/big": {"usd": 1.0}}}, models=("big",))
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=25)
    asyncio.run(go())
    events = w.spend_events()
    assert len(events) == 2, events
    assert sum("acme/big" in event_text(e) for e in events) == 1, events


def test_r4a_a_new_cap_value_in_the_same_period_is_a_new_crossing(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        a = await w.started("worker", "first")
        await w.until(a, timeout=25)
        w.p.cap("acme", {"usd": 2.0})
        acme.script(steps=[{"cost": 1.0}, {"cost": 1.0, "sleep": 4.0}])
        b = await w.started("worker", "second")
        await w.until(b, timeout=25)
    asyncio.run(go())
    assert len(w.spend_events()) == 2, w.spend_events()


def test_r4a_a_new_period_is_a_new_crossing(w):
    acme = acme_world(w, {"usd": 1.0})
    w.up()
    acme.script(steps=CROSS["steps"])

    async def go():
        a = await w.started("worker", "first")
        await w.until(a, timeout=25)
        w.clock.set(utc(2026, 9, 17, 9, 0, 0))
        b = await w.started("worker", "second")
        await w.until(b, timeout=25)
    asyncio.run(go())
    assert len(w.spend_events()) == 2, w.spend_events()


# ---------------------------------------------------------------- lowered cap --

def test_r4a_lowering_a_cap_below_the_spend_stops_the_run_at_its_next_cost_event(w):
    acme = acme_world(w, {"usd": 100.0}, models=("m1", "m2"))
    w.up()
    acme.script(steps=[{"cost": 0.1, "sleep": 0.5}] * 12)

    async def go():
        aid = await w.started("worker", "slow spender")
        await asyncio.sleep(2.0)                              # about 0.3 spent
        w.p.cap("acme", {"usd": 0.2})
        w.server.list_agents()                                # the next server call sees the edit
        refused = await w.start("worker2", "new launch")
        states = await w.until(aid, timeout=20)
        return aid, states[aid], refused
    aid, state, refused = asyncio.run(go())
    assert state == "limited", f"the lowered cap did not stop the run: {state}"
    assert says_spend_cap(seen_by_caller(w, aid))
    assert not refused.get("agent_id") and "spend_cap" in str(refused), refused
    assert acme.finished() == 0
    day = [v for v in spent_day(w) if v]
    assert day and max(day) < 1.0, f"the run kept spending: {day}"
