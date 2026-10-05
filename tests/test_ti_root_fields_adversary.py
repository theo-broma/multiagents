"""Edges of TI-R2/R3 the contract suite does not walk (an addition, not a change):
a template that declares its own root fields, a root field a caller omits,
a dependency the instance cannot take, an unprivileged caller, a replayed
request id, and a window whose zone was never loaded.

`tests/test_ti_template_instantiation.py` is the contract; this file only asks
whether the root fields behave off the paths it takes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.m4_world import M4World, err_code, unwrap  # noqa: E402

from multiagents import server  # noqa: E402

URGENT_TPL = """\
template: urgent-root
version: 1
params:
  first: {type: text, default: "one"}
root:
  key: top
  kind: sequence
  urgent: true
  depends_on: []
  children:
    - {key: one, kind: simple, agent: worker, task: {param: first}}
    - {key: two, kind: simple, agent: worker, task: "two"}
"""

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


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.start_scheduler()
    world.register_ok(TPL)
    world.register_ok(URGENT_TPL)
    yield world
    world.close()


def params(tag: str) -> dict:
    return {"first": task(f"{tag}1", "first"), "second": task(f"{tag}2", "second")}


def test_a_template_root_that_declares_urgent(w):
    server._reset()
    reply = server.instantiate_template(name="urgent-root", params={}, plan_revision=w.plan_revision())
    top = unwrap(reply)
    assert w.get(top["id"])["urgent"] is True, "the template's own root field must survive"


def test_explicit_false_overrides_it(w):
    server._reset()
    reply = server.instantiate_template(name="urgent-root", params={},
                                        plan_revision=w.plan_revision(), urgent=False)
    top = unwrap(reply)
    assert w.get(top["id"])["urgent"] is False


def test_depends_on_require_finished_and_inputs_generation(w):
    src = w.simple("SRC")
    w.wait_state(src, "done", timeout=8)
    server._reset()
    reply = server.instantiate_template(
        name="seq2", params=params("Q"), plan_revision=w.plan_revision(),
        depends_on=[{"node": src, "require": "finished"}],
        inputs=[{"node": src, "generation": 1}])
    top = unwrap(reply)
    node = w.get(top["id"])
    assert node["depends_on"] == [{"node": src, "require": "finished"}]
    assert node["inputs"] == [{"node": src, "generation": 1}]
    w.wait_spawn("Q1", timeout=8)


def test_a_dependency_on_the_instances_own_child_is_a_cycle(w):
    holder = w.simple("H", fx={"hang": True})
    w.wait_running(holder, timeout=8)
    server._reset()
    reply = server.instantiate_template(name="seq2", params=params("C"),
                                         plan_revision=w.plan_revision(),
                                         depends_on=[{"node": holder}])
    top = unwrap(reply)
    for _ in range(4):
        server._reset()
        cyclic = server.update_node(top["id"], w.get(top["id"])["revision"],
                                    depends_on=[{"node": top["children"][0]}])
        if err_code({"ok": not cyclic.get("error"), **cyclic}) != "conflict":
            break
    assert err_code({"ok": not cyclic.get("error"), **cyclic}) == "invalid", cyclic


def test_a_retry_with_the_same_request_id_and_other_arguments(w):
    a = w.rpc("instantiate_template", {"name": "seq2", "params": params("R1"), "locks": ["L"]},
              request_id="ti-1")
    b = w.rpc("instantiate_template", {"name": "seq2", "params": params("R1"), "urgent": True},
              request_id="ti-1")
    assert a.get("ok") and err_code(b) == "request_id_reused", (a, b)


def test_a_subagent_may_not_name_a_node_outside_its_subtree(w):
    outside = w.simple("OUT")
    own = w.simple("OWN")
    from multiagents.scheduler import issue_run_capability
    token = issue_run_capability(w.root, "run-x", own, {"read", "delegate"})
    reply = w.rpc("instantiate_template",
                  {"name": "seq2", "params": params("S"), "depends_on": [{"node": outside}]},
                  token=token)
    assert err_code(reply) == "forbidden", reply


def test_a_subagent_with_scoped_dependencies_is_allowed(w):
    own = w.simple("OWN2")
    w.wait_running(own, timeout=8)
    from multiagents.scheduler import issue_run_capability
    token = issue_run_capability(w.root, "run-y", own, {"read", "delegate"})
    reply = w.rpc("instantiate_template",
                  {"name": "seq2", "params": params("T"), "parent": own,
                   "locks": ["L2"]}, token=token)
    assert reply.get("ok"), reply
    top = unwrap(reply["result"])
    assert w.get(top["id"], token)["locks"] == ["L2"]


def test_an_invalid_window_names_the_window(w):
    window = {"timezone": "Mars/Base", "days": ["mon"], "ranges": ["09:00-17:00"]}
    reply = w.rpc("instantiate_template", {"name": "seq2", "params": params("W"), "window": window})
    assert err_code(reply) == "invalid", reply
    assert reply["error"]["problems"][0].startswith("window"), reply