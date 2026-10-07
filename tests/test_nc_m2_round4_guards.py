"""NC-R17/R26/R80: cancel stops outside the write lock; confirmed death lifts recovery holds."""
import asyncio
import json
import sqlite3
import threading
import uuid
from unittest.mock import AsyncMock

import pytest

from test_nc_m2_guard_recovery import local, deposit, claim, read  # noqa: F401
from multiagents.scheduler.model import create_record


def running(engine, *, locks=()):
    node = deposit(engine, state="running", locks=locks)
    attempt = claim(engine, node, state="launched")
    node["runs"] = [{"run_id": attempt["run_id"], "attempt_id": attempt["attempt_id"],
                     "activation_id": attempt["activation_id"]}]
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    return node, attempt


def cancel(engine, world, node, request_id=None):
    return engine.service.request({
        "op": "cancel_node", "request_id": request_id or uuid.uuid4().hex, "token": world.root_token(),
        "args": {"id": node["id"], "revision": node["revision"]}})


def transitions(engine, node_id):
    with engine.store.transaction(write=False) as db:
        rows = db.execute("SELECT record FROM notifications").fetchall()
    return [r["kind"] for r in (json.loads(row[0]) for row in rows) if r["node_id"] == node_id]


@pytest.mark.parametrize("confirmed", [True, False])
def test_nc_r26_cancel_stops_runs_without_holding_the_write_lock(local, monkeypatch, confirmed):
    world, engine = local
    node, attempt = running(engine)
    entered, release = threading.Event(), threading.Event()
    observed = {}

    class Stopper:
        def __init__(self, *args):
            pass

        async def stop(self, run_id):
            entered.set()
            await asyncio.to_thread(release.wait, 10)
            return {"predecessor_death_confirmed": confirmed}

    monkeypatch.setattr("multiagents.scheduler.engine.Runner", Stopper)
    replies = []
    request_id = uuid.uuid4().hex
    rpc = threading.Thread(target=lambda: replies.append(cancel(engine, world, node, request_id)))
    rpc.start()
    try:
        assert entered.wait(10)
        # While the stop is in flight, another writer gets the lock at once
        # and the request is already durable for the worker to observe.
        db = sqlite3.connect(engine.store.file, timeout=0.5, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        finally:
            db.close()
        observed["changed"] = engine.service.changed.acquire(timeout=0.5)
        if observed["changed"]:
            engine.service.changed.release()
        nodes, journal = read(engine)
        observed["state"] = nodes[node["id"]]["state"]
        observed["requested"] = journal[attempt["attempt_id"]].get("cancel_requested")
    finally:
        release.set()
        rpc.join(10)
    assert observed == {"changed": True, "state": "held", "requested": True}
    reply = replies[0]
    assert reply["ok"], reply
    final = "cancelled" if confirmed else "held"
    assert reply["result"]["state"] == final
    nodes, _ = read(engine)
    assert nodes[node["id"]]["state"] == final
    assert transitions(engine, node["id"]) == ["held", "cancelled" if confirmed else "termination_unconfirmed"]
    # A replay returns the outcome, not the interim hold.
    assert cancel(engine, world, node, request_id) == reply


def test_nc_r80_cancel_with_a_pending_stop_admits_no_sibling_meanwhile(local, monkeypatch):
    world, engine = local
    child, attempt = running(engine)
    sibling = deposit(engine)
    parent = create_record({"kind": "group"}, "root")
    parent["children"] = [child["id"], sibling["id"]]
    with engine.store.transaction() as db:
        for record in (child, sibling):
            record["parent"] = parent["id"]
            engine.store.save_node(db, record)
        engine.store.save_node(db, parent)
    seen = {}

    class Stopper:
        def __init__(self, *args):
            pass

        async def stop(self, run_id):
            nodes, _ = read(engine)
            seen.update({id: nodes[id]["state"] for id in (child["id"], sibling["id"], parent["id"])})
            return {"predecessor_death_confirmed": True}

    monkeypatch.setattr("multiagents.scheduler.engine.Runner", Stopper)
    reply = cancel(engine, world, parent)
    assert reply["ok"], reply
    assert seen == {child["id"]: "held", sibling["id"]: "cancelled", parent["id"]: "cancelled"}
    nodes, _ = read(engine)
    assert {nodes[id]["state"] for id in (child["id"], sibling["id"], parent["id"])} == {"cancelled"}


def test_nc_r26_a_failing_stop_leaves_the_node_held(local, monkeypatch):
    world, engine = local
    node, _ = running(engine)

    class Stopper:
        def __init__(self, *args):
            pass

        async def stop(self, run_id):
            raise RuntimeError("daemon unreachable")

    monkeypatch.setattr("multiagents.scheduler.engine.Runner", Stopper)
    reply = cancel(engine, world, node)
    assert reply["ok"], reply
    assert reply["result"]["state"] == "held"
    assert reply["result"]["hold"]["reason"] == "termination_unconfirmed"


def recover_twice(engine, monkeypatch, confirmed):
    monkeypatch.setattr(engine.runner, "_steer_predecessor", lambda _: object())
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=confirmed))
    asyncio.run(engine.reconcile())
    asyncio.run(engine.reconcile())


@pytest.mark.parametrize("confirmed", [True, False])
def test_nc_r17_confirmed_death_returns_an_unlaunched_claim_to_eligible(local, monkeypatch, confirmed):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    attempt = claim(engine, node)
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    recover_twice(engine, monkeypatch, confirmed)
    nodes, journal = read(engine)
    current = nodes[node["id"]]
    if confirmed:
        assert journal[attempt["attempt_id"]]["state"] == "abandoned"
        assert current["state"] == "open" and not current.get("hold")
        assert engine.view(current, nodes, journal)["eligible"]
    else:
        assert journal[attempt["attempt_id"]]["state"] == "claimed"
        assert current["state"] == "held"
        assert current["hold"]["reason"] == "termination_unconfirmed"


def test_nc_r17_confirmed_death_of_a_launched_run_without_tree_ends_the_node_failed(local, monkeypatch):
    _, engine = local
    node, attempt = running(engine, locks=["schema"])
    recover_twice(engine, monkeypatch, True)
    nodes, journal = read(engine)
    assert journal[attempt["attempt_id"]]["state"] == "abandoned"
    assert nodes[node["id"]]["state"] == "done" and nodes[node["id"]]["outcome"] == "failed"
    waiter = deposit(engine, locks=["schema"])
    nodes, journal = read(engine)
    assert engine.lock_blockers(waiter, nodes, journal) == []


def test_nc_r17_confirmed_death_leaves_a_hold_it_did_not_place(local, monkeypatch):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    attempt = claim(engine, node)
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    monkeypatch.setattr(engine.runner, "_steer_predecessor", lambda _: object())
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=True))
    asyncio.run(engine.reconcile())
    with engine.store.transaction() as db:
        current = engine.store.nodes(db)[node["id"]]
        current.update(hold={"reason": "operator"}, revision=current["revision"] + 1)
        engine.store.save_node(db, current)
    asyncio.run(engine.reconcile())
    nodes, journal = read(engine)
    assert journal[attempt["attempt_id"]]["state"] == "abandoned"
    assert nodes[node["id"]]["state"] == "held"
    assert nodes[node["id"]]["hold"] == {"reason": "operator"}
