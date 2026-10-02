"""Regression tests for SC review round 3 (reviewer ag-1ef2a0), one or more
per finding. Contract: `context/specs/spend-caps.md`, decisions included."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from multiagents import spendcap  # noqa: E402
from multiagents.runner import SPEND_PENDING  # noqa: E402
from multiagents.tree import Node, Tree  # noqa: E402

LONG = [{"cost": 0.0, "sleep": 0.3}] * 40
DAY_CAP = [spendcap.Cap("acme", "", 1.0, "day")]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def held(usd, at=None, node="ag-gone001", key=None):
    return {"key": key or json.dumps(["held", node, usd]), "usd": usd,
            "at": time.time() if at is None else at, "provider": "acme",
            "model": "acme/m1", "agent": "worker", "node": node}


def foreign(w, usd, caps=(), node="ag-other1"):
    return spendcap.Ledger(w.runner.ledger.path).charge(
        key=json.dumps(["foreign", node, usd]), provider="acme", model="acme/m1",
        agent="other", node=node, usd=usd, caps=list(caps))


def capped_world(w, **extra):
    w.provider("acme", spend_cap={"usd": 1.0}, **extra)
    w.agent("worker", "acme", "acme/m1")
    w.up()
    return w.runner


# ---------------------------------------------------------------- #1 ----

def test_1_a_held_charge_that_cannot_be_committed_refuses_as_unreadable(w, monkeypatch):
    runner = capped_world(w)
    runner._hold_charges([held(1.5)])

    def no_space(records):
        if any(r.get("kind") == "charge" for r in records):
            raise spendcap.LedgerError("No space left on device")
        return real(records)
    real = runner.ledger._append
    monkeypatch.setattr(runner.ledger, "_append", no_space)
    refusal = runner._cap_refusal("acme", "acme/m1")
    assert refusal is not None and refusal["cause"] == "spend_cap_unreadable", refusal


# ---------------------------------------------------------------- #2 ----

def test_2_a_charge_another_process_held_just_now_binds_the_next_admission(w):
    runner = capped_world(w)
    assert runner._pending() == []               # this runner's cache: empty, just read
    other = Tree(runner.paths.tree_file, runner.paths.events_file)
    with other.transaction() as data:            # "another server" holds a charge
        data.setdefault(SPEND_PENDING, []).append(held(1.5))
    refusal = runner._cap_refusal("acme", "acme/m1")
    assert refusal is not None and refusal["cause"] == "spend_cap", refusal


def test_2_with_no_cap_the_cheap_cache_still_applies(w):
    w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    assert runner._pending() == []
    other = Tree(runner.paths.tree_file, runner.paths.events_file)
    with other.transaction() as data:
        data.setdefault(SPEND_PENDING, []).append(held(1.5))
    assert runner._cap_refusal("acme", "acme/m1") is None      # no cap: never refused
    assert runner._pending() == []                             # cached (SC-R6)


# ---------------------------------------------------------------- #3 ----

def test_3_a_late_charge_from_an_ended_period_stops_nothing_now(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    acme.script(steps=LONG)

    async def go():
        aid = await w.started("worker", "today")
        await asyncio.sleep(0.5)
        runner._hold_charges([held(1.5, at=sc.WED - 86400)])   # yesterday's
        assert runner._flush_pending(provider="acme", fresh=True)
        await asyncio.sleep(1.0)
        run = runner.runs[aid]
        status = w.status(aid)
        await w.server.stop_agent(aid)
        await w.settle()
        return run, status
    run, status = asyncio.run(go())
    assert run.cap_stop is None and status == "running", (run.cap_stop, status)
    fresh = spendcap.Ledger(runner.ledger.path)
    fresh.refresh()
    assert fresh.crossings, "yesterday's crossing was not recorded"
    assert not any(fresh.stops.values()), fresh.stops


# ---------------------------------------------------------------- #4 ----

def test_4_a_torn_event_line_is_not_a_delivery(w, monkeypatch):
    runner = capped_world(w)
    monkeypatch.setattr(runner, "ANNOUNCE_GRACE_SECONDS", 0.0)
    new, _ = foreign(w, 1.5, caps=DAY_CAP)
    ident = new[0]["id"]
    events = runner.paths.events_file
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a") as fh:                  # a writer died mid-line
        fh.write(json.dumps({"kind": "spend_cap", "crossing_id": ident})[:-3])
    runner._announce_pending()
    whole = [e for e in w.p.event_records() if e.get("crossing_id") == ident]
    assert len(whole) == 1 and whole[0].get("recovered") is True, whole
    runner._announce_pending()
    assert len([e for e in w.p.event_records() if e.get("crossing_id") == ident]) == 1


# ---------------------------------------------------------------- #5 ----

def test_5_a_stop_record_that_failed_is_retried(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 10.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    monkeypatch.setattr(runner, "SPEND_CAP_POLL_SECONDS", 0.2)
    acme.script(steps=LONG)
    real = runner.ledger.record_stop
    attempts = []

    def fail_first(ident, node):
        attempts.append(node)
        if len(attempts) == 1:
            raise spendcap.LedgerError("No space left on device")
        return real(ident, node)
    monkeypatch.setattr(runner.ledger, "record_stop", fail_first)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        new, _ = foreign(w, 1.5, caps=DAY_CAP)
        await w.until(aid, timeout=15)
        for _ in range(50):
            if not runner._unrecorded_stops:
                break
            await asyncio.sleep(0.1)
        return aid, new[0]["id"]
    aid, ident = asyncio.run(go())
    assert len(attempts) >= 2
    fresh = spendcap.Ledger(runner.ledger.path)
    fresh.refresh()
    assert aid in fresh.stops.get(ident, []), fresh.stops


# ---------------------------------------------------------------- #6 ----

def test_6_only_the_launched_route_carries_the_deferred_identity(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    beta = w.provider("beta")
    w.agent("worker", "acme", "acme/m1", models={"beta": "beta/b1"})
    w.up()
    acme.costs(0.01)
    beta.costs(0.01)
    runner = w.runner
    entry = runner.tree.defer({"agent": "worker", "task": "later", "timeout": None,
                               "model": None, "workdir": None, "provider": "acme"},
                              time.time() - 5, "quota")
    real = runner._reserve_launch
    crossed = []

    def cross_acme(node_id, provider_name, *args, **kwargs):
        if provider_name == "acme" and not crossed:
            crossed.append(foreign(w, 1.5, caps=DAY_CAP))
        return real(node_id, provider_name, *args, **kwargs)
    monkeypatch.setattr(runner, "_reserve_launch", cross_acme)

    async def go():
        out = await runner.resume_deferred()
        await w.settle()
        return out
    out = asyncio.run(go())
    assert crossed
    carriers = [n for n in runner.tree.read()["nodes"].values()
                if n.get("deferred_id") == entry["id"]]
    assert len(carriers) == 1 and carriers[0]["provider"] == "beta", carriers
    assert [r["agent_id"] for r in out["restarted"]] == [carriers[0]["id"]], out


def test_6_a_crash_after_a_refusal_at_spawn_requeues_the_entry(w):
    runner = capped_world(w)
    entry = runner.tree.defer({"agent": "worker", "task": "later", "timeout": None,
                               "model": None, "workdir": None, "provider": "acme"},
                              time.time() - 5, "quota")
    assert runner.tree.claim_deferred(entry["id"])
    with runner.tree.transaction() as data:     # its drain died: a dead claimer
        for d in data["deferred"]:
            if d.get("id") == entry["id"]:
                d["claim"] = dict(d.get("claim") or {}, pid=2 ** 22 + 12345)
    # the node refused before its spawn, as `_cap_raced_start` leaves it:
    # the identity given up (r4 #1: a node still carrying it launched)
    runner.tree.add(Node(id="ag-refused1", agent="worker", provider="acme",
                         model="acme/m1", parent=None, depth=1, status="refused",
                         deferred_id=""))
    assert runner._recover_stale_restarts() == []
    waiting = [d for d in runner.tree.read()["deferred"] if d.get("id") == entry["id"]]
    assert waiting and waiting[0].get("status", "waiting") == "waiting", waiting
