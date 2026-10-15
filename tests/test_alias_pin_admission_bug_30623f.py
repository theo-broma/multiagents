"""bug-30623f part 1: current pins outrank a previous alias activation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from multiagents.config import AgentSpec, Config
from multiagents.scheduler import sessions
from multiagents.tree import Tree
from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry


PAIR = """\
template: alias-pin
version: 1
params: {}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: 3
  children:
    - {key: wrk, kind: simple, agent: wk, task: implement}
    - {key: rev, kind: simple, agent: rv, task: review, session: B, pins: {model: fxr/m1}}
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr", family="review", session_model_change=True)
    world.fxb = world.provider("fxb", family="review")
    world.fxc = world.provider("fxc", family="other")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True,
                models={"fxb": "fxb/m1", "fxc": "fxc/m1"})
    yield world
    world.close()


def pending_round(w, *, dirty=False, initial="fxr"):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"),
                commit_entry("b.txt", "two\n", "r2", tag="W2", gate="work"))
    w.providers[initial].queue(verdict_entry(
        "rejected", [finding("redo")], tag="R1",
        **({"leave": {"scratch.txt": "keep this\n"}} if dirty else {})))
    w.fxr.queue(verdict_entry("approved", tag="R2"))
    w.fxb.queue(verdict_entry("approved", tag="B2"))
    w.fxc.queue(verdict_entry("approved", tag="C2"))
    w.start_scheduler()
    w.register_ok(PAIR.replace("pins: {model: fxr/m1}", "pins: {model: " + initial + "/m1}"))
    top = w.instantiate_ok("alias-pin")
    wk, rv = w.children(top)
    w.wait_spawn("W2", w.fxw)
    assert w.get(rv)["state"] == "open" and len(w.get(rv)["runs"]) == 1
    assert w.root_op("update_node", rv, pins={}).get("ok")
    bound = next(b for b in w.status()["aliases"] if b["alias"] == "B")
    return top, rv, bound


def pin(w, rv, **pins):
    reply = w.root_op("update_node", rv, pins=pins)
    assert reply.get("ok"), reply
    return reply["result"]


def finish(w, top):
    w.gate("work", w.fxw)
    assert w.wait_state(top, "done", timeout=25)["outcome"] == "approved"


@pytest.mark.parametrize("pins,target", [
    ({"provider": "fxb"}, "fxb"),
    ({"provider": "fxb", "model": "fxb/m1"}, "fxb"),
    ({"provider": "fxc", "model": "fxc/m1"}, "fxc"),
    ({"model": "fxc/m1"}, "fxc"),
    ({"model": "fxr/m2"}, "fxr"),
])
def test_r1_current_pin_renews_at_admission_and_archives_the_old_session(w, pins, target):
    top, rv, before = pending_round(w)
    old_home = w.paths.home(before["home_id"])
    old_home.mkdir(parents=True, exist_ok=True)
    marker = old_home / "old-session-store"
    marker.write_text("previous conversation\n")
    # An exhausted old account must not prevent the requested replacement.
    if target != "fxr":
        w.fxr.entry["enabled"] = False
        w.write_config()
    pin(w, rv, **pins)
    finish(w, top)
    calls = w.providers[target].calls()
    second = calls[-1]
    assert second["resume"] is None
    assert second["session"] != w.fxr.calls()[0]["session"]
    assert second["model"] == pins.get("model", "fxb/m1")
    after = next(b for b in w.status()["aliases"] if b["id"] == before["id"])
    assert after["provider"] == target and after["reason"] == "provider_changed"
    assert w.get(rv)["runs"][-1]["reason"] == "provider_changed"
    assert marker.read_text() == "previous conversation\n"
    assert any(a["session_id"] == before["session_id"] and a["home_id"] == before["home_id"]
               for a in after["archives"])


@pytest.mark.parametrize("field,value", [("container_account", "replacement-account"),
                                         ("family", "replacement-family")])
def test_r1_pin_also_renews_when_the_provider_name_keeps_a_different_identity(w, field, value):
    top, rv, before = pending_round(w)
    w.fxr.entry[field] = value
    w.write_config()
    pin(w, rv, provider="fxr")
    finish(w, top)
    assert w.fxr.calls()[1]["resume"] is None
    after = next(b for b in w.status()["aliases"] if b["id"] == before["id"])
    assert after["reason"] == "provider_changed"
    assert after["account" if field == "container_account" else "family"] == value


@pytest.mark.parametrize("unavailable", ["disabled", "cooldown"])
def test_r2_refusal_names_the_pinned_provider_and_keeps_the_old_binding(w, unavailable):
    top, rv, before = pending_round(w)
    if unavailable == "disabled":
        w.fxb.entry["enabled"] = False
        w.write_config()
    else:
        Tree(w.paths.tree_file, w.paths.events_file).set_cooldown(
            "fxb", time.time() + 600, "test cooling down")
    pin(w, rv, provider="fxb")
    w.gate("work", w.fxw)
    blocked = w.until(lambda: w.get(rv)["blocked"] and w.get(rv), timeout=15,
                      what="the pinned provider refusal")
    # Wait until the previous worker's structural gate is gone.
    blocked = w.until(lambda: any(b["code"].startswith("admission:")
                                 for b in w.get(rv)["blocked"]) and w.get(rv),
                      timeout=15, what="admission refusal")
    assert "fxb" in json.dumps(blocked["blocked"])
    assert "fxr and all fallbacks" not in json.dumps(blocked["blocked"])
    assert w.fxb.calls() == []
    assert next(b for b in w.status()["aliases"] if b["id"] == before["id"]) == before


@pytest.mark.parametrize("state", ["claimed", "launched", "captured", "suspended"])
def test_r3_a_live_conversation_blocks_renewal_with_the_binding_reason(state):
    node = {"id": "child", "parent": None, "session": "B",
            "template": {"instance": "instance"}, "agent": "rv",
            "pins": {"provider": "fxb"}}
    nodes = {node["id"]: node}
    key = sessions.alias_id(node, nodes)
    bound = {"id": key, "provider": "fxr", "model": "fxr/m1", "account": "fxr"}
    config = Config(project={}, agents={"rv": AgentSpec("rv", "fxr", "fxr/m1")},
                    providers={"fxr": {}, "fxb": {}}, models={}, instruction_dirs=[])
    runner = SimpleNamespace(config=config)
    journal = {"other": {"alias_id": key, "state": state}}
    before = copy.deepcopy(bound)
    assert sessions.blockers(node, nodes, journal, {key: bound}, runner) == [
        {"code": "session_binding", "detail": "fxr"}]
    assert bound == before


def test_r1_dirty_checkout_still_holds_before_the_binding_can_be_renewed(w):
    top, rv, before = pending_round(w, dirty=True)
    # The old account cannot run the one permitted cleanup turn.
    w.fxr.entry["enabled"] = False
    w.write_config()
    pin(w, rv, provider="fxb")
    w.gate("work", w.fxw)
    w.wait_held(rv, "dirty_worktree", timeout=25)
    assert w.fxb.calls() == []
    assert (Path(before["worktree"]) / "scratch.txt").read_text() == "keep this\n"
    assert next(b for b in w.status()["aliases"] if b["id"] == before["id"]) == before


def test_r4_update_reports_the_pending_renewal_without_changing_the_binding(w):
    _, rv, before = pending_round(w)
    result = pin(w, rv, provider="fxb", model="fxb/m1")
    assert result["session_renewal"] == {"reason": "provider_changed", "at": "next_admission"}
    assert next(b for b in w.status()["aliases"] if b["id"] == before["id"]) == before


@pytest.mark.parametrize("pins", [None, {"provider": "fxr"}, {"model": "fxr/m1"},
                                 {"provider": "fxr", "model": "fxr/m1"}])
def test_r5_no_pin_or_an_equal_pin_keeps_resuming_the_alias(w, pins):
    top, rv, before = pending_round(w)
    if pins is not None:
        result = pin(w, rv, **pins)
        assert not result.get("session_renewal")
    finish(w, top)
    first, second = w.fxr.calls()
    assert second["resume"] == first["session"] == before["session_id"]
    assert second["cwd"] == first["cwd"]
    assert not w.get(rv)["runs"][-1].get("reason")


@pytest.mark.parametrize("pins", [None, {"provider": "fxb"}])
def test_r5_an_alias_bound_on_a_fallback_stays_on_that_binding(w, pins):
    top, rv, before = pending_round(w, initial="fxb")
    assert before["provider"] == "fxb"
    if pins:
        assert not pin(w, rv, **pins).get("session_renewal")
    finish(w, top)
    first, second = w.fxb.calls()
    assert second["resume"] == first["session"] == before["session_id"]
    assert w.fxr.calls() == []
