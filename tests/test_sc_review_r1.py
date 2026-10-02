"""Regression tests for SC review round 1 (reviewer ag-d45444), one or more
per finding. Contract: `context/specs/spend-caps.md`. Numbers are the
review's finding numbers."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from multiagents import spendcap  # noqa: E402
from multiagents.providers import Event  # noqa: E402
from multiagents.runner import Run  # noqa: E402

LONG = [{"cost": 0.0, "sleep": 0.3}] * 40
CROSS = [{"cost": 1.5, "sleep": 0.4}, {"cost": 0.1, "sleep": 4.0}]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def foreign_crossing(w, cap_usd=1.0, usd=1.5, model="acme/m1", cap_model=""):
    """A crossing recorded by "another process": its own Ledger object on the
    same file, with its own idea of the cap."""
    other = spendcap.Ledger(w.runner.ledger.path)
    new, _ = other.charge(key=json.dumps(["foreign", usd, cap_usd]), provider="acme",
                          model=model, agent="other", node="ag-other1", usd=usd,
                          caps=[spendcap.Cap("acme", cap_model, cap_usd, "day")])
    return new


# ---------------------------------------------------------------- #1 ----

def test_1_a_crossing_recorded_under_another_processes_lower_cap_stops_the_run(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 10.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    monkeypatch.setattr(w.runner, "SPEND_CAP_POLL_SECONDS", 0.3)
    acme.script(steps=LONG)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(1.0)
        assert foreign_crossing(w, cap_usd=1.0), "the foreign crossing was not claimed"
        return aid, (await w.until(aid, timeout=15))[aid]
    aid, state = asyncio.run(go())
    assert state == "limited", state
    assert sc.mentions(w.agent_view(aid), "spend_cap")


def test_1_a_crossing_from_before_a_launch_does_not_stop_it(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 10.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    monkeypatch.setattr(w.runner, "SPEND_CAP_POLL_SECONDS", 0.3)
    foreign_crossing(w, cap_usd=1.0)          # spend 1.5 < 10: admitted
    acme.script(steps=[{"cost": 0.0, "sleep": 0.3}] * 6)

    async def go():
        aid = await w.started("worker", "after")
        return (await w.until(aid, timeout=15))[aid]
    assert asyncio.run(go()) == "done"


# ------------------------------------------------------- #2 / #6 / #7 ----

def test_2_6_7_a_crossing_during_launch_preparation_is_refused_at_spawn_and_deferred(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.costs(0.01)
    runner = w.runner
    real = runner._reserve_launch

    def crossing_lands_now(*args, **kwargs):
        foreign_crossing(w, cap_usd=1.0)
        return real(*args, **kwargs)
    monkeypatch.setattr(runner, "_reserve_launch", crossing_lands_now)
    r = asyncio.run(w.start("worker", "raced"))
    assert acme.spawns() == 0, "a CLI was spawned under a cap spent during preparation"
    assert r.get("deferred") and "spend_cap" in str(r.get("reason")), r
    assert [d for d in w.deferred() if d.get("id") == r["deferred_id"]]
    node = w.node(r["refused_node"])
    assert node.status == "refused" and "spend_cap" in node.reason
    assert r["refused_node"] not in runner._locks, "the supervision lock leaked"
    assert not runner._held(r["refused_node"]), "the launch hold leaked"
    # the startup claim went back: the provider is claimable again
    assert runner.startup.claim("acme", "ag-probe01")


# ---------------------------------------------------------------- #3 ----

def test_3_with_no_cap_a_failed_charge_is_retried_with_the_next_and_never_blocks(w, monkeypatch):
    acme = w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.costs(0.25, 0.5, sleep=0.2)
    ledger = w.runner.ledger
    real = ledger._append
    failures = []

    def fail_once(records):
        if not failures and any(r.get("kind") == "charge" for r in records):
            failures.append(True)
            raise spendcap.LedgerError("disk full, once")
        return real(records)
    monkeypatch.setattr(ledger, "_append", fail_once)

    async def go():
        aid = await w.started("worker", "spend")
        return (await w.until(aid, timeout=20))[aid]
    assert asyncio.run(go()) == "done"
    assert failures
    assert sc.period_spend_is(w.budget(), "acme", "day", 0.75), \
        sc.period_numbers(w.budget(), "acme", "day")


def test_3_with_a_cap_a_failed_charge_stops_the_run_fail_closed(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 100.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.script(steps=[{"cost": 0.25}, {"cost": 0.25, "sleep": 4.0}])

    def fail(records):
        raise spendcap.LedgerError("disk full")
    monkeypatch.setattr(w.runner.ledger, "_append", fail)

    async def go():
        aid = await w.started("worker", "spend")
        return aid, (await w.until(aid, timeout=20))[aid]
    aid, state = asyncio.run(go())
    assert state == "limited", state
    assert sc.mentions(w.agent_view(aid), "spend_cap_unreadable")
    assert acme.finished() == 0


# ---------------------------------------------------------------- #4 ----

@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_4_a_dangling_ledger_link_into_a_read_only_directory_fails_the_probe(tmp_path):
    locked = tmp_path / "ro"
    locked.mkdir()
    home = tmp_path / "data"
    home.mkdir()
    (home / spendcap.LEDGER_NAME).symlink_to(locked / "ledger.jsonl")
    locked.chmod(0o555)
    try:
        with pytest.raises(spendcap.LedgerError):
            spendcap.Ledger(home / spendcap.LEDGER_NAME).probe()
    finally:
        locked.chmod(0o755)


# ---------------------------------------------------------------- #5 ----

def test_5_a_fix_turn_stopped_by_a_cap_is_a_cap_verdict_not_a_provider_failure(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    fed = []
    monkeypatch.setattr(runner.tree, "note_run_outcome",
                        lambda *a, **k: fed.append((a, k)))
    run = Run(node_id="ag-fixturn", provider=runner.providers["acme"],
              spec=runner.config.agent("worker"), fix_turn=True)
    run.cap_stop = {"cause": "spend_cap", "reason": "spend_cap: acme reached",
                    "until": sc.period_end(sc.WED, "day"), "caps": []}
    assert asyncio.run(runner._finalize_fix_turn(run, -15, {})) is True
    assert run.fix_verdict["status"] == "limited"
    assert run.fix_verdict["cap_stop"] is run.cap_stop
    assert not fed, "a cap stop fed the failure breaker"


# ---------------------------------------------------------------- #8 ----

def test_8_a_queued_resume_refused_by_a_cap_keeps_its_place(w):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.script(steps=[{"cost": 0.6}, {"cost": 0.6}, {"cost": 0.6, "sleep": 5.0}])

    async def go():
        aid = await w.started("worker", "long job")
        await w.until(aid, timeout=20)
        entry = {"id": "df-qr0001", "spec": {"op": "resume", "node_id": aid,
                                             "message": "carry on", "provider": "acme"}}
        return await w.runner._pc_dispatch(entry)
    outcome, info = asyncio.run(go())
    assert outcome == "blocked", (outcome, info)
    assert "spend_cap" in str(info)


# ---------------------------------------------------------------- #9 ----

def test_9_a_write_failing_part_way_leaves_nothing_behind(tmp_path, monkeypatch):
    ledger = spendcap.Ledger(tmp_path / "ledger.jsonl")
    ledger.charge(key="a", provider="acme", model="m", agent="x", node="n",
                  usd=0.5, caps=[], at=sc.WED)
    size = ledger.path.stat().st_size
    real = os.write
    calls = []

    def part_then_fail(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real(fd, bytes(data[:10]))
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", part_then_fail)
        with pytest.raises(spendcap.LedgerError):
            ledger.charge(key="b", provider="acme", model="m", agent="x", node="n",
                          usd=0.25, caps=[], at=sc.WED)
    assert ledger.path.stat().st_size == size
    ledger.charge(key="c", provider="acme", model="m", agent="x", node="n",
                  usd=0.125, caps=[], at=sc.WED)
    fresh = spendcap.Ledger(ledger.path)
    fresh.refresh()
    assert fresh.spend("acme", "", "day", sc.WED) == 0.625


# --------------------------------------------------------------- #10 ----

def test_10_a_replayed_cost_is_never_charged(w):
    w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    run = Run(node_id="ag-replay1", provider=runner.providers["acme"],
              spec=runner.config.agent("worker"))
    assert runner._charge(run, Event(kind="step", cost=3.0, step_id="prt_old"),
                          "ses_old", 100, True)
    runner.ledger.refresh()
    assert runner.ledger.charges == []


# --------------------------------------------------------------- #11 ----

def test_11_a_cap_limited_read_only_run_keeps_its_branch_and_worktree(w):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("reader", "acme", "acme/m1", writes=False)
    w.up()
    acme.script(steps=CROSS)

    async def go():
        aid = await w.started("reader", "look")
        await w.until(aid, timeout=25)
        await w.settle()
        return aid
    aid = asyncio.run(go())
    node = w.node(aid)
    assert node.status == "limited"
    assert node.worktree and Path(node.worktree).is_dir(), "the worktree was dropped"
    branches = subprocess.run(["git", "-C", str(w.p.root), "branch", "--list", node.branch],
                              capture_output=True, text=True).stdout
    assert node.branch and node.branch in branches, "the branch was dropped"


# --------------------------------------------------------------- #12 ----

def test_12_a_route_both_capped_and_full_reports_both_causes(w):
    acme = w.provider("acme", max_concurrent=1,
                      spend_cap={"models": {"acme/big": {"usd": 0}}})
    w.agent("small", "acme", "acme/small")
    w.agent("big", "acme", "acme/big")
    w.up()
    acme.script(steps=LONG)

    async def go():
        holder = await w.started("small", "holder")
        r = await w.start("big", "both")
        await w.server.stop_agent(holder)
        await w.settle()
        return r
    r = asyncio.run(go())
    assert r.get("deferred"), r
    assert "spend_cap" in r["reason"] and "provider_concurrency" in r["reason"], r


# --------------------------------------------------------------- #13 ----

def test_13_a_crossing_whose_claimer_died_before_its_event_is_announced_once(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    assert foreign_crossing(w)                 # claimed; no event recorded
    assert not w.spend_events()
    monkeypatch.setattr(w.runner, "ANNOUNCE_GRACE_SECONDS", 0.0)
    w.runner._announce_pending()
    w.runner._announce_pending()
    asyncio.run(w.server.wait_for_agents(timeout=1))
    events = w.spend_events()
    assert len(events) == 1, events
    assert "ag-other1" in json.dumps(events[0])


# --------------------------------------------------------------- #14 ----

def test_14_model_caps_of_two_instances_on_one_model_id_are_both_shown(w):
    w.provider("acme", spend_cap={"models": {"acme/big": {"usd": 1.0}}})
    w.provider("acme2", models=["acme/*"], spend_cap={"models": {"acme/big": {"usd": 2.0}}})
    w.agent("worker", "acme", "acme/big")
    w.up()
    shown = w.budget()["spend"]["providers"]
    assert shown["acme"]["models"]["acme/big"]["cap"]["usd"] == 1.0
    assert shown["acme2"]["models"]["acme/big"]["cap"]["usd"] == 2.0
