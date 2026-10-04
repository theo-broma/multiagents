"""Adversary round 3 (M1): the MCP server's gate and transport replies."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc, tool  # noqa: E402,F401
from multiagents import server  # noqa: E402


@pytest.mark.parametrize("as_run", [False, True])
def test_adv3_a_stopped_scheduler_is_unavailable_not_an_exception(live, monkeypatch, as_run):
    node = live.create()
    if as_run:
        monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run-1")
        monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", live.issue("run-1", node["id"], {"read"}))
    live.stop()
    assert tool(server.list_nodes).get("error") == "scheduler_unavailable"
    assert tool(server.get_node, node["id"]).get("error") == "scheduler_unavailable"


def test_adv3_with_the_gate_on_a_broken_yaml_never_falls_back_to_a_runner_launch(nc):
    server.runner()                           # an established runner, gate on
    nc.p.write_raw("project.yaml", "scheduler: [enabled: true\n")
    result = tool(server.start_agent, "worker", "a task that must not launch")
    assert result.get("error") in {"scheduler_unavailable", "not_implemented"}, result
