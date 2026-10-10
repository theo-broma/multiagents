"""RR-R1/R2: relaunch arguments are applied before the next admission.

Uses LR's real provider/scheduler world. Assertions concern RPC replies,
node views and what the provider actually receives, not admission internals.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lr_loop_resume import make_world, worker_failed, work
from test_tb_a_relaunch_simple import failed_simple, refused, tool_relaunch
from nc_fixture.m4_world import err_code, verdict_entry
from support.nc_harness import stored

WAIT = 5


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, session_model_change=True)
    yield world
    world.close()


def snapshot(w, *ids):
    return [stored(w.get(n)) for n in ids]


def test_rr_r1_mcp_task_reaches_the_first_relaunched_attempt(w):
    n = failed_simple(w)
    w.fxw.queue({"gate": "retry"})
    try:
        try:
            reply = tool_relaunch(w, n, task="NEW-TASK-RR-901")
        except TypeError as exc:
            pytest.fail(f"relaunch_node MCP does not accept task: {exc}")
        assert not refused(reply), reply
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="patched attempt")
        assert "NEW-TASK-RR-901" in w.fxw.calls()[-1]["prompt"]
        assert "TASK-PROSE-XYZ" not in w.fxw.calls()[-1]["prompt"]
        assert w.get(n)["task"] == "NEW-TASK-RR-901"
    finally:
        w.gate("retry", w.fxw)


def test_rr_r1_without_task_the_original_task_is_kept(w):
    n = failed_simple(w)
    w.fxw.queue({"gate": "retry"})
    try:
        assert not refused(tool_relaunch(w, n))
        w.until(lambda: len(w.fxw.calls()) == 2, WAIT, what="unchanged task attempt")
        assert "TASK-PROSE-XYZ" in w.fxw.calls()[-1]["prompt"]
    finally:
        w.gate("retry", w.fxw)


def test_rr_r1_loop_task_is_invalid_names_children_and_changes_nothing(w):
    loop, wk, rv = worker_failed(w)
    before = snapshot(w, loop, wk, rv)
    reply = w.root_op("relaunch_node", loop, task="wrong surface")
    assert err_code(reply) == "invalid", reply
    assert "children" in json.dumps(reply), reply
    assert snapshot(w, loop, wk, rv) == before


def test_rr_r2_patched_tasks_and_pins_reach_first_admission(w):
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "retry", **work(3)})
    w.fxr.queue(verdict_entry("approved"))
    try:
        reply = w.root_op("relaunch_node", loop, children={
            wk: {"revision": w.get(wk)["revision"], "task": "PATCHED-WORK-RR",
                 "pins": {"model": "fxw/m2"}},
            rv: {"revision": w.get(rv)["revision"], "task": "PATCHED-REVIEW-RR"}})
        assert reply.get("ok"), reply
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="first patched launch")
        call = w.fxw.calls()[-1]
        assert "PATCHED-WORK-RR" in call["prompt"] and call["model"] == "fxw/m2"
        assert w.get(rv)["task"] == "PATCHED-REVIEW-RR"
        w.gate("retry", w.fxw)
        w.until(lambda: w.fxr.calls(), WAIT, what="patched reviewer")
        assert "PATCHED-REVIEW-RR" in w.fxr.calls()[0]["prompt"]
    finally:
        w.gate("retry", w.fxw)


@pytest.mark.parametrize("bad", ["stale", "unknown", "composite", "outside", "field",
                                  "revision_missing", "revision_bool", "task_type", "pins_type",
                                  "session_type", "duplicate_pins", "duplicate_session"])
def test_rr_r2_invalid_child_patch_is_atomic(w, bad):
    loop, wk, rv = worker_failed(w)
    # A valid patch precedes the invalid one, including alias renewal.
    other = w.simple("OTHER", "wk", depends_on=[{"node": loop, "require": "success"}])
    children = {wk: {"revision": w.get(wk)["revision"], "task": "MUST-NOT-COMMIT",
                     "new_session": True},
                rv: {"revision": w.get(rv)["revision"], "task": "new review"}}
    extra = {}
    if bad == "stale":
        children[rv]["revision"] -= 1
    elif bad in {"unknown", "composite", "outside"}:
        children[{"unknown": "nd-00000000", "composite": loop, "outside": other}[bad]] = children.pop(rv)
    elif bad == "field":
        children[rv]["unexpected"] = True
    elif bad == "revision_missing":
        del children[rv]["revision"]
    elif bad == "revision_bool":
        children[rv]["revision"] = True
    elif bad == "task_type":
        children[rv]["task"] = 12
    elif bad == "pins_type":
        children[rv]["pins"] = []
    elif bad == "session_type":
        children[rv]["new_session"] = "yes"
    elif bad == "duplicate_pins":
        extra["pins"] = {wk: {"model": "fxw/m2"}}
    else:
        extra["new_session"] = [wk]
    before = snapshot(w, loop, wk, rv, other)
    reply = w.root_op("relaunch_node", loop, children=children, **extra)
    assert err_code(reply) == ("conflict" if bad == "stale" else "invalid"), reply
    assert snapshot(w, loop, wk, rv, other) == before
    # Alias renewal must also have rolled back: a subsequent valid retry resumes it.
    session = w.fxw.calls()[-1]["session"]
    w.fxw.queue({"gate": "retry"})
    try:
        assert w.root_op("relaunch_node", loop).get("ok")
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="retry after refusal")
        assert w.fxw.calls()[-1]["resume"] == session
    finally:
        w.gate("retry", w.fxw)


def test_rr_r2_simple_target_refuses_children_without_change(w):
    n = failed_simple(w)
    before = snapshot(w, n)
    reply = w.root_op("relaunch_node", n, children={})
    assert err_code(reply) == "invalid", reply
    assert snapshot(w, n) == before


def test_rr_r2_active_descendant_is_invalid_and_changes_nothing(w):
    loop, wk, rv = worker_failed(w)
    # A second branch under a held loop cannot be admitted naturally. Seed the
    # explicit active-attempt condition through the existing host store seam.
    from multiagents.scheduler.rpc import Service
    from multiagents.scheduler.engine import save_attempt
    from multiagents.tree import now
    from uuid import uuid4
    store = Service(w.root, now()).store
    with store.transaction() as db:
        save_attempt(db, {"attempt_id": uuid4().hex, "activation_id": uuid4().hex,
                          "node_id": rv, "run_id": "ag-active", "state": "claimed",
                          "locks": [], "at": now()})
    before = snapshot(w, loop, wk, rv)
    reply = w.root_op("relaunch_node", loop, children={rv: {"revision": w.get(rv)["revision"], "task": "new"}})
    assert err_code(reply) == "invalid", reply
    assert snapshot(w, loop, wk, rv) == before


def test_rr_r2_child_new_session_renews_the_alias(w):
    loop, wk, rv = worker_failed(w)
    old_session = w.fxw.calls()[-1]["session"]
    w.fxw.queue({"gate": "retry"})
    try:
        reply = w.root_op("relaunch_node", loop, children={wk: {
            "revision": w.get(wk)["revision"], "new_session": True}})
        assert reply.get("ok"), reply
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="renewed session")
        assert w.fxw.calls()[-1]["resume"] is None
        assert w.fxw.calls()[-1]["session"] != old_session
    finally:
        w.gate("retry", w.fxw)


def test_rr_r2_mcp_accepts_children_patch(w):
    from nc_fixture.world import call_tool
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "retry"})
    try:
        try:
            reply = call_tool(w, "relaunch_node", loop, w.get(loop)["revision"],
                              children={wk: {"revision": w.get(wk)["revision"], "task": "MCP-CHILD-PATCH"}})
        except TypeError as exc:
            pytest.fail(f"relaunch_node MCP does not accept children: {exc}")
        assert not refused(reply), reply
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="MCP child patch launch")
        assert "MCP-CHILD-PATCH" in w.fxw.calls()[-1]["prompt"]
    finally:
        w.gate("retry", w.fxw)


def test_rr_r2_new_session_patch_renews_the_binding_shared_by_siblings(w):
    from test_lr_loop_resume import PAIR, FAIL
    w.agent("rv", "fxw", writes=True)
    w.fxw.queue(FAIL, FAIL)
    w.start_scheduler()
    w.register_ok(PAIR.replace("session: B", "session: A"))
    loop = w.instantiate_ok("lrpair")
    wk, rv = w.children(loop)
    w.wait_held(loop, "run_failed", timeout=WAIT)
    old = w.fxw.calls()[-1]["session"]
    w.fxw.queue({"gate": "retry"}, verdict_entry("approved"))
    try:
        reply = w.root_op("relaunch_node", loop, children={wk: {
            "revision": w.get(wk)["revision"], "new_session": True}})
        assert reply.get("ok"), reply
        w.until(lambda: len(w.fxw.calls()) == 3, WAIT, what="fresh shared worker session")
        fresh = w.fxw.calls()[-1]["session"]
        assert fresh != old and w.fxw.calls()[-1]["resume"] is None
        w.gate("retry", w.fxw)
        w.until(lambda: len(w.fxw.calls()) == 4, WAIT, what="sibling sharing renewed binding")
        assert w.fxw.calls()[-1]["resume"] == fresh
    finally:
        w.gate("retry", w.fxw)
