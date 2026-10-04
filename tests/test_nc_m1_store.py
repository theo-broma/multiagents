"""NC-R4, NC-R6, NC-R7, NC-R59 (milestone M1): the node record, validation on
every write, revisions, and which fields a client may edit.

Spoken over the socket of NC-R8, as root unless a test says otherwise.

Assumptions (also in the run report), none of which the contract spells out:
  * a node is created with `{"kind", ...fields, "plan_revision"}` and the reply
    `result` is the node; `list_nodes` replies `{"nodes": [...], "plan_revision": n}`.
    That is the only place a client can learn the plan revision from.
  * a composite is created either with `children: [ids of existing parentless
    nodes]` (which adopts them) or by creating each child with `parent: <id>`.
  * a field the client may not write is either refused or ignored, never stored:
    the contract says "never client-writable" without naming the error.
"""
from __future__ import annotations

import re
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import (ABSENT, ROOT, code, current_revision, live, nc, problems,  # noqa: E402,F401
                        stored, tname)

GOOD_WINDOW = {"days": ["mon", "tue"], "ranges": ["09:00-17:00"]}


def refused(sched, fields, expect="invalid", token=ROOT):
    """A write the contract refuses: right code, and the whole plan unchanged,
    and no transition recorded."""
    before = sched.snapshot()
    seen = len(sched.transitions())
    reply = sched.create_raw(fields, token)
    assert reply.get("ok") is False, reply
    assert code(reply) == expect, reply
    if expect == "invalid":
        assert problems(reply), f"invalid must say what is wrong: {reply}"
    assert sched.snapshot() == before
    assert len(sched.transitions()) == seen
    return reply


def refused_update(sched, node_id, fields, expect="invalid", revision=None, token=ROOT):
    before = sched.snapshot()
    seen = len(sched.transitions())
    reply = sched.update_raw(node_id, revision, token, **fields)
    assert reply.get("ok") is False, reply
    assert code(reply) == expect, reply
    if expect == "invalid":
        assert problems(reply), reply
    assert sched.snapshot() == before
    assert len(sched.transitions()) == seen
    return reply


# ================================================================== NC-R4

def test_nc_r4_a_created_simple_node_has_the_documented_shape_and_defaults(live):
    node = live.create(agent="worker", task="write it")
    assert re.fullmatch(r"nd-[0-9a-f]{8}", node["id"])
    assert node["kind"] == "simple"
    assert (node["agent"], node["task"]) == ("worker", "write it")
    assert node["parent"] is None
    assert node["state"] == "open"
    assert node["hold"] is None and node["outcome"] is None
    assert node["urgent"] is False
    assert node["locks"] == [] and node["depends_on"] == [] and node["inputs"] == []
    assert node["runs"] == [] and node["generations"] == []
    assert node["window"] is None and node["session"] is None
    assert node["template"] is None
    assert node["created_by"] == "root" or "root" in str(node["created_by"]).lower()
    assert node["created_at"]
    assert isinstance(node["revision"], int)


def test_nc_r4_node_ids_are_distinct(live):
    ids = {live.create(task=f"t{i}")["id"] for i in range(6)}
    assert len(ids) == 6


def test_nc_r4_every_writable_field_round_trips_through_get_node(live):
    dep = live.create(task="first")
    inp = live.create(task="source")
    node = live.create(
        task="everything", urgent=True, locks=["runner.py", "db"],
        pins={"model": "acme/m1", "effort": "high", "provider": "acme"},
        depends_on=[{"node": dep["id"], "require": "approved"}],
        inputs=[{"node": inp["id"]}],
        window={"timezone": "UTC", **GOOD_WINDOW})
    got = live.get(node["id"])
    assert got["urgent"] is True
    assert sorted(got["locks"]) == ["db", "runner.py"]
    assert got["pins"] == {"model": "acme/m1", "effort": "high", "provider": "acme"}
    assert got["depends_on"][0]["node"] == dep["id"]
    assert got["depends_on"][0]["require"] == "approved"
    assert got["inputs"][0]["node"] == inp["id"]
    assert got["window"]["days"] == ["mon", "tue"]
    assert got["window"]["ranges"] == ["09:00-17:00"]
    assert got["window"]["timezone"] == "UTC"


@pytest.mark.parametrize("require", ["success", "approved", "finished"])
def test_nc_r4_each_documented_dependency_requirement_is_accepted(live, require):
    dep = live.create(task="d")
    node = live.create(depends_on=[{"node": dep["id"], "require": require}])
    assert live.get(node["id"])["depends_on"][0]["require"] == require


def test_nc_r4_a_dependency_without_require_means_success(live):
    dep = live.create(task="d")
    node = live.create(depends_on=[{"node": dep["id"]}])
    assert live.get(node["id"])["depends_on"][0].get("require", "success") == "success"


def test_nc_r4_an_unknown_dependency_requirement_is_refused(live):
    dep = live.create(task="d")
    refused(live, {"kind": "simple", "agent": "worker", "task": "t",
                   "depends_on": [{"node": dep["id"], "require": "whenever"}]})


@pytest.mark.parametrize("field", ["eligible", "blocked"])
def test_nc_r4_a_derived_field_is_rejected_on_create(live, field):
    before = live.snapshot()
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "t", field: True})
    assert reply.get("ok") is False and code(reply) == "invalid", reply
    assert live.snapshot() == before


@pytest.mark.parametrize("field", ["eligible", "blocked"])
def test_nc_r4_a_derived_field_is_rejected_on_update(live, field):
    node = live.create()
    refused_update(live, node["id"], {field: True})


def test_nc_r4_a_loop_starts_with_a_zero_rejected_counter_and_keeps_its_spec(live):
    a, b = live.create(task="a"), live.create(task="b")
    loop = live.create(kind="loop", children=[a["id"], b["id"]],
                       loop={"verdict_child": b["id"], "max_rounds": 3})
    got = live.get(loop["id"])
    assert got["loop"]["verdict_child"] == b["id"]
    assert got["loop"]["max_rounds"] == 3
    assert got["loop"]["rounds_rejected"] == 0
    assert got["children"] == [a["id"], b["id"]]


@pytest.mark.parametrize("kind", ["sequence", "group"])
def test_nc_r4_composites_keep_their_children_in_order(live, kind):
    ids = [live.create(task=f"t{i}")["id"] for i in range(3)]
    comp = live.create(kind=kind, children=[ids[2], ids[0], ids[1]])
    assert live.get(comp["id"])["children"] == [ids[2], ids[0], ids[1]]


def test_nc_r4_creating_a_composite_gives_its_children_that_parent(live):
    a, b = live.create(task="a"), live.create(task="b")
    comp = live.create(kind="sequence", children=[a["id"], b["id"]])
    assert live.get(a["id"])["parent"] == comp["id"]
    assert live.get(b["id"])["parent"] == comp["id"]


def test_nc_r4_creating_a_child_with_a_parent_lists_it_among_the_parents_children(live):
    seed = live.create(task="seed")
    comp = live.create(kind="sequence", children=[seed["id"]])
    child = live.create(parent=comp["id"], task="inside")
    assert live.get(child["id"])["parent"] == comp["id"]
    assert live.get(comp["id"])["children"] == [seed["id"], child["id"]]


def test_nc_r4_group_children_may_depend_on_each_other(live):
    seed = live.create(task="seed")
    comp = live.create(kind="group", children=[seed["id"]])
    a = live.create(parent=comp["id"], task="a")
    b = live.create(parent=comp["id"], task="b",
                    depends_on=[{"node": a["id"], "require": "success"}])
    assert live.get(b["id"])["depends_on"][0]["node"] == a["id"]


def test_nc_r4_a_nodes_revision_is_one_more_after_each_accepted_edit(live):
    node = live.create()
    r0 = node["revision"]
    r1 = live.update(node["id"], task="one")["revision"]
    r2 = live.update(node["id"], task="two")["revision"]
    assert (r1, r2) == (r0 + 1, r0 + 2)
    assert live.get(node["id"])["revision"] == r2


# ================================================================== NC-R6

def test_nc_r6_unknown_parent(live):
    refused(live, {"kind": "simple", "agent": "worker", "task": "t", "parent": "nd-deadbeef"})


def test_nc_r6_unknown_child(live):
    refused(live, {"kind": "sequence", "children": ["nd-deadbeef"]})


def test_nc_r6_unknown_dependency(live):
    refused(live, {"kind": "simple", "agent": "worker", "task": "t",
                   "depends_on": [{"node": "nd-deadbeef"}]})


def test_nc_r6_unknown_input(live):
    refused(live, {"kind": "simple", "agent": "worker", "task": "t",
                   "inputs": [{"node": "nd-deadbeef"}]})


def test_nc_r6_unknown_verdict_child(live):
    a = live.create(task="a")
    refused(live, {"kind": "loop", "children": [a["id"]],
                   "loop": {"verdict_child": "nd-deadbeef", "max_rounds": 2}})


def test_nc_r6_unknown_references_on_update_are_refused_without_partial_effect(live):
    node = live.create(task="keep me")
    refused_update(live, node["id"], {"task": "new task",
                                      "depends_on": [{"node": "nd-deadbeef"}]})
    assert live.get(node["id"])["task"] == "keep me"


def test_nc_r6_a_dependency_on_itself_is_a_cycle(live):
    node = live.create()
    refused_update(live, node["id"], {"depends_on": [{"node": node["id"]}]})


def test_nc_r6_a_two_node_dependency_cycle(live):
    a, b = live.create(task="a"), live.create(task="b")
    live.update(a["id"], depends_on=[{"node": b["id"]}])
    refused_update(live, b["id"], {"depends_on": [{"node": a["id"]}]})


def test_nc_r6_a_longer_dependency_cycle(live):
    a, b, c = (live.create(task=t) for t in "abc")
    live.update(a["id"], depends_on=[{"node": b["id"]}])
    live.update(b["id"], depends_on=[{"node": c["id"]}])
    refused_update(live, c["id"], {"depends_on": [{"node": a["id"]}]})


def test_nc_r6_a_cycle_through_an_implicit_sequence_edge(live):
    first, second = live.create(task="1"), live.create(task="2")
    live.create(kind="sequence", children=[first["id"], second["id"]])
    # second implicitly depends on first; first depending on second closes the loop
    refused_update(live, first["id"], {"depends_on": [{"node": second["id"]}]})


def test_nc_r6_a_cycle_through_an_ancestor_edge(live):
    seed = live.create(task="seed")
    comp = live.create(kind="group", children=[seed["id"]])
    child = live.create(parent=comp["id"], task="inside")
    # the composite is done only when the child is; the child cannot wait for it
    refused_update(live, child["id"], {"depends_on": [{"node": comp["id"]}]})


def test_nc_r6_an_acyclic_diamond_is_accepted(live):
    top = live.create(task="top")
    left = live.create(task="l", depends_on=[{"node": top["id"]}])
    right = live.create(task="r", depends_on=[{"node": top["id"]}])
    bottom = live.create(task="b", depends_on=[{"node": left["id"]}, {"node": right["id"]}])
    assert len(live.get(bottom["id"])["depends_on"]) == 2


def test_nc_r6_verdict_child_must_be_one_of_the_loops_children(live):
    a, b, outsider = (live.create(task=t) for t in "abc")
    refused(live, {"kind": "loop", "children": [a["id"], b["id"]],
                   "loop": {"verdict_child": outsider["id"], "max_rounds": 2}})


def test_nc_r6_a_loop_without_a_verdict_child_is_refused(live):
    a = live.create(task="a")
    refused(live, {"kind": "loop", "children": [a["id"]], "loop": {"max_rounds": 2}})


@pytest.mark.parametrize("rounds", [0, -1, -100])
def test_nc_r6_max_rounds_below_one_is_refused(live, rounds):
    a = live.create(task="a")
    refused(live, {"kind": "loop", "children": [a["id"]],
                   "loop": {"verdict_child": a["id"], "max_rounds": rounds}})


def test_nc_r6_max_rounds_of_exactly_one_is_accepted(live):
    a = live.create(task="a")
    loop = live.create(kind="loop", children=[a["id"]],
                       loop={"verdict_child": a["id"], "max_rounds": 1})
    assert live.get(loop["id"])["loop"]["max_rounds"] == 1


def test_nc_r6_a_simple_node_without_an_agent_is_refused(live):
    refused(live, {"kind": "simple", "task": "t"})


def test_nc_r6_a_simple_node_without_a_task_is_refused(live):
    refused(live, {"kind": "simple", "agent": "worker"})


def test_nc_r6_an_unknown_agent_is_refused(live):
    refused(live, {"kind": "simple", "agent": "no-such-agent", "task": "t"})


def test_nc_r6_an_unknown_kind_is_refused(live):
    refused(live, {"kind": "banana", "agent": "worker", "task": "t"})


@pytest.mark.parametrize("window", [
    {"timezone": "Mars/Base", **GOOD_WINDOW},
    {"days": ["mon"], "ranges": ["9-17"]},
    {"days": ["mon"], "ranges": ["09:00-17"]},
    {"days": ["mon"], "ranges": ["25:00-26:00"]},
    {"days": ["mon"], "ranges": ["09:60-10:00"]},
    {"days": ["mon"], "ranges": ["09:00-09:00"]},          # start == end
    {"days": ["mon"], "ranges": ["00:00-00:00"]},
    {"days": [], "ranges": ["09:00-17:00"]},
    {"days": ["funday"], "ranges": ["09:00-17:00"]},
])
def test_nc_r6_an_invalid_window_is_refused_on_create(live, window):
    refused(live, {"kind": "simple", "agent": "worker", "task": "t", "window": window})


def test_nc_r6_an_invalid_window_is_refused_on_update(live):
    node = live.create()
    refused_update(live, node["id"], {"window": {"days": ["mon"], "ranges": ["09:00-09:00"]}})


@pytest.mark.parametrize("window", [
    {"days": ["mon"], "ranges": ["22:00-06:00"]},           # crosses midnight
    {"days": ["sat", "sun"], "ranges": ["00:00-24:00"]},    # all day
    {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
     "ranges": ["08:00-12:00", "13:00-17:00"]},
    {"timezone": "America/New_York", "days": ["fri"], "ranges": ["09:30-17:45"]},
])
def test_nc_r6_valid_windows_are_accepted_and_kept(live, window):
    node = live.create(window=window)
    got = live.get(node["id"])["window"]
    assert got["days"] == window["days"] and got["ranges"] == window["ranges"]


def test_nc_r6_a_session_alias_outside_a_template_instance_is_refused(live):
    refused(live, {"kind": "simple", "agent": "worker", "task": "t", "session": "A"})


def test_nc_r6_a_session_alias_cannot_be_added_by_update_outside_an_instance(live):
    node = live.create()
    refused_update(live, node["id"], {"session": "A"})


def test_nc_r6_a_child_cannot_have_two_parents_by_create(live):
    a = live.create(task="a")
    live.create(kind="sequence", children=[a["id"]])
    refused(live, {"kind": "group", "children": [a["id"]]})


def test_nc_r6_a_child_cannot_have_two_parents_by_update(live):
    a = live.create(task="a")
    live.create(kind="sequence", children=[a["id"]])
    spare = live.create(task="spare")
    other = live.create(kind="group", children=[spare["id"]])
    refused_update(live, other["id"], {"children": [spare["id"], a["id"]]})


def test_nc_r6_the_same_child_twice_in_one_composite_is_refused(live):
    a = live.create(task="a")
    refused(live, {"kind": "sequence", "children": [a["id"], a["id"]]})


def test_nc_r6_a_cancelled_node_cannot_be_edited(live):
    node = live.create()
    cancelled = live.ok("cancel_node", {"id": node["id"], "revision": node["revision"]})
    refused_update(live, node["id"], {"task": "too late"}, revision=cancelled["revision"])


def test_nc_r6_a_refused_creation_leaves_no_node_behind(live):
    before = live.snapshot()
    live.create_raw({"kind": "simple", "agent": "worker", "task": "t", "parent": "nd-deadbeef"})
    live.create_raw({"kind": "simple", "agent": "ghost", "task": "t"})
    assert live.snapshot() == before
    assert before["nodes"] == []


# ================================================================== NC-R7

def test_nc_r7_a_stale_node_revision_is_a_conflict_naming_the_current_one(live):
    node = live.create()
    base = node["revision"]
    live.update(node["id"], revision=base, task="first")
    before = live.snapshot()
    reply = live.update_raw(node["id"], base, task="second")
    assert reply.get("ok") is False and code(reply) == "conflict", reply
    assert current_revision(reply) == base + 1
    assert live.snapshot() == before
    assert live.get(node["id"])["task"] == "first"          # nothing merged


def test_nc_r7_a_revision_ahead_of_the_node_is_also_a_conflict(live):
    node = live.create()
    reply = live.update_raw(node["id"], node["revision"] + 5, task="x")
    assert code(reply) == "conflict"
    assert live.get(node["id"])["task"] != "x"


@pytest.mark.parametrize("bad", [None, "1", -1, 1.5])
def test_nc_r7_a_malformed_revision_is_refused_and_changes_nothing(live, bad):
    node = live.create()
    before = live.snapshot()
    reply = live.rpc("update_node", {"id": node["id"], "revision": bad, "task": "x"})
    assert reply.get("ok") is False, reply
    assert live.snapshot() == before


def test_nc_r7_an_edit_without_a_revision_is_refused(live):
    node = live.create()
    before = live.snapshot()
    reply = live.rpc("update_node", {"id": node["id"], "task": "x"})
    assert reply.get("ok") is False
    assert live.snapshot() == before


def test_nc_r7_a_stale_plan_revision_refuses_a_creation(live):
    base = live.plan_revision()
    live.create_raw({"kind": "simple", "agent": "worker", "task": "one", "plan_revision": base})
    before = live.snapshot()
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "two",
                             "plan_revision": base})
    assert reply.get("ok") is False and code(reply) == "conflict", reply
    assert isinstance(current_revision(reply), int) and current_revision(reply) != base
    assert current_revision(reply) == live.plan_revision()
    assert live.snapshot() == before


def test_nc_r7_every_accepted_creation_moves_the_plan_revision(live):
    r0 = live.plan_revision()
    live.create()
    r1 = live.plan_revision()
    live.create()
    r2 = live.plan_revision()
    assert r0 < r1 < r2


def test_nc_r7_a_refused_write_does_not_move_the_plan_revision(live):
    r0 = live.plan_revision()
    live.create_raw({"kind": "simple", "agent": "ghost", "task": "t"})
    assert live.plan_revision() == r0


def test_nc_r7_cancel_with_a_stale_revision_is_a_conflict_and_does_not_cancel(live):
    node = live.create()
    live.update(node["id"], task="moved on")
    reply = live.cancel_raw(node["id"], node["revision"])
    assert code(reply) == "conflict", reply
    assert live.get(node["id"])["state"] == "open"


def test_nc_r7_concurrent_creations_on_one_plan_revision_admit_exactly_one(live):
    base = live.plan_revision()
    replies = live.in_threads([
        (lambda i=i: live.create_raw({"kind": "simple", "agent": "worker", "task": f"t{i}",
                                      "plan_revision": base}))
        for i in range(8)])
    assert all(isinstance(r, dict) for r in replies), replies
    assert sum(1 for r in replies if r.get("ok") is True) == 1
    assert all(code(r) == "conflict" for r in replies if r.get("ok") is not True)
    assert len(live.snapshot()["nodes"]) == 1


def test_nc_r7_concurrent_edits_on_one_node_revision_admit_exactly_one(live):
    node = live.create()
    replies = live.in_threads([
        (lambda i=i: live.update_raw(node["id"], node["revision"], task=f"edit{i}"))
        for i in range(8)])
    assert all(isinstance(r, dict) for r in replies), replies
    winners = [r for r in replies if r.get("ok") is True]
    assert len(winners) == 1
    assert all(code(r) == "conflict" for r in replies if r.get("ok") is not True)
    assert live.get(node["id"])["task"] == winners[0]["result"]["task"]
    assert live.get(node["id"])["revision"] == node["revision"] + 1


def test_nc_r7_a_reply_that_was_sent_is_durable_through_kill_minus_nine(live):
    node = live.create(task="survives")
    edited = live.update(node["id"], task="survives, edited")
    cancel_me = live.create(task="to cancel")
    live.ok("cancel_node", {"id": cancel_me["id"], "revision": cancel_me["revision"]})
    revision = live.plan_revision()
    live.restart(hard=True)
    assert live.get(node["id"])["task"] == "survives, edited"
    assert live.get(node["id"])["revision"] == edited["revision"]
    assert live.get(cancel_me["id"])["state"] == "cancelled"
    assert live.plan_revision() == revision


def test_nc_r7_a_refused_write_is_equally_absent_after_a_restart(live):
    live.create_raw({"kind": "simple", "agent": "ghost", "task": "t"})
    live.restart(hard=True)
    assert live.snapshot()["nodes"] == []


def test_nc_r7_the_plan_survives_a_clean_stop_and_start(live):
    ids = [live.create(task=f"t{i}")["id"] for i in range(3)]
    live.restart()
    assert sorted(n["id"] for n in live.snapshot()["nodes"]) == sorted(ids)


# ================================================================== NC-R59

def test_nc_r59_root_may_edit_each_listed_field(live):
    dep = live.create(task="dep")
    node = live.create(task="old")
    r = live.update(node["id"], task="new task")
    assert live.get(node["id"])["task"] == "new task"
    live.update(node["id"], pins={"model": "acme/m1", "effort": "low"})
    assert live.get(node["id"])["pins"]["effort"] == "low"
    live.update(node["id"], depends_on=[{"node": dep["id"], "require": "finished"}])
    assert live.get(node["id"])["depends_on"][0]["require"] == "finished"
    live.update(node["id"], inputs=[{"node": dep["id"]}])
    assert live.get(node["id"])["inputs"][0]["node"] == dep["id"]
    live.update(node["id"], urgent=True)
    assert live.get(node["id"])["urgent"] is True
    live.update(node["id"], locks=["runner.py"])
    assert live.get(node["id"])["locks"] == ["runner.py"]
    live.update(node["id"], window=GOOD_WINDOW)
    assert live.get(node["id"])["window"]["ranges"] == ["09:00-17:00"]
    live.update(node["id"], window=None)
    assert live.get(node["id"])["window"] is None
    assert live.get(node["id"])["revision"] == r["revision"] + 7


def test_nc_r59_a_partial_edit_leaves_the_other_fields_alone(live):
    node = live.create(task="t", urgent=True, locks=["x"])
    live.update(node["id"], task="t2")
    got = live.get(node["id"])
    assert got["urgent"] is True and got["locks"] == ["x"] and got["agent"] == "worker"


def test_nc_r59_children_of_a_composite_can_be_changed_before_anything_launched(live):
    a, b, c = (live.create(task=t) for t in "abc")
    comp = live.create(kind="sequence", children=[a["id"], b["id"]])
    live.update(comp["id"], children=[b["id"], a["id"], c["id"]])
    assert live.get(comp["id"])["children"] == [b["id"], a["id"], c["id"]]
    assert live.get(c["id"])["parent"] == comp["id"]


def test_nc_r59_max_rounds_can_be_raised_but_not_below_one(live):
    a = live.create(task="a")
    loop = live.create(kind="loop", children=[a["id"]],
                       loop={"verdict_child": a["id"], "max_rounds": 2})
    live.update(loop["id"], loop={"max_rounds": 5})
    got = live.get(loop["id"])["loop"]
    assert got["max_rounds"] == 5 and got["verdict_child"] == a["id"]
    refused_update(live, loop["id"], {"loop": {"max_rounds": 0}})


@pytest.mark.parametrize("field,value", [
    ("state", "done"), ("state", "running"), ("hold", {"reason": "x", "detail": "", "since": 1}),
    ("outcome", "approved"), ("generations", [{"seq": 1}]),
    ("runs", [{"run_id": "r", "attempt_id": "a"}]), ("created_by", "someone"),
    ("created_at", "1999-01-01T00:00:00Z"), ("template", {"name": "x"}),
    ("attempts", [{"attempt_id": "a"}]), ("bindings", {"A": "x"}),
])
def test_nc_r59_scheduler_owned_fields_are_never_client_writable_on_update(live, field, value):
    node = live.create()
    before = live.get(node["id"])
    reply = live.update_raw(node["id"], node["revision"], **{field: value})
    if reply.get("ok") is True:                      # ignored is acceptable; stored is not
        after = live.get(node["id"])
        assert after.get(field) == before.get(field), (field, after.get(field))
    else:
        assert stored(live.get(node["id"])) == stored(before)


@pytest.mark.parametrize("field,value", [
    ("state", "done"), ("outcome", "approved"), ("hold", {"reason": "x", "detail": "", "since": 1}),
    ("revision", 99), ("generations", [{"seq": 1}]), ("runs", [{"run_id": "r", "attempt_id": "a"}]),
    ("created_by", "someone"), ("template", {"name": "x"}),
])
def test_nc_r59_scheduler_owned_fields_are_never_client_writable_on_create(live, field, value):
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "t", field: value})
    if reply.get("ok") is True:
        made = live.get(reply["result"]["id"])
        assert made[field] != value
    else:
        assert live.snapshot()["nodes"] == []


def test_nc_r59_a_cancelled_node_is_not_reopened_by_update(live):
    # `state` is not editable at all: an update naming it cannot reopen a node
    node = live.create()
    live.cancel_raw(node["id"])
    reply = live.update_raw(node["id"], None, state="open")
    assert live.get(node["id"])["state"] == "cancelled"
    assert reply.get("ok") is False


def test_nc_r59_cancel_moves_an_open_node_to_cancelled_without_an_outcome(live):
    node = live.create()
    result = live.ok("cancel_node", {"id": node["id"], "revision": node["revision"]})
    assert result["state"] == "cancelled"
    assert result["outcome"] is None and result["hold"] is None
    assert result["revision"] == node["revision"] + 1


def test_nc_r59_cancel_keeps_the_node_listed_with_its_fields(live):
    node = live.create(task="remember me")
    live.ok("cancel_node", {"id": node["id"], "revision": node["revision"]})
    assert live.get(node["id"])["task"] == "remember me"
    listed = live.ok("list_nodes", {"state": "cancelled"})["nodes"]
    assert [n["id"] for n in listed] == [node["id"]]
