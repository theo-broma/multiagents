"""Round-three guards for configuration drift and retained server gates."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from nc_harness import code, live, nc, tool  # noqa: E402,F401
from multiagents import scheduler, server  # noqa: E402
from multiagents.scheduler.store import Store  # noqa: E402


def remove_agent(live):
    gone = live.create(agent="other")
    live.p.agents.pop("other")
    live.p.write()
    return gone


def test_nc_r6_new_known_assignments_remain_valid_after_agent_removal(live):
    gone = remove_agent(live)
    assert live.create(task="new known assignment")["agent"] == "worker"
    assert live.get(gone["id"])["agent"] == "other"


@pytest.mark.parametrize("edit_removed", [False, True])
def test_nc_r6_retained_assignments_remain_editable_after_agent_removal(live, edit_removed):
    keep = live.create()
    gone = remove_agent(live)
    target = gone if edit_removed else keep
    assert live.update(target["id"], task="edited after drift")["task"] == "edited after drift"


@pytest.mark.parametrize("agent", ["other", "never-configured"])
def test_nc_r6_new_unknown_assignments_are_still_refused_after_agent_removal(live, agent):
    remove_agent(live)
    before, transitions = live.snapshot(), live.transitions()
    reply = live.create_raw({"kind": "simple", "agent": agent, "task": "invalid new assignment"})
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before
    assert live.transitions() == transitions


def test_nc_r6_config_drift_does_not_disable_whole_plan_cycle_checks(live):
    gone = remove_agent(live)
    dependent = live.create(depends_on=[{"node": gone["id"]}])
    before = live.snapshot()
    reply = live.update_raw(gone["id"], depends_on=[{"node": dependent["id"]}])
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before


@pytest.mark.parametrize("pinned", [False, True])
def test_nc_r6_r50_retained_alias_assignments_can_be_cancelled_after_agent_removal(live, pinned):
    gone = live.create(agent="other", pins={"provider": "acme2"} if pinned else {})
    # Template instances are host-owned; M4 will create this metadata.
    store = Store(live.root)
    with store.transaction() as db:
        node = store.nodes(db)[gone["id"]]
        node.update(template={"instance": "instance-1"}, session="review")
        store.save_node(db, node)
    live.p.agents.pop("other")
    live.p.write()
    assert live.cancel_raw(gone["id"])["ok"] is True
    assert live.get(gone["id"])["state"] == "cancelled"


def test_nc_r1_r76_node_tools_use_the_retained_gate_after_bad_yaml(nc):
    server.runner()
    nc.p.write_raw("project.yaml", "scheduler: [enabled: true\n")
    assert tool(server.list_nodes) == {"error": "scheduler_unavailable"}


def test_nc_r1_r76_node_tools_without_retained_config_report_unavailable_on_bad_yaml(nc):
    nc.p.write_raw("project.yaml", "scheduler: [enabled: true\n")
    assert tool(server.list_nodes) == {"error": "scheduler_unavailable"}


def test_nc_r76_a_retained_gate_still_requires_a_run_token(nc, monkeypatch):
    server.runner()
    nc.p.write_raw("project.yaml", "scheduler: [enabled: true\n")
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run")
    monkeypatch.delenv("MULTIAGENTS_RPC_TOKEN", raising=False)

    def no_root(*args):
        raise AssertionError("a run server must never read the root capability")

    monkeypatch.setattr(scheduler, "root_capability", no_root)
    assert tool(server.start_agent, "worker", "delegated") == {"error": "unauthenticated"}
