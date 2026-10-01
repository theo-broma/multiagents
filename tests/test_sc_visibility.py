"""SC-R5 / SC-R6 — what `budget_status` shows, and what must not change.

Contract: `context/specs/spend-caps.md`. The spend is seeded by real runs of
fake metered CLIs (there is no other way to write the ledger), at times the
test chooses with the offset clock.

The contract fixes *what* is shown, not the field names. Each figure is looked
for as "a number about this provider/model" (the subtree under a dict key
equal to its name, or a row naming it); distinguishing numbers are chosen so
nothing else in `budget_status` can equal them (cap 5.0 / spend 1.5 / remain
3.5), and the period-keyed ones are read off paths naming `day`/`week`/`month`.
The admission flag is a boolean whose key says refused/blocked/admitted/
available.
"""
from __future__ import annotations

import asyncio
import sys
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


def spend(w, acme, cost, agent="worker"):
    acme.costs(cost)

    async def go():
        aid = await w.started(agent, "spend")
        await w.until(aid, timeout=25)
        await w.settle()
    asyncio.run(go())


def about(status, scope):
    return [v for _, v in sc.numbers_about(status, scope)]


# ---------------------------------------------------------- capped provider --

def test_r5_a_capped_provider_shows_cap_period_spend_remaining_reset_and_admission(w):
    acme = w.provider("acme", spend_cap={"usd": 5.0, "period": "week"})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    spend(w, acme, 1.5)
    status = w.budget()
    nums = about(status, "acme")
    assert 5.0 in nums, f"cap not shown: {nums}"
    assert any(abs(v - 1.5) < 1e-9 for v in nums), f"spend not shown: {nums}"
    assert any(abs(v - 3.5) < 1e-9 for v in nums), f"remaining not shown: {nums}"
    subtree_strings = [s for sub in sc.scope_subtrees(status, "acme") for s in sc.strings(sub)]
    assert "week" in subtree_strings, "the period is not shown"
    assert any(sc.has_time(sub, sc.period_end(WED, "week")) for sub in sc.scope_subtrees(status, "acme")), \
        "the reset time is not shown"
    assert sc.refused_flag(status, "acme") is False, "no 'admission refused: false' flag"


def test_r5_a_provider_at_its_cap_shows_admission_refused_and_nothing_remaining(w):
    acme = w.provider("acme", spend_cap={"usd": 2.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    spend(w, acme, 2.0)
    status = w.budget()
    assert sc.refused_flag(status, "acme") is True, status
    left = [v for path, v in sc.numbers_about(status, "acme")
            if any(word in part.lower() for part in path for word in ("remain", "left"))]
    assert left and all(v <= 1e-9 for v in left), f"remaining is not shown as exhausted: {left}"


def test_r5_the_reset_follows_the_period_and_the_clock(w):
    acme = w.provider("acme", spend_cap={"usd": 5.0, "period": "month"})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    spend(w, acme, 1.5)
    for sub in sc.scope_subtrees(w.budget(), "acme"):
        if sc.has_time(sub, sc.period_end(WED, "month")):
            break
    else:
        pytest.fail("the month's end is not shown")
    w.clock.set(utc(2026, 10, 2, 8, 0, 0))                  # next month: spend resets
    nums = about(w.budget(), "acme")
    assert not any(abs(v - 3.5) < 1e-9 for v in nums), "last month's remaining is still shown"
    assert any(abs(v - 5.0) < 1e-9 for v in nums), "the full cap is not remaining in the new period"


def test_r5_per_model_caps_show_their_own_cap_spend_remaining_and_period(w):
    acme = w.provider("acme", spend_cap={"usd": 100.0, "period": "day",
                                         "models": {"acme/big": {"usd": 4.0, "period": "week"}}})
    w.agent("worker", "acme", "acme/big")
    w.agent("small", "acme", "acme/small")
    w.up()
    spend(w, acme, 1.25, agent="worker")
    spend(w, acme, 0.5, agent="small")
    status = w.budget()
    nums = about(status, "acme/big")
    assert 4.0 in nums, f"model cap not shown: {nums}"
    assert any(abs(v - 1.25) < 1e-9 for v in nums), f"model spend (not the provider's 1.75): {nums}"
    assert any(abs(v - 2.75) < 1e-9 for v in nums), f"model remaining not shown: {nums}"
    strings = [s for sub in sc.scope_subtrees(status, "acme/big") for s in sc.strings(sub)]
    assert "week" in strings, "the model's own period is not shown"
    assert sc.refused_flag(status, "acme/big") is False


def test_r5_a_capped_model_at_its_cap_shows_refused_while_the_provider_does_not(w):
    acme = w.provider("acme", spend_cap={"usd": 100.0, "models": {"acme/big": {"usd": 1.0}}})
    w.agent("worker", "acme", "acme/big")
    w.up()
    spend(w, acme, 1.0)
    status = w.budget()
    assert sc.refused_flag(status, "acme/big") is True, status


# -------------------------------------------------------- uncapped, metered --

def test_r5_an_uncapped_metered_provider_shows_its_day_week_and_month_spend(w):
    acme = w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()

    async def go():
        for when, cost in [(utc(2026, 9, 1, 0, 0, 0), 8.0), (utc(2026, 9, 14, 0, 0, 0), 2.0),
                           (utc(2026, 9, 16, 0, 0, 0), 1.0)]:
            w.clock.set(when)
            acme.costs(cost)
            aid = await w.started("worker", "spend")
            await w.until(aid, timeout=25)
            await w.settle()
        w.clock.set(WED)
    asyncio.run(go())
    status = w.budget()
    assert sc.period_spend_is(status, "acme", "day", 1.0), sc.period_numbers(status, "acme", "day")
    assert sc.period_spend_is(status, "acme", "week", 3.0), sc.period_numbers(status, "acme", "week")
    assert sc.period_spend_is(status, "acme", "month", 11.0), sc.period_numbers(status, "acme", "month")
    assert sc.refused_flag(status, "acme") in (None, False)


def test_r5_an_uncapped_provider_shows_no_cap_and_no_refusal(w):
    acme = w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    spend(w, acme, 7.25)
    status = w.budget()
    assert sc.refused_flag(status, "acme") in (None, False)
    assert sc.period_spend_is(status, "acme", "day", 7.25)


def test_r5_a_plan_provider_shows_no_period_spend(w):
    acme = w.provider("acme", billing="plan")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    spend(w, acme, 3.75)
    status = w.budget()
    assert not sc.period_spend_is(status, "acme", "day", 3.75)
    row = [r for r in status["by_model"] if r.get("provider") == "acme"]
    assert row and row[0].get("billing") == "plan", "the existing plan marker is unchanged"


# ---------------------------------------------------------------- SC-R6 --

EXISTING_KEYS = {"providers", "tree_usage", "by_model", "deferred_tasks", "advice", "context"}


def test_r6_budget_status_keeps_every_existing_field_with_and_without_a_cap(w):
    acme = w.provider("acme", spend_cap={"usd": 5.0})
    w.provider("beta")
    w.agent("worker", "acme", "acme/m1")
    w.agent("bworker", "beta", "beta/m1")
    w.up()
    spend(w, acme, 0.5)
    spend(w, w.fakes["beta"], 0.25, agent="bworker")
    status = w.budget()
    assert EXISTING_KEYS <= set(status), set(status)
    rows = {r["provider"]: r for r in status["by_model"]}
    assert {"provider", "model", "runs", "tokens", "cost_usd", "agents"} <= set(rows["acme"])
    assert rows["acme"]["cost_usd"] == 0.5 and rows["beta"]["cost_usd"] == 0.25
    assert status["tree_usage"]["cost_usd"] == 0.75
    for name in ("acme", "beta"):
        entry = status["providers"][name]
        assert {"provider", "known", "severity", "usable", "spent"} <= set(entry), entry
        assert entry["usable"] is True


def test_r6_with_no_cap_anywhere_a_huge_spend_changes_nothing(w):
    acme = w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.costs(1000.0, 1000.0)

    async def go():
        a = await w.started("worker", "one")
        await w.until(a, timeout=25)
        b = await w.started("worker", "two")
        states = await w.until(b, timeout=25)
        await w.settle()
        return states[b]
    assert asyncio.run(go()) == "done"
    assert not w.spend_events()
    assert not w.deferred()
