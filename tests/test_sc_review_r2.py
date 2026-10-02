"""Regression tests for SC review round 2 (reviewer ag-eb9628), one or more
per finding, under the decisions SC-R3c and SC-R4c of
`context/specs/spend-caps.md`. Finding #5 was declined (SC-R4b)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sc_harness as sc  # noqa: E402
from multiagents import gitops, spendcap  # noqa: E402
from multiagents.runner import Run, SpendCapRefused  # noqa: E402
from multiagents.tree import Node  # noqa: E402

LONG = [{"cost": 0.0, "sleep": 0.3}] * 40


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def foreign(w, usd, caps=(), key=None, node="ag-other1", provider="acme", model="acme/m1"):
    """A charge recorded by another process: its own Ledger on the same file."""
    other = spendcap.Ledger(w.runner.ledger.path)
    return other.charge(key=key or json.dumps(["foreign", node, usd]), provider=provider,
                        model=model, agent="other", node=node, usd=usd, caps=list(caps))


# ---------------------------------------------------------------- #1 ----

def test_1_admission_reads_the_cap_on_disk_not_the_last_reloaded_one(w):
    acme = w.provider("acme", spend_cap={"usd": 10.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    acme.costs(0.01)
    runner = w.runner
    foreign(w, 1.5)
    # lowered on disk; no server call, so the Runner's own config is still $10
    w.p.providers["acme"]["spend_cap"] = {"usd": 1.0}
    w.p.write()
    r = asyncio.run(runner.start("worker", "after the lowering"))
    assert r.get("deferred") and "spend_cap" in str(r.get("reason")), r
    assert acme.spawns() == 0
    assert runner._cap_refusal("acme", "acme/m1") is not None   # the spawn guard too
    w.p.providers["acme"]["spend_cap"] = {"usd": 50.0}           # raised again
    w.p.write()
    assert runner._cap_refusal("acme", "acme/m1") is None


def test_1_an_invalid_edit_on_disk_keeps_the_cap_in_force(w):
    w.provider("acme", spend_cap={"usd": 0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    assert runner._cap_refusal("acme", "acme/m1") is not None
    w.p.providers["acme"]["spend_cap"] = {"usd": "lots"}
    w.p.write()
    assert runner._cap_refusal("acme", "acme/m1") is not None


# ---------------------------------------------------------------- #2 ----

def test_2_a_charge_that_failed_survives_the_run_and_lands_at_the_next_steer(w, monkeypatch):
    acme = w.provider("acme")
    w.agent("worker", "acme", "acme/m1")
    w.up()
    ledger = w.runner.ledger
    real = ledger._append
    broken = {"on": True}

    def maybe_fail(records):
        if broken["on"] and any(r.get("kind") == "charge" for r in records):
            raise spendcap.LedgerError("disk full")
        return real(records)
    monkeypatch.setattr(ledger, "_append", maybe_fail)
    acme.costs(0.5)

    async def first():
        aid = await w.started("worker", "spend")
        await w.until(aid, timeout=20)
        await w.settle()
        return aid
    aid = asyncio.run(first())
    assert w.runner._pending(node=aid), "the failed charge was not kept past its run"
    broken["on"] = False
    acme.costs(0.25)

    async def steer():
        out = await w.server.steer_agent(aid, "more")
        await w.until(aid, timeout=20)
        await w.settle()
        return out
    assert asyncio.run(steer()).get("steered") is True
    assert sc.period_spend_is(w.budget(), "acme", "day", 0.75), \
        sc.period_numbers(w.budget(), "acme", "day")
    w.runner.__dict__["_pending_seen"] = True
    assert not w.runner._pending(node=aid)


def test_2_a_held_charge_counts_before_a_capped_admission(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    runner._hold_charges([{"key": json.dumps(["held", 1]), "usd": 1.0, "at": sc.WED,
                           "provider": "acme", "model": "acme/m1", "agent": "worker",
                           "node": "ag-gone001"}])
    refusal = runner._cap_refusal("acme", "acme/m1")
    assert refusal is not None and refusal["cause"] == "spend_cap", refusal
    assert not runner._pending(provider="acme")


# ---------------------------------------------------------------- #3 ----

def test_3_a_commit_fix_turn_refused_by_a_cap_ends_the_parent_limited(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    refusal = {"cause": "spend_cap", "reason": "spend_cap: acme reached",
               "until": sc.period_end(sc.WED, "day"), "caps": []}

    async def refuse(**kwargs):
        raise SpendCapRefused(refusal)
    monkeypatch.setattr(runner, "_launch", refuse)
    run = Run(node_id="ag-parent1", provider=runner.providers["acme"],
              spec=runner.config.agent("worker"))
    node = Node(id="ag-parent1", agent="worker", provider="acme", model="acme/m1",
                parent=None, depth=1, status="running", worktree=str(w.p.root))
    failed = gitops.GitResult(ok=False, out="", err="hook says no", code=1, hook="pre-commit")
    _, attempts, ended, _, cut = asyncio.run(
        runner._commit_fix_loop(run, node, failed, "ses_1"))
    assert attempts == 1 and ended == ""
    assert cut is not None and cut["status"] == "limited", cut
    assert cut["cap_stop"] is refusal


# ---------------------------------------------------------------- #4 ----

def test_4_a_capped_queued_retry_stays_blocked_in_its_place(w):
    w.provider("acme", spend_cap={"usd": 0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    entry = runner.tree.enqueue("acme", {"op": "retry", "node_id": "ag-retry01",
                                         "agent": "worker", "session_id": "",
                                         "model": "acme/m1", "task": "t"}, "full")
    runner.tree.add(Node(id="ag-retry01", agent="worker", provider="acme", model="acme/m1",
                         parent=None, depth=1, status="failed", task="t",
                         reason=f"its free retry is queued ({entry['id']})",
                         worktree=str(w.p.root)))
    outcome, info = asyncio.run(runner._pc_dispatch(entry))
    assert outcome == "blocked" and "spend_cap" in str(info), (outcome, info)
    ids = [d.get("id") for d in runner.tree.read()["deferred"]]
    assert entry["id"] in ids, "the queued retry was consumed"
    assert runner.tree.get("ag-retry01").status == "failed"


# ---------------------------------------------------------------- #6 ----

def test_6_a_raced_fresh_start_tries_the_fallback_before_deferring(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 1.0})
    beta = w.provider("beta")
    w.agent("worker", "acme", "acme/m1", models={"beta": "beta/b1"})
    w.up()
    acme.costs(0.01)
    beta.costs(0.01)
    runner = w.runner
    real = runner._reserve_launch
    crossed = []

    def cross_acme(node_id, provider_name, *args, **kwargs):
        if provider_name == "acme" and not crossed:
            crossed.append(foreign(w, 1.5, caps=[spendcap.Cap("acme", "", 1.0, "day")]))
        return real(node_id, provider_name, *args, **kwargs)
    monkeypatch.setattr(runner, "_reserve_launch", cross_acme)

    async def go():
        r = await w.start("worker", "raced")
        await w.settle()
        return r
    r = asyncio.run(go())
    assert crossed
    assert r.get("agent_id") and r.get("provider") == "beta", r
    assert acme.spawns() == 0 and beta.spawns() == 1
    assert w.node(r["refused_node"]).status == "refused"


# ------------------------------------------------------------- #7 / #8 ----

def test_7_a_failed_event_write_leaves_the_crossing_unannounced_for_a_retry(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    monkeypatch.setattr(runner, "ANNOUNCE_GRACE_SECONDS", 0.0)
    assert foreign(w, 1.5, caps=[spendcap.Cap("acme", "", 1.0, "day")])[0]
    real = runner.tree.emit_checked
    calls = []

    def fail_first(*args, **kwargs):
        calls.append(args[1])
        if calls.count(spendcap.CAUSE) == 1 and args[1] == spendcap.CAUSE:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)
    monkeypatch.setattr(runner.tree, "emit_checked", fail_first)
    runner._announce_pending()
    assert not w.spend_events()
    runner._announce_pending()
    runner._announce_pending()
    assert len(w.spend_events()) == 1, w.spend_events()


def test_8_a_recovered_event_lists_the_recorded_stops_not_the_active_runs(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    monkeypatch.setattr(runner, "ANNOUNCE_GRACE_SECONDS", 0.0)
    new, _ = foreign(w, 1.5, caps=[spendcap.Cap("acme", "", 1.0, "day")])
    spendcap.Ledger(runner.ledger.path).record_stop(new[0]["id"], "ag-sibling")
    runner.tree.add(Node(id="ag-newcomer", agent="worker", provider="acme",
                         model="acme/m1", parent=None, depth=1, status="running"))
    runner._announce_pending()
    events = w.spend_events()
    assert len(events) == 1, events
    assert events[0].get("recovered") is True
    assert sorted(events[0]["agents"]) == ["ag-other1", "ag-sibling"], events[0]


# ---------------------------------------------------------------- #9 ----

def test_9_a_pinned_start_refused_by_a_cap_on_a_full_provider_names_both(w):
    acme = w.provider("acme", max_concurrent=1,
                      spend_cap={"models": {"acme/big": {"usd": 0}}})
    w.agent("worker", "acme", "acme/small")
    w.up()
    acme.script(steps=LONG)

    async def go():
        holder = await w.started("worker", "holder")
        r = await w.start("worker", "pinned", model="acme/big")
        await w.server.stop_agent(holder)
        await w.settle()
        return r
    r = asyncio.run(go())
    assert not r.get("agent_id"), r
    text = str(r.get("reason") or r.get("error"))
    assert "spend_cap" in text and "provider_concurrency" in text, r
