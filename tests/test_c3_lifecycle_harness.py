"""Proof that the C3 harness (tests/support/c3_harness.py) runs.

Not a characterization suite — one or two tests per entry point, enough to
show the seams actually reach real production code, including the full
runner spawn/consume/finalize pipeline through a real (fake-CLI) subprocess.
The next phase's characterizers own the full suite.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402


# ---------------------------------------------------------------------------
# Entry point 1 — Runner.start(), through a real subprocess, as root
# ---------------------------------------------------------------------------

def test_start_runs_a_real_subprocess_and_finalizes_the_node(tmp_path, monkeypatch):
    """The can_spawn wall, worked around: this passes from inside an agent.

    `start()` returns once the process is LAUNCHED, not once it finishes —
    `_consume`/`_finalize` run as background tasks on the same event loop, so
    a caller that wants the finished node has to keep that loop alive (as the
    real MCP server's long-lived loop does) rather than let a single
    `asyncio.run()` close underneath them. One coroutine, one `asyncio.run`,
    so the background tasks get to complete before the loop tears down.
    """
    provider = h.fake_cli(tmp_path, "p", events=[
        {"type": "result", "subtype": "success", "result": "done"},
    ], exit_code=0)
    spec = h.AgentSpec("worker", "p", "m")
    r = h.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                      providers={"p": provider})

    async def go():
        result = await r.start("worker", "go")
        run = r.runs[result["agent_id"]]
        await asyncio.wait_for(run.done.wait(), timeout=10)
        return result

    result = asyncio.run(go())

    assert result.get("agent_id"), result
    node = r.tree.get(result["agent_id"])
    assert node is not None
    assert node.status == "done", node.status


def test_can_spawn_gate_refuses_a_subagent_without_permission(tmp_path, monkeypatch):
    """The gate itself, exercised deliberately via `as_subagent` rather than
    worked around — proves the harness can test the permission check, not
    just bypass it."""
    provider = h.fake_cli(tmp_path, "p", events=[])
    spec = h.AgentSpec("worker", "p", "m", can_spawn=False)
    r = h.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                      providers={"p": provider})
    h.as_subagent(monkeypatch, agent_id="ag-caller", can_spawn=False)

    import pytest
    with pytest.raises(PermissionError, match="can_spawn is false"):
        asyncio.run(r.start("worker", "go"))


# ---------------------------------------------------------------------------
# Entry point 2 — Tree, direct state mutation
# ---------------------------------------------------------------------------

def test_tree_add_and_set_status_round_trip(tmp_path):
    tree = h.make_tree(tmp_path)
    node = h.Node(id="ag-1", agent="worker", provider="p", model="m",
                  parent=None, depth=1)
    tree.add(node)
    tree.set_status("ag-1", "running")
    assert tree.get("ag-1").status == "running"
    assert [n.id for n in tree.active()] == ["ag-1"]


# ---------------------------------------------------------------------------
# Entry point 3 — gitops.merge, against throwaway repositories
# ---------------------------------------------------------------------------

def test_merge_lands_a_clean_branch(tmp_path):
    repo, _worktree, branch = h.make_repo_pair(tmp_path)
    status, _detail = h.gitops.merge(repo, branch, "merge worker's branch")
    assert status == "merged"
    assert (repo / "agent-work.txt").exists()


def test_merge_reports_conflict_without_losing_the_branch(tmp_path):
    repo, second_branch = h.make_conflicting_branches(tmp_path)
    status, detail = h.gitops.merge(repo, second_branch, "merge branch two")
    assert status == "conflict"
    assert detail
    # The branch survives a conflict — merge() aborts, it does not delete.
    assert h.gitops.branch_exists(repo, second_branch)


# ---------------------------------------------------------------------------
# Entry point 4 — watchdog.verdict (pure) and .sample (real transcript file)
# ---------------------------------------------------------------------------

def test_verdict_is_pure_and_needs_no_clock():
    state, detail = h.watchdog.verdict(
        running=True, quiet_for=30.0, quota_known=True, quota_left=0.5,
        active_agents=1)
    assert state == "working"
    assert "30s ago" in detail


def test_sample_reads_quiet_for_off_a_backdated_transcript_file(tmp_path):
    import os
    provider, cwd = h.make_transcript(tmp_path, lines=[{"type": "assistant"}],
                                      quiet_for=400.0)
    paths = h.make_paths(cwd)
    # This test's own pid is a real, currently-alive process — no fake needed.
    record = h.watchdog.sample(paths, config=None, provider=provider,
                               role="orchestrator", pid=os.getpid())
    assert record["transcript"]["quiet_for"] >= 399.0
    assert record["verdict"] == "idle"
