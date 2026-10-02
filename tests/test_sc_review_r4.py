"""Regression tests for SC review round 4 (reviewer ag-f0cf70): crash
recovery. Contract: `context/specs/spend-caps.md`, decisions included."""
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
from multiagents.tree import Node  # noqa: E402

LONG = [{"cost": 0.0, "sleep": 0.3}] * 40
DAY_CAP = [spendcap.Cap("acme", "", 1.0, "day")]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    world.down()


def foreign(w, usd, caps=(), node="ag-other1"):
    return spendcap.Ledger(w.runner.ledger.path).charge(
        key=json.dumps(["foreign", node, usd]), provider="acme", model="acme/m1",
        agent="other", node=node, usd=usd, caps=list(caps))


# ---------------------------------------------------------------- #1 ----

def test_1_a_launched_restart_that_ended_refused_is_the_restart(w):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    entry = runner.tree.defer({"agent": "worker", "task": "later", "timeout": None,
                               "model": None, "workdir": None, "provider": "acme"},
                              time.time() - 5, "quota")
    assert runner.tree.claim_deferred(entry["id"])
    with runner.tree.transaction() as data:     # its drain died before bookkeeping
        for d in data["deferred"]:
            if d.get("id") == entry["id"]:
                d["claim"] = dict(d.get("claim") or {}, pid=2 ** 22 + 12345)
    # the restart launched; adoption then finalized it as provider-refused
    runner.tree.add(Node(id="ag-ran0001", agent="worker", provider="acme",
                         model="acme/m1", parent=None, depth=1, status="refused",
                         deferred_id=entry["id"]))
    resolved = runner._recover_stale_restarts()
    assert [r["agent_id"] for r in resolved] == ["ag-ran0001"], resolved
    assert not [d for d in runner.tree.read()["deferred"] if d.get("id") == entry["id"]
                and d.get("status", "waiting") == "waiting"], "requeued: it would run twice"


# ---------------------------------------------------------------- #2 ----

def test_2_a_stop_whose_record_never_landed_is_named_after_a_restart(w, monkeypatch):
    acme = w.provider("acme", spend_cap={"usd": 10.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    monkeypatch.setattr(runner, "SPEND_CAP_POLL_SECONDS", 0.2)
    acme.script(steps=LONG)

    def never(ident, node):
        raise spendcap.LedgerError("No space left on device")
    monkeypatch.setattr(runner.ledger, "record_stop", never)

    async def go():
        aid = await w.started("worker", "long")
        await asyncio.sleep(0.5)
        new, _ = foreign(w, 1.5, caps=DAY_CAP)     # claimed elsewhere, then that server died
        states = await w.until(aid, timeout=15)
        await w.settle()
        return aid, states[aid], new[0]["id"]
    aid, state, ident = asyncio.run(go())
    assert state == "limited"
    assert ident in w.node(aid).spend_cap_crossings
    w.restart_server()                              # the in-memory retry is gone
    fresh = w.runner
    monkeypatch.setattr(fresh, "ANNOUNCE_GRACE_SECONDS", 0.0)
    fresh._announce_pending()
    events = [e for e in w.p.event_records() if e.get("crossing_id") == ident]
    assert len(events) == 1 and events[0].get("recovered") is True, events
    assert aid in events[0]["agents"], events[0]


# ---------------------------------------------------------------- #3 ----

def test_3_a_complete_event_without_its_newline_is_delivered_once(w, monkeypatch):
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    runner = w.runner
    monkeypatch.setattr(runner, "ANNOUNCE_GRACE_SECONDS", 0.0)
    new, _ = foreign(w, 1.5, caps=DAY_CAP)
    ident = new[0]["id"]
    events = runner.paths.events_file
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a") as fh:                    # the writer died before "\n"
        fh.write(json.dumps({"t": time.time(), "agent": "ag-other1", "kind": "spend_cap",
                             "provider": "acme", "crossing_id": ident, "agents": []}))
    runner._announce_pending()
    runner._announce_pending()
    runner.tree.emit("system", "after")             # a later event: a line of its own
    found = [e for e in w.p.event_records() if e.get("crossing_id") == ident]
    assert len(found) == 1, found
    assert [e.get("kind") for e in w.p.event_records()][-1] == "after"
