"""ST4-R1/R2: a non-writing roster entry still reports its published range."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m4_world import M4World, commit_entry  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402


@pytest.mark.parametrize("surface", ["collect_agent", "wait_for_agents"])
@pytest.mark.parametrize("committed", [False, True])
def test_st4_non_writing_agent_reports_the_recorded_range(tmp_path, monkeypatch,
                                                        surface, committed):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.agent("observer", "fx", writes=False, permission="readonly")
    try:
        world.start_scheduler(timeout=8)
        node = world.simple("observed", "observer", fx=(
            commit_entry("observed.txt", "one\n") if committed else {}))
        done = world.wait_state(node, "done", timeout=8)
        run_id = done["runs"][0]["run_id"]
        if surface == "collect_agent":
            result = call_tool(world, surface, run_id)
        else:
            reply = call_tool(world, surface, [run_id], timeout=0)
            result = next(entry for entry in reply["changed"]
                          if entry["agent_id"] == run_id)
        assert not result.get("error"), result
        assert result["commits"] == int(committed), result
        assert bool(result.get("no_commits")) == (not committed), result
        assert not result.get("commits_pending"), result
        assert ("observed.txt" in result["diff_stat"]) == committed, result
    finally:
        world.close()
