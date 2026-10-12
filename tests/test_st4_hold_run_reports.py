"""ST4-R1/R2: hold delivery carries the same run-local report as collect."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m4_world import M4World, commit_entry  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402
from test_tb_a_wait_unlaunched import InFlight  # noqa: E402


def test_st4_hold_delivery_reports_the_runs_commit_range(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.agent("coder", "fx", writes=True)
    try:
        world.start_scheduler(timeout=8)
        node = world.simple("question", "coder", fx={
            **commit_entry("question.txt", "one\n"),
            "text": "NEED_INFO(question): clarify the next step"})
        held = world.wait_held(node, "needs_info", timeout=8)
        run_id = held["runs"][-1]["run_id"]
        collected = call_tool(world, "collect_agent", run_id)
        reply = call_tool(world, "wait_for_agents", [run_id], timeout=0)
        fields = {"commits", "diff_stat", "no_commits", "no_commits_note", "commits_pending"}
        expected = {k: v for k, v in collected.items() if k in fields}
        assert expected, collected
        for surface in ("changed", "holds"):
            entry = next(e for e in reply[surface] if e.get("agent_id") == run_id)
            assert {k: v for k, v in entry.items() if k in fields} == expected, reply
    finally:
        world.close()


def test_st4_wait_reports_a_node_run_that_launches_during_the_wait(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.agent("coder", "fx", writes=True)
    try:
        world.start_scheduler(timeout=8)
        blocker = world.simple("blocker", "coder", fx={
            "text": "NEED_INFO(question): clarify the next step"})
        node = world.simple("committing", "coder",
                            depends_on=[{"node": blocker, "require": "success"}],
                            fx=commit_entry("later.txt", "one\n", gate="finish"))
        world.wait_held(blocker, "needs_info", timeout=8)
        call_tool(world, "wait_for_agents", timeout=0)
        waiting = InFlight(world, timeout=8)
        waiting.settle(world)
        assert not waiting.returned(0)
        assert world.root_op("close_node", blocker, outcome="approved").get("ok")
        running = world.wait_running(node, timeout=8)
        run_id = running["runs"][-1]["run_id"]
        world.gate("finish")
        reply = waiting.result()
        entry = next(e for e in reply["changed"] if e.get("agent_id") == run_id)
        assert entry.get("commits") == 1 or entry.get("commits_pending") is True, reply
        assert not entry.get("no_commits"), reply
        if "commits" in entry:
            assert "later.txt" in entry["diff_stat"], reply
    finally:
        world.close()
