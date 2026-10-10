"""RR-R5/R6: stale hold repair and recursive scheduling closure.

The contract explicitly requires seeding inconsistent store state for R5.
Reuse the phase7 host-store fixture for setup only; all assertions read
public RPC node views and the specified transition events.
"""
import asyncio
import copy
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_nc_m2_guard_recovery import local, deposit, claim
from multiagents.scheduler.engine import Engine
from multiagents.scheduler.model import create_record
from nc_fixture.world import err_code, unwrap
from support.nc_harness import stored


def rpc(world, engine, op, **args):
    return engine.service.request({"op": op, "args": args,
                                   "token": world.root_token(), "request_id": uuid4().hex})


def get(world, engine, n):
    reply = rpc(world, engine, "get_node", id=n)
    assert reply.get("ok"), reply
    return unwrap(reply["result"])


def save(engine, *records):
    with engine.store.transaction() as db:
        for record in records:
            engine.store.save_node(db, record)


def pair(engine, *, child_state="held", composite="loop"):
    child = deposit(engine)
    child.update(state=child_state, hold={"reason": "needs_info", "detail": "question"}
                 if child_state == "held" else None)
    sibling = deposit(engine)
    loop = create_record({"kind": composite, "children": [child["id"], sibling["id"]],
                          **({"loop": {"verdict_child": sibling["id"], "max_rounds": 3}}
                             if composite == "loop" else {})}, "root")
    loop.update(state="held", hold={"reason": "child_held", "detail": child["id"]})
    child["parent"] = sibling["parent"] = loop["id"]
    save(engine, child, sibling, loop)
    return loop, child, sibling


@pytest.mark.parametrize("child_state", ["open", "running"])
def test_rr_r5_restart_repairs_stale_current_round_hold_once(local, child_state):
    world, engine = local
    loop, child, sibling = pair(engine, child_state="done")
    child.update(outcome="failed", crash_retry_round=0)
    loop["hold"] = {"reason": "run_failed", "detail": child["id"]}
    # A dependency prevents real launch during the single-tick repair check.
    blocker = deposit(engine, state="held")
    child["depends_on"] = [{"node": blocker["id"], "require": "success"}]
    save(engine, child, loop)
    reply = rpc(world, engine, "relaunch_node", id=child["id"], revision=child["revision"])
    assert reply.get("ok"), reply
    # Preserve whatever durable round record relaunch chose, restoring only
    # the stale loop hold, as after a crash on old code.
    with engine.store.transaction() as db:
        reopened = copy.deepcopy(engine.store.nodes(db)[child["id"]])
        reopened["state"] = child_state
        engine.store.save_node(db, reopened)
        stale = copy.deepcopy(engine.store.nodes(db)[loop["id"]])
        stale.update(state="held", hold={"reason": "run_failed", "detail": child["id"]})
        engine.store.save_node(db, stale)
    engine.loop.close()
    restarted = Engine(engine.service)
    engine.service.engine = restarted
    try:
        asyncio.run(restarted.tick())
        got = get(world, restarted, loop["id"])
        assert got["state"] == "running" and got["hold"] is None, got
        repaired = [e for e in world.events() if world.kind_of(e) == "node.hold_repaired"
                    and loop["id"] in json.dumps(e)]
        assert len(repaired) == 1 and child["id"] in json.dumps(repaired[0]), repaired
        asyncio.run(restarted.tick())
        assert sum(world.kind_of(e) == "node.hold_repaired" and loop["id"] in json.dumps(e)
                   for e in world.events()) == 1
    finally:
        restarted.loop.close()


@pytest.mark.parametrize("case", ["missing", "outside", "changed_round", "done_failed",
                                  "done_approved", "cancelled", "no_relaunch_record"])
def test_rr_r5_unqualified_stale_hold_is_untouched(local, case):
    world, engine = local
    loop, child, sibling = pair(engine, child_state="done")
    child.update(outcome="failed", crash_retry_round=0)
    loop["hold"] = {"reason": "run_failed", "detail": child["id"]}
    save(engine, child, loop)
    assert rpc(world, engine, "relaunch_node", id=child["id"], revision=child["revision"]).get("ok")
    with engine.store.transaction() as db:
        current = copy.deepcopy(engine.store.nodes(db)[child["id"]])
        stale = copy.deepcopy(engine.store.nodes(db)[loop["id"]])
        stale.update(state="held", hold={"reason": "run_failed", "detail": child["id"]})
        if case == "missing":
            stale["hold"]["detail"] = "nd-00000000"
        elif case == "outside":
            outside = create_record({"kind": "simple", "agent": "worker", "task": "outside"}, "root")
            engine.store.save_node(db, outside)
            stale["hold"]["detail"] = outside["id"]
        elif case == "changed_round":
            stale["loop"]["rounds_rejected"] += 1
        elif case.startswith("done_"):
            current.update(state="done", outcome=case[5:])
        elif case == "cancelled":
            current.update(state="cancelled")
        else:
            # Original pre-relaunch record has no new round provenance.
            current = copy.deepcopy(child)
            current.update(state="open", outcome=None)
        engine.store.save_node(db, current)
        engine.store.save_node(db, stale)
    before = stored(get(world, engine, loop["id"]))
    engine.loop.close()
    restarted = Engine(engine.service)
    engine.service.engine = restarted
    try:
        asyncio.run(restarted.tick())
        assert stored(get(world, restarted, loop["id"])) == before
        assert "hold_repaired" not in world.transitions(loop["id"])
    finally:
        restarted.loop.close()


@pytest.mark.parametrize("outcome", ["approved", "failed", "exhausted"])
@pytest.mark.parametrize("kind", ["loop", "sequence", "group"])
def test_rr_r6_close_settles_held_and_open_descendants_with_provenance(local, outcome, kind):
    world, engine = local
    top, child, sibling = pair(engine, composite=kind)
    before = {n["id"]: get(world, engine, n["id"])["revision"] for n in (child, sibling)}
    reply = rpc(world, engine, "close_node", id=top["id"], revision=top["revision"], outcome=outcome)
    assert reply.get("ok"), reply
    for n in (child, sibling):
        got = get(world, engine, n["id"])
        assert got["state"] == "done" and got["outcome"] == outcome, got
        assert got["closure"] == {"by": "root", "via": top["id"], "outcome": outcome}
        assert got["revision"] == before[n["id"]] + 1
        assert got["generations"] == []
        events = [e for e in world.events() if world.kind_of(e) == "node.closed_by_ancestor"
                  and n["id"] in json.dumps(e)]
        assert len(events) == 1
        assert not got.get("blocked"), got


@pytest.mark.parametrize("state", ["claimed", "launched", "captured", "suspended", "resume_pending"])
def test_rr_r6_active_descendant_refuses_close_atomically(local, state):
    world, engine = local
    top, child, sibling = pair(engine)
    attempt = claim(engine, sibling, state="recorded" if state == "resume_pending" else state)
    if state == "resume_pending":
        from multiagents.scheduler.engine import save_attempt
        attempt["resume_pending"] = {"request_id": "pending-resume", "at": 1}
        with engine.store.transaction() as db:
            save_attempt(db, attempt)
    before = [stored(get(world, engine, n["id"])) for n in (top, child, sibling)]
    reply = rpc(world, engine, "close_node", id=top["id"], revision=top["revision"], outcome="failed")
    assert err_code(reply) == "active_runs", reply
    assert [stored(get(world, engine, n["id"])) for n in (top, child, sibling)] == before
    assert all("closed_by_ancestor" not in world.transitions(n["id"]) for n in (child, sibling))


def test_rr_r6_nested_descendants_close_but_already_done_outcomes_are_kept(local):
    world, engine = local
    inner, child, sibling = pair(engine, composite="sequence")
    sibling.update(state="done", outcome="completed")
    outer = create_record({"kind": "group", "children": [inner["id"]]}, "root")
    inner["parent"] = outer["id"]
    save(engine, inner, sibling, outer)
    before = stored(get(world, engine, sibling["id"]))
    assert rpc(world, engine, "close_node", id=outer["id"], revision=outer["revision"], outcome="failed").get("ok")
    assert get(world, engine, child["id"])["closure"]["via"] == outer["id"]
    assert get(world, engine, inner["id"])["state"] == "done"
    assert stored(get(world, engine, sibling["id"])) == before


def test_rr_r6_descendant_historical_generation_gets_no_publication_right(tmp_path, monkeypatch):
    from test_lr_loop_resume import make_world, mkloop
    from nc_fixture.m4_world import commit_entry
    world = make_world(tmp_path, monkeypatch)
    try:
        world.fxw.queue({**commit_entry("work.txt", "unapproved work\n"),
                         "text": "NEED_INFO(q): decide?"})
        world.start_scheduler()
        loop, child, sibling = mkloop(world)
        world.wait_held(loop, "child_held", timeout=5)
        generations = world.get(child)["generations"]
        assert generations, "fixture needs a captured historical generation"
        assert world.root_op("close_node", loop, outcome="approved").get("ok")
        assert world.get(child)["state"] == "done"
        assert world.get(child)["generations"] == generations
        reply = world.root_op("publish_node", child)
        assert not reply.get("ok"), "ancestor closure granted a descendant publication right"
    finally:
        world.close()


@pytest.mark.parametrize("outcome", ["approved", "failed", "exhausted"])
def test_rr_r6_finished_dependencies_are_released_after_descendant_closure(local, outcome):
    world, engine = local
    top, child, sibling = pair(engine)
    dependent = deposit(engine)
    dependent["depends_on"] = [{"node": sibling["id"], "require": "finished"}]
    save(engine, dependent)
    assert not get(world, engine, dependent["id"])["eligible"]
    reply = rpc(world, engine, "close_node", id=top["id"], revision=top["revision"], outcome=outcome)
    assert reply.get("ok"), reply
    got = get(world, engine, dependent["id"])
    assert got["eligible"], got
