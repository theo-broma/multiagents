"""NC-R59 amendment (2026-10-17): root changes `task` only while no attempt of
the node is claimed, launching or running.

Contract: `context/specs/phase7-part1-contract.md`, the amendment bullet after
NC-R59. `update_node` with `task` on such a node is refused `active_runs`,
naming `steer_run` and `relaunch_node`, and changes nothing. Other fields are
unchanged by the amendment.

Two worlds, both deterministic (no wall-clock races):
- in-process engine (`local`, as tests/test_rr_settlement.py): attempts are laid
  into the journal by hand, so "claimed but not yet recorded" is a state, not a
  moment; one test hits the real window through the tick's spawn hook;
- a live scheduler with a gated fixture agent for the running cases.

Assumption where the contract is silent (kept loose): the refusal names the two
ops somewhere in the reply text, not in a particular field.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import asyncio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from nc_fixture.world import World, err_code, unwrap  # noqa: E402
from test_nc_m2_guard_recovery import claim, deposit, local  # noqa: E402,F401
from support.nc_harness import stored  # noqa: E402

WAIT = 8
ORIGINAL = "original task"
EDITED = "edited task"


# ------------------------------------------------------------ in-process

def rpc(world, engine, op, **args):
    return engine.service.request({"op": op, "args": args, "token": world.root_token(),
                                   "request_id": uuid4().hex})


def get(world, engine, node_id):
    reply = rpc(world, engine, "get_node", id=node_id)
    assert reply.get("ok"), reply
    return unwrap(reply["result"])


def assert_refused_untouched(reply, before, after):
    assert err_code(reply) == "active_runs", reply
    text = json.dumps(reply)
    assert "steer_run" in text and "relaunch_node" in text, reply
    assert after == before, "a refused update changed the node"


@pytest.mark.parametrize("attempt_state", ["claimed", "launched"])
def test_r59a_task_update_is_refused_while_an_attempt_is_active(local, attempt_state):
    world, engine = local
    node = deposit(engine)
    node["task"] = ORIGINAL
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    claim(engine, node, state=attempt_state)
    if attempt_state == "launched":
        with engine.store.transaction() as db:
            running = engine.store.nodes(db)[node["id"]]
            running["state"] = "running"
            engine.store.save_node(db, running)
    before = stored(get(world, engine, node["id"]))
    reply = rpc(world, engine, "update_node", id=node["id"],
                revision=before["revision"], task=EDITED)
    assert_refused_untouched(reply, before, stored(get(world, engine, node["id"])))
    assert get(world, engine, node["id"])["task"] == ORIGINAL


def test_r59a_a_refused_task_update_does_not_apply_its_other_fields(local):
    """`changes nothing`: the pins riding along with a refused task are not applied."""
    world, engine = local
    node = deposit(engine)
    claim(engine, node)
    before = stored(get(world, engine, node["id"]))
    reply = rpc(world, engine, "update_node", id=node["id"], revision=before["revision"],
                task=EDITED, urgent=True)
    assert_refused_untouched(reply, before, stored(get(world, engine, node["id"])))


def test_r59a_the_same_update_succeeds_once_the_claim_has_ended(local):
    """Refused while claimed; the same edit succeeds once the claim is abandoned."""
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    rev = get(world, engine, node["id"])["revision"]
    assert err_code(rpc(world, engine, "update_node", id=node["id"], revision=rev,
                        task=EDITED)) == "active_runs"
    from multiagents.scheduler.engine import save_attempt
    attempt.update(state="abandoned", ended_at=1)
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    reply = rpc(world, engine, "update_node", id=node["id"], revision=rev, task=EDITED)
    assert reply.get("ok"), reply
    assert get(world, engine, node["id"])["task"] == EDITED


def test_r59a_an_open_unclaimed_node_accepts_the_task_update(local):
    world, engine = local
    node = deposit(engine)
    rev = get(world, engine, node["id"])["revision"]
    reply = rpc(world, engine, "update_node", id=node["id"], revision=rev, task=EDITED)
    assert reply.get("ok"), reply
    got = get(world, engine, node["id"])
    assert got["task"] == EDITED and got["revision"] == rev + 1


def test_r59a_an_ended_attempt_does_not_block_a_task_update(local):
    """Only claimed/launching/running attempts block; an abandoned claim does not."""
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    from multiagents.scheduler.engine import save_attempt
    attempt.update(state="abandoned", ended_at=1)
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    rev = get(world, engine, node["id"])["revision"]
    reply = rpc(world, engine, "update_node", id=node["id"], revision=rev, task=EDITED)
    assert reply.get("ok"), reply


def test_r59a_a_claimed_node_still_accepts_a_pins_update(local):
    world, engine = local
    node = deposit(engine)
    claim(engine, node)
    rev = get(world, engine, node["id"])["revision"]
    reply = rpc(world, engine, "update_node", id=node["id"], revision=rev,
                pins={"effort": "high"})
    assert reply.get("ok"), reply
    assert get(world, engine, node["id"])["pins"].get("effort") == "high"


def test_r59a_a_task_update_raced_into_the_claim_window_is_refused(tmp_path, monkeypatch):
    """The reported defect: the tick has claimed the attempt and handed it to
    the worker, which has not recorded the launch yet. A task update landing
    in that window is refused and the claim keeps the task (and revision) the
    launch was claimed with."""
    h = Harness(tmp_path, monkeypatch)
    try:
        node = h.record(task=ORIGINAL)
        h.save(node)
        outcome = {}

        async def start(*args, **kwargs):
            return {"admitted": True}

        def spawn(attempt):
            with h.service.store.transaction(write=False) as db:
                token = h.service.store.meta(db, "root_token")
                outcome["claimed"] = h.service.store.nodes(db)[node["id"]]
            outcome["reply"] = h.service.request({
                "op": "update_node", "token": token, "request_id": uuid4().hex,
                "args": {"id": node["id"], "revision": outcome["claimed"]["revision"],
                         "task": EDITED}})
            h.engine.stopped.set()

        monkeypatch.setattr(h.engine.runner, "start", start)
        monkeypatch.setattr(h.engine, "spawn", spawn)
        asyncio.run(h.engine.tick())
        assert "reply" in outcome, "the tick never reached the claim window"
        after = h.nodes()[node["id"]]
        assert err_code(outcome["reply"]) == "active_runs", outcome["reply"]
        assert after["task"] == ORIGINAL
        assert after["revision"] == outcome["claimed"]["revision"]
        claims = [a for a in h.journal().values() if a["node_id"] == node["id"]]
        assert len(claims) == 1 and claims[0]["state"] == "claimed", claims
    finally:
        h.close()


# ------------------------------------------------------------------ live

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.write_config()
    yield world
    world.close()


def started(w, **fields):
    w.start_scheduler()
    node = w.simple("A", prose=ORIGINAL, fx={"gate": "ga"}, **fields)
    w.wait_running(node, WAIT)
    w.wait_spawn("A")
    return node


def test_r59a_task_update_is_refused_while_the_run_is_running(w):
    node = started(w)
    before = w.get(node)
    reply = w.rpc("update_node", {"id": node, "revision": before["revision"], "task": EDITED})
    assert err_code(reply) == "active_runs", reply
    assert "steer_run" in json.dumps(reply) and "relaunch_node" in json.dumps(reply), reply
    after = w.get(node)
    assert after["task"] == before["task"] and after["revision"] == before["revision"]
    assert ORIGINAL in w.fx.by_tag("A")[0]["prompt"] and EDITED not in w.fx.by_tag("A")[0]["prompt"]
    w.gate("ga")
    done = w.wait_state(node, "done", WAIT)
    assert done["task"] == before["task"]
    assert len(w.fx.by_tag("A")) == 1, "the refused update relaunched the node"


def test_r59a_a_pins_update_during_a_run_behaves_as_before(w):
    node = started(w)
    rev = w.get(node)["revision"]
    reply = w.rpc("update_node", {"id": node, "revision": rev, "pins": {"effort": "high"}})
    assert reply.get("ok"), reply
    got = w.get(node)
    assert got["pins"].get("effort") == "high" and got["revision"] == rev + 1
    assert got["state"] == "running"
    w.gate("ga")
    w.wait_state(node, "done", WAIT)
    assert len(w.fx.by_tag("A")) == 1


def test_r59a_relaunch_with_task_works_once_the_run_has_settled(w):
    node = started(w)
    rev = w.get(node)["revision"]
    assert err_code(w.rpc("update_node", {"id": node, "revision": rev, "task": EDITED})) == "active_runs"
    w.gate("ga")
    done = w.wait_state(node, "done", WAIT)
    new_task = "relaunched task [B]\nFX " + json.dumps({"tag": "B"})
    reply = w.rpc("relaunch_node", {"id": node, "revision": done["revision"], "task": new_task})
    assert reply.get("ok"), reply
    w.wait_spawn("B")
    assert "relaunched task" in w.fx.by_tag("B")[0]["prompt"]
    assert w.get(node)["task"] == new_task


def test_r59a_task_update_succeeds_on_an_open_node_that_cannot_launch_yet(w):
    w.start_scheduler()
    blocker = w.simple("H", fx={"gate": "gh"})
    w.wait_running(blocker, WAIT)
    waiting = w.simple("W", prose=ORIGINAL, depends_on=[{"node": blocker}])
    rev = w.get(waiting)["revision"]
    reply = w.rpc("update_node", {"id": waiting, "revision": rev, "task": EDITED})
    assert reply.get("ok"), reply
    assert w.get(waiting)["task"] == EDITED
    w.gate("gh")
