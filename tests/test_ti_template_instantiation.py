"""TI — `instantiate_template` accepts its own parameters
(context/specs/template-instantiation.md): TI-R1..TI-R5. TI-R6 (the tool's
description) is checked by review.

Assumptions where the contract is silent (kept loose):
- the reply of a successful instantiation carries the root node's `id`
  (as in test_nc_m4_templates); the root is read back with `get_node`.
- a refused call is `{"error": <code>, ...}` through the MCP tool, `{"ok": false, ...}`
  through the RPC; `invalid` carries a `problems` list.
- "same validation as `create_node`" is asserted by comparing the refusal code with
  the one `create_node` gives for the same bad value, never by a hard-coded message.
- the root's `urgent` / `window` / `depends_on` / `locks` read back from `get_node`
  under those names, as `create_node` stores them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.m4_world import M4World, err_code, unwrap  # noqa: E402


def call_tool(w, op: str, **kwargs):
    """An MCP tool function of `multiagents.server`, called as the root orchestrator does."""
    from multiagents import server
    server._reset()
    return getattr(server, op)(**kwargs)

TPL = """\
template: seq2
version: 1
params:
  first: {type: text, default: "one"}
  second: {type: text, default: "two"}
root:
  key: top
  kind: sequence
  children:
    - {key: one, kind: simple, agent: worker, task: {param: first}}
    - {key: two, kind: simple, agent: worker, task: {param: second}}
"""

WAIT = 8            # one launch / one scheduler reaction
QUIET = 2.5         # how long "does not launch" is observed (ticks are 0.1 s)
DAY = {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "ranges": ["09:00-17:00"]}
BAD_WINDOWS = {
    "unknown_zone": {**DAY, "timezone": "Mars/Base"},
    "start_equals_end": {**DAY, "ranges": ["09:00-09:00"]},
    "empty_days": {**DAY, "days": []},
}


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.start_scheduler()
    world.register_ok(TPL)
    yield world
    world.close()


def params(tag: str, **fx) -> dict:
    return {"first": task(f"{tag}1", "first", **fx), "second": task(f"{tag}2", "second")}


def snapshot(w):
    res = w.ok("list_nodes", {})
    return res["plan_revision"], sorted(n["id"] for n in res["nodes"])


def tool_inst(w, args: dict, revision: int | None = None) -> dict:
    """The MCP tool function, as the orchestrator calls it."""
    rev = w.plan_revision() if revision is None else revision
    return call_tool(w, "instantiate_template", name="seq2", plan_revision=rev, **args)


def root_of(reply: dict) -> str:
    assert "error" not in reply, reply
    top = unwrap(reply)
    return top["id"] if "id" in top else top["root"]


def tool_create(w, **fields) -> dict:
    return call_tool(w, "create_node", plan_revision=w.plan_revision(), **fields)


def dep(node: str) -> dict:
    return {"node": node}


def holder(w, lock: str | None = None, name: str = "h") -> str:
    extra = {"locks": [lock]} if lock else {}
    node = w.simple("HOLD", fx={"gate": name}, **extra)
    w.wait_running(node, timeout=WAIT)
    return node


def never_launched(w, tag: str) -> None:
    w.quiet(QUIET)
    assert w.fx.by_tag(tag) == [], f"{tag} launched although it must stay blocked"


def refused_on(reply: dict, field: str) -> None:
    """Refused as `invalid` for a problem about `field` itself, not for being un-writable."""
    assert code_of(reply) == "invalid", reply
    problems = [str(p) for p in reply.get("problems", [])]
    assert any(p.startswith(field) for p in problems), reply
    assert not any("not client-writable" in p for p in problems), reply


def code_of(reply: dict) -> str | None:
    return reply.get("error") if isinstance(reply.get("error"), str) else err_code(reply)


# ------------------------------------------------------------------ TI-R1

def test_ti_r1_the_mcp_tool_with_defaults_deposits_the_instance(w):
    before = snapshot(w)
    reply = call_tool(w, "instantiate_template", name="seq2", params=params("D"),
                      plan_revision=before[0])
    top = root_of(reply)
    node = w.get(top)
    assert node["kind"] == "sequence" and len(node["children"]) == 2
    assert snapshot(w)[1] != before[1]


def test_ti_r1_the_instance_deposited_by_the_tool_runs(w):
    root_of(tool_inst(w, {"params": params("R")}))
    w.wait_spawn("R1")


def test_ti_r1_the_default_tool_call_leaves_urgent_false_and_no_window(w):
    node = w.get(root_of(tool_inst(w, {"params": params("U")})))
    assert node["urgent"] is False
    assert not node.get("window")


def test_ti_r1_the_rpc_without_optional_arguments_still_deposits(w):
    assert w.instantiate_ok("seq2", params("P"))


# ------------------------------------------------------------------ TI-R2

def test_ti_r2_urgent_applies_to_the_root_through_the_tool(w):
    top = root_of(tool_inst(w, {"params": params("A"), "urgent": True}))
    assert w.get(top)["urgent"] is True


def test_ti_r2_urgent_applies_to_the_root_through_the_rpc(w):
    top = w.instantiate_ok("seq2", params("B"), urgent=True)
    assert w.get(top)["urgent"] is True


def test_ti_r2_urgent_has_the_meaning_it_has_on_a_composite_create_node(w):
    kid = w.simple("K")
    made = unwrap(tool_create(w, kind="sequence", children=[kid], urgent=True))
    assert w.get(made["id"])["urgent"] is True       # the reference behaviour
    top = root_of(tool_inst(w, {"params": params("C"), "urgent": True}))
    assert w.get(top)["urgent"] == w.get(made["id"])["urgent"]


def test_ti_r2_explicit_defaults_are_the_same_as_omitting_them(w):
    omitted = w.get(root_of(tool_inst(w, {"params": params("E1")})))
    explicit = w.get(root_of(tool_inst(w, {"params": params("E2"), "urgent": False,
                                           "window": None})))
    for key in ("urgent", "window", "depends_on", "inputs", "locks", "kind"):
        assert omitted.get(key) == explicit.get(key), key


def test_ti_r2_a_window_applies_to_the_root(w):
    top = root_of(tool_inst(w, {"params": params("W"), "window": DAY}))
    assert w.get(top)["window"]["ranges"] == DAY["ranges"]


@pytest.mark.parametrize("name", sorted(BAD_WINDOWS))
def test_ti_r2_an_invalid_window_is_refused_as_create_node_refuses_it(w, name):
    kid = w.simple("K")
    reference = tool_create(w, kind="sequence", children=[kid], window=BAD_WINDOWS[name])
    assert code_of(reference) == "invalid", reference
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("X"), "window": BAD_WINDOWS[name]})
    refused_on(reply, "window")
    assert snapshot(w) == before


def test_ti_r2_a_non_boolean_urgent_is_refused_and_creates_nothing(w):
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("N"), "urgent": "yes"})
    refused_on(reply, "urgent")
    assert snapshot(w) == before


# ------------------------------------------------------------------ TI-R3

def test_ti_r3_depends_on_an_unfinished_node_launches_no_child_until_it_is_done(w):
    h = holder(w)
    top = root_of(tool_inst(w, {"params": params("DEP"), "depends_on": [dep(h)]}))
    assert [d["node"] for d in w.get(top)["depends_on"]] == [h]
    never_launched(w, "DEP1")
    w.gate("h")
    w.wait_spawn("DEP1")


def test_ti_r3_a_lock_held_by_another_run_blocks_every_child(w):
    holder(w, lock="L")
    top = root_of(tool_inst(w, {"params": params("LK"), "locks": ["L"]}))
    assert w.get(top)["locks"] == ["L"]
    never_launched(w, "LK1")
    w.gate("h")
    w.wait_spawn("LK1")


def test_ti_r3_inputs_are_recorded_on_the_root_as_create_node_records_them(w):
    src = w.simple("SRC")
    w.wait_state(src, "done", timeout=WAIT)
    kid = w.simple("K")
    ref = [{"node": src}]
    made = unwrap(tool_create(w, kind="sequence", children=[kid], inputs=ref))
    top = root_of(tool_inst(w, {"params": params("IN"), "inputs": ref}))
    assert w.get(top)["inputs"] == w.get(made["id"])["inputs"] != []


BAD_REFS = {
    "depends_on": [{"node": "nd-00000000"}],
    "inputs": [{"node": "nd-00000000"}],
    "locks": [""],
}


@pytest.mark.parametrize("field", sorted(BAD_REFS))
def test_ti_r3_invalid_values_are_refused_as_create_node_refuses_them(w, field):
    kid = w.simple("K")
    reference = tool_create(w, kind="sequence", children=[kid], **{field: BAD_REFS[field]})
    assert "error" in reference, reference
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("BAD"), field: BAD_REFS[field]})
    assert code_of(reply) == code_of(reference), (reply, reference)
    assert snapshot(w) == before


# ------------------------------------------------------------------ TI-R4

def test_ti_r4_no_child_launches_before_the_dependency_is_applied_under_fast_ticks(w):
    h = holder(w)
    tops = [root_of(tool_inst(w, {"params": params(f"AT{i}"), "depends_on": [dep(h)]}))
            for i in range(4)]
    for i in range(4):
        assert w.fx.by_tag(f"AT{i}1") == []
    w.quiet(QUIET)
    for i in range(4):
        assert w.fx.by_tag(f"AT{i}1") == [], i
    assert all(w.get(t)["depends_on"] for t in tops)


def test_ti_r4_no_child_launches_before_the_lock_is_applied_under_fast_ticks(w):
    holder(w, lock="L")
    for i in range(4):
        root_of(tool_inst(w, {"params": params(f"AL{i}"), "locks": ["L"]}))
    w.quiet(QUIET)
    for i in range(4):
        assert w.fx.by_tag(f"AL{i}1") == [], i


def test_ti_r4_a_refused_instantiation_launches_nothing_even_after_ticks(w):
    holder(w, lock="L")
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("RF"), "locks": ["L"], "window": BAD_WINDOWS["empty_days"]})
    refused_on(reply, "window")
    w.quiet(1)
    assert snapshot(w) == before and w.fx.by_tag("RF1") == []


# ------------------------------------------------------------------ TI-R5

def test_ti_r5_an_unknown_argument_is_still_not_client_writable_over_the_rpc(w):
    before = snapshot(w)
    reply = w.instantiate("seq2", params("Z"), bogus=1)
    assert err_code(reply) == "invalid", reply
    assert "not client-writable" in str(reply), reply
    assert snapshot(w) == before


@pytest.mark.parametrize("field,value", [("state", "done"), ("kind", "simple"),
                                         ("created_by", "x"), ("revision", 9)])
def test_ti_r5_node_fields_other_than_the_granted_ones_stay_refused(w, field, value):
    before = snapshot(w)
    reply = w.instantiate("seq2", params("Y"), **{field: value})
    assert err_code(reply) == "invalid" and "not client-writable" in str(reply), reply
    assert snapshot(w) == before


def test_ti_r5_a_stale_plan_revision_is_a_conflict_through_the_tool(w):
    stale = w.plan_revision()
    w.simple("BUMP")
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("S")}, revision=stale)
    assert code_of(reply) == "conflict", reply
    assert snapshot(w) == before


def test_ti_r5_a_stale_plan_revision_is_still_a_conflict_with_the_new_arguments(w):
    stale = w.plan_revision()
    w.simple("BUMP")
    before = snapshot(w)
    reply = tool_inst(w, {"params": params("S2"), "urgent": True, "locks": ["L"]}, revision=stale)
    assert code_of(reply) == "conflict", reply
    assert snapshot(w) == before
