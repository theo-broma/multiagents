"""SC-R2 / SC-R2a — the durable spend ledger. Contract: `context/specs/spend-caps.md`.

The ledger has no public reader in the contract, so everything is observed
through `budget_status` (SC-R5: period spend of every metered provider, capped
or not) and through what the ledger makes possible (a cap that remembers).
The fake provider streams opencode-style `step_finish` parts with unique ids.

Assumptions where the contract is silent (each deliberately loose):
- Period spend is read as "a number about the provider on a path naming
  `day`, `week` or `month`" (`sc_harness.period_spend_is`). The contract fixes
  the three periods, not the field names. Partial periods are a truthy leaf
  on a path naming `partial` and the period, or a `partial` value naming it.
- The ledger *file* is only touched by the torn-write and fail-closed tests,
  and is found by content (the step id those tests plant), excluding the tree,
  the event log and the run directories. Finding none is a failure: the
  contract says the ledger is a dedicated store.
- A torn last entry is "ignored or completed" (contract): the tests accept
  either, and demand that earlier entries survive and new spend still counts.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from sc_harness import WED, utc  # noqa: E402
from multiagents.tree import Node  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def one(w, spend_cap=None, **prov):
    acme = w.provider("acme", **({"spend_cap": spend_cap} if spend_cap is not None else {}), **prov)
    w.agent("worker", "acme", "acme/m1")
    w.up()
    return acme


async def run_once(w, task="do it", agent="worker", **kw):
    aid = await w.started(agent, task, **kw)
    states = await w.until(aid)
    await w.settle()
    return aid, states[aid]


def spend(w, period="day", scope="acme"):
    return [v for v in sc.period_numbers(w.budget(), scope, period) if abs(v) < 1e6]


def assert_spend(w, expected: dict, scope="acme"):
    status = w.budget()
    for period, value in expected.items():
        assert sc.period_spend_is(status, scope, period, value), (
            f"{scope} {period}: expected {value}, budget_status showed "
            f"{sc.period_numbers(status, scope, period)}")


# ------------------------------------------------------------ what lands --

def test_r2_every_step_cost_lands_in_every_period_total(w):
    acme = one(w)
    acme.costs(0.5, 0.25)

    async def go():
        return await run_once(w)
    _, state = asyncio.run(go())
    assert state == "done"
    assert_spend(w, {"day": 0.75, "week": 0.75, "month": 0.75})


def test_r2_zero_cost_steps_add_nothing_and_do_not_hide_the_real_ones(w):
    acme = one(w)
    acme.costs(0.0, 0.5, 0.0)
    asyncio.run(run_once(w))
    assert_spend(w, {"day": 0.5})


def test_r2_spend_accumulates_across_runs(w):
    acme = one(w)
    acme.costs(0.5)

    async def go():
        await run_once(w)
        await run_once(w)
        await run_once(w)
    asyncio.run(go())
    assert_spend(w, {"day": 1.5})


def test_r2_the_ledger_is_a_dedicated_store_that_is_not_the_event_log_or_the_tree(w):
    acme = one(w)
    acme.costs(0.5, ids=["prt_LEDGERMARK_1"])
    asyncio.run(run_once(w))
    found = locate_ledger(w, b"prt_LEDGERMARK_1")
    assert found is not None, "no dedicated file outside the tree, events and run dirs holds the entry"


# ----------------------------------------------------------------- dedup --

def test_r2_replaying_the_same_stream_adds_nothing(w):
    acme = one(w)
    acme.costs(0.5, 0.25, ids=["prt_a", "prt_b"])

    async def go():
        aid, _ = await run_once(w)
        # the resumed session re-emits its history, then does one new step
        acme.costs(0.5, 0.25, 0.125, ids=["prt_a", "prt_b", "prt_c"])
        await w.server.steer_agent(aid, "carry on")
        await w.until(aid)
        await w.settle()
    asyncio.run(go())
    assert_spend(w, {"day": 0.875})


def test_r2_the_same_step_id_in_another_session_is_another_charge(w):
    acme = one(w)
    acme.costs(0.5, ids=["prt_same"])

    async def go():
        await run_once(w)
        await run_once(w)
    asyncio.run(go())
    assert_spend(w, {"day": 1.0})


def test_r2_without_step_ids_every_stream_position_counts(w):
    acme = one(w)
    acme.script(steps=sc.steps_of([0.25, 0.25, 0.25], noid=True))
    asyncio.run(run_once(w))
    assert_spend(w, {"day": 0.75})


def test_r2_a_replay_keeps_the_first_observation_time(w):
    acme = one(w)
    acme.costs(1.0, ids=["prt_first"])

    async def go():
        aid, _ = await run_once(w)
        w.clock.set(utc(2026, 9, 17, 10, 0, 0))                 # the next day
        acme.costs(1.0, 0.25, ids=["prt_first", "prt_new"])
        await w.server.steer_agent(aid, "again")
        await w.until(aid)
        await w.settle()
    asyncio.run(go())
    assert_spend(w, {"day": 0.25, "week": 1.25, "month": 1.25})


# --------------------------------------------------------------- survival --

def test_r2_spend_survives_steer_and_the_agent_keeps_its_session(w):
    acme = one(w)
    acme.costs(0.5)

    async def go():
        aid, _ = await run_once(w)
        acme.costs(0.25)
        await w.server.steer_agent(aid, "more")
        await w.until(aid)
        await w.settle()
        return aid
    asyncio.run(go())
    assert_spend(w, {"day": 0.75})


def test_r2_a_discarded_runs_spend_still_counts(w):
    acme = one(w)
    acme.costs(0.5)

    async def go():
        aid, _ = await run_once(w)
        return aid
    aid = asyncio.run(go())
    out = w.server.discard_agent(aid, force=True)
    assert "error" not in out, out
    assert_spend(w, {"day": 0.5})


def test_r2_a_capped_provider_still_counts_a_discarded_runs_spend(w):
    acme = one(w, {"usd": 1.0})
    acme.costs(0.5)

    async def go():
        aid, _ = await run_once(w)
        return aid
    aid = asyncio.run(go())
    w.server.discard_agent(aid, force=True)
    acme.costs(0.5)
    r = asyncio.run(w.start("worker"))
    assert not r.get("agent_id"), "the discarded run's 0.5 was forgotten; 0.5 + 0.5 reaches the cap"
    assert "spend_cap" in str(r)


# ------------------------------------------------------------------- plan --

def test_r2_a_plan_providers_would_be_cost_is_never_recorded_or_counted(w):
    """Two providers, one cap, one stream: only the metered one is billed."""
    for name, billing in (("acme", "plan"), ("beta", "metered")):
        w.provider(name, billing=billing, spend_cap={"usd": 1.0})
        w.agent(f"{name}-worker", name, f"{name}/m1")
        w.fakes[name].costs(5.0, 5.0)
    w.up()

    async def go():
        states = {}
        for name in ("acme", "beta"):
            aid, states[name] = await run_once(w, agent=f"{name}-worker")
        again = {name: await w.start(f"{name}-worker") for name in ("acme", "beta")}
        await w.settle()
        return states, again
    states, again = asyncio.run(go())
    assert states["acme"] == "done", states
    assert again["acme"].get("agent_id"), \
        f"a plan provider was refused by a cap it cannot spend against: {again['acme']}"
    assert not again["beta"].get("agent_id"), "the metered provider's 10.0 should have capped it"
    assert not [v for v in spend(w, "day", "acme") if v], spend(w, "day", "acme")
    assert all("acme" not in str(e) for e in w.spend_events())


# ------------------------------------------------------------------ periods --

BOUNDARY = [
    pytest.param(
        WED,
        [(utc(2026, 8, 31, 23, 59, 0), 64.0),       # last month
         (utc(2026, 9, 1, 0, 0, 0), 8.0),           # month starts: counts for month only
         (utc(2026, 9, 13, 23, 59, 0), 4.0),        # Sunday: last week, this month
         (utc(2026, 9, 14, 0, 0, 0), 2.0),          # Monday 00:00: week starts
         (utc(2026, 9, 15, 23, 59, 0), 0.5),        # yesterday
         (utc(2026, 9, 16, 0, 0, 0), 1.0)],         # today, at 00:00
        {"day": 1.0, "week": 3.5, "month": 15.5},
        id="mid-month-wednesday"),
    pytest.param(
        utc(2027, 1, 7, 12, 0, 0),
        [(utc(2026, 12, 31, 23, 59, 0), 8.0),       # last year
         (utc(2027, 1, 1, 0, 0, 0), 4.0),           # the year turns: month only (week began Dec 28)
         (utc(2027, 1, 4, 0, 0, 0), 0.5),           # Monday of this week is Jan 4
         (utc(2027, 1, 7, 0, 0, 0), 1.0)],
        {"day": 1.0, "week": 1.5, "month": 5.5},
        id="year-boundary"),
]


@pytest.mark.parametrize("now,events,expected", BOUNDARY)
def test_r2_period_sums_are_right_across_day_week_and_month_boundaries(w, now, events, expected):
    acme = one(w)

    async def go():
        for when, cost in events:
            w.clock.set(when)
            acme.costs(cost)
            await run_once(w)
        w.clock.set(now)
    asyncio.run(go())
    assert_spend(w, expected)


@pytest.mark.parametrize("zone,now,events,day", [
    pytest.param("America/Los_Angeles", utc(2026, 9, 16, 5, 0, 0),
                 [(utc(2026, 9, 16, 1, 0, 0), 1.0), (utc(2026, 9, 15, 23, 30, 0), 2.0)], 1.0,
                 id="behind-utc"),
    pytest.param("Pacific/Auckland", utc(2026, 9, 15, 20, 0, 0),
                 [(utc(2026, 9, 15, 1, 0, 0), 1.0), (utc(2026, 9, 14, 23, 30, 0), 2.0)], 1.0,
                 id="ahead-of-utc"),
])
def test_r2_periods_are_utc_whatever_the_machines_time_zone(w, monkeypatch, zone, now, events, day):
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        acme = one(w)

        async def go():
            for when, cost in events:
                w.clock.set(when)
                acme.costs(cost)
                await run_once(w)
            w.clock.set(now)
        asyncio.run(go())
        assert_spend(w, {"day": day, "week": 3.0})
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


# ------------------------------------------------------------ concurrency --

def test_r2_concurrent_runs_never_lose_or_double_an_entry(w):
    acme = one(w)
    acme.script(steps=sc.steps_of([0.0625] * 10, sleep=0.03))

    async def go():
        ids = [await w.started("worker", f"run {i}") for i in range(4)]
        await w.until(ids, timeout=60)
        await w.settle()
    asyncio.run(go())
    assert_spend(w, {"day": 2.5, "week": 2.5, "month": 2.5})


# --------------------------------------------------------------- history --

def test_r2_spend_from_before_the_ledger_has_no_timestamp_and_does_not_count(w):
    acme = one(w, {"usd": 5.0})
    # a finished run from before the ledger existed: usage on a node, nothing else
    w.tree().add(Node(id="ag-old001", agent="worker", provider="acme", model="acme/m1",
                      parent=None, depth=1, status="done", task="old work",
                      usage={"cost_usd": 4.0, "total": 1000}, started_at=sc.WED - 86400))
    acme.costs(1.5)

    async def go():
        return await run_once(w)
    _, state = asyncio.run(go())
    assert state == "done", "history was counted: 4.0 + 1.5 passes the 5.0 cap"
    assert_spend(w, {"day": 1.5, "week": 1.5, "month": 1.5})


def test_r2_the_period_of_the_upgrade_is_reported_partial_until_it_ends(w):
    acme = one(w)
    acme.costs(1.0)

    async def stage(when):
        w.clock.set(when)
        w.restart_server()                  # the ledger's start time outlives the server
        await run_once(w)
        return sc.partial_periods(w.budget(), "acme")

    async def go():
        seen = {}
        seen["wed"] = await stage(WED)
        seen["thu"] = await stage(utc(2026, 9, 17, 0, 0, 30))
        seen["next_monday"] = await stage(utc(2026, 9, 21, 0, 0, 30))
        seen["next_month"] = await stage(utc(2026, 10, 1, 0, 0, 30))
        return seen
    seen = asyncio.run(go())
    assert seen["wed"] == {"day", "week", "month"}, seen
    assert seen["thu"] == {"week", "month"}, seen
    assert seen["next_monday"] == {"month"}, seen
    assert seen["next_month"] == set(), seen


def test_r2_figures_are_labelled_as_this_projects_accounting_not_the_invoice(w):
    acme = one(w)
    acme.costs(0.5)
    asyncio.run(run_once(w))
    text = " ".join(sc.strings(w.budget())).lower()
    assert "this project's accounting" in text and "invoice" in text


# ------------------------------------------------------- torn / unreadable --

def locate_ledger(w, marker: bytes):
    roots = [w.p.root / ".multiagents", Path(os.environ["MULTIAGENTS_STATE_DIR"])]
    skip_parts = {"runs", "worktrees", "homes"}
    skip_names = {"tree.json", "events.jsonl"}
    hits = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.name in skip_names:
                continue
            if skip_parts & set(path.relative_to(root).parts):
                continue
            try:
                if marker in path.read_bytes():
                    hits.append(path)
            except OSError:
                pass
    named = [p for p in hits if "ledger" in p.name or "spend" in p.name]
    return (named or hits or [None])[0]


def is_sqlite(path: Path) -> bool:
    return path.read_bytes()[:15] == b"SQLite format 3"


def test_r2a_a_torn_last_entry_is_recovered_from(w):
    acme = one(w)
    acme.costs(0.5, ids=["prt_TORN_1"])

    async def first():
        await run_once(w)
    asyncio.run(first())
    ledger = locate_ledger(w, b"prt_TORN_1")
    assert ledger is not None, "ledger not found by content"
    if is_sqlite(ledger):
        pytest.skip("the ledger is a transactional database; a torn append cannot occur")
    with ledger.open("ab") as fh:                           # a half-written next entry
        fh.write(b'{"provider": "acme", "model": "acme/m1", "usd": 0.7, "ts')
    w.restart_server()
    assert_spend(w, {"day": 0.5})                           # earlier entries intact
    acme.costs(0.25, ids=["prt_TORN_2"])
    asyncio.run(run_once(w))
    assert_spend(w, {"day": 0.75})                          # and the next charge still lands


def test_r2a_a_truncated_last_entry_is_ignored_or_completed_never_corrupting_earlier_ones(w):
    acme = one(w)

    async def go():
        acme.costs(0.5, ids=["prt_TRUNC_1"])
        await run_once(w)
        acme.costs(0.25, ids=["prt_TRUNC_2"])
        await run_once(w)
    asyncio.run(go())
    ledger = locate_ledger(w, b"prt_TRUNC_2")
    assert ledger is not None, "ledger not found by content"
    if is_sqlite(ledger):
        pytest.skip("the ledger is a transactional database; a torn append cannot occur")
    data = ledger.read_bytes()
    ledger.write_bytes(data[:-9])                           # tear the last entry
    w.restart_server()
    day = [v for v in spend(w, "day") if v]
    assert 0.5 in day or 0.75 in day, f"earlier entry lost or corrupted: {day}"
    kept = 0.75 if 0.75 in day else 0.5
    acme.costs(0.125, ids=["prt_TRUNC_3"])
    asyncio.run(run_once(w))
    assert_spend(w, {"day": kept + 0.125})


def _lock_down(ledger: Path, mode: int):
    for path in ledger.parent.glob(ledger.name + "*"):
        if path.is_file():
            path.chmod(mode)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.parametrize("mode", [0o000, 0o444], ids=["unreadable", "unwritable"])
def test_r2a_under_a_cap_an_unusable_ledger_refuses_launches_as_spend_cap_unreadable(w, mode):
    acme = one(w, {"usd": 100.0})
    acme.costs(0.5, ids=["prt_LOCK_1"])
    asyncio.run(run_once(w))
    ledger = locate_ledger(w, b"prt_LOCK_1")
    assert ledger is not None, "ledger not found by content"
    spawned = acme.spawns()
    _lock_down(ledger, mode)
    try:
        r = asyncio.run(w.start("worker"))
    finally:
        _lock_down(ledger, 0o644)
    assert not r.get("agent_id"), f"launched under a cap with no usable ledger: {r}"
    assert "spend_cap_unreadable" in str(r), r
    assert acme.spawns() == spawned


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.parametrize("mode", [0o000, 0o444], ids=["unreadable", "unwritable"])
def test_r2a_with_no_cap_an_unusable_ledger_never_blocks_anything(w, mode):
    acme = one(w)
    acme.costs(0.5, ids=["prt_FREE_1"])
    asyncio.run(run_once(w))
    ledger = locate_ledger(w, b"prt_FREE_1")
    assert ledger is not None, "ledger not found by content"
    _lock_down(ledger, mode)
    try:
        async def go():
            return await run_once(w)
        aid, state = asyncio.run(go())
    finally:
        _lock_down(ledger, 0o644)
    assert state == "done", (aid, state)
    assert acme.spawns() == 2
