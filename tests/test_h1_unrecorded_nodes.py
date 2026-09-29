"""Host operations on nodes added to tree.json after the one-time H1 seed."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from multiagents import cli, gitops
from multiagents.tree import Node

from test_h1_host_authority import git, project, start  # noqa: F401


def unrecorded(runner, node_id: str, *, branch: str, worktree: Path,
               status: str = "done", session_id: str = "",
               parent: str | None = None) -> Node:
    """Insert only container-visible tree state after the host seed has run."""
    node = Node(id=node_id, agent="worker", provider="fake", model="m",
                parent=parent, depth=2 if parent else 1, status=status, task="forged",
                branch=branch, worktree=str(worktree), session_id=session_id)
    runner.tree.add(node)
    return node


def mismatch(runner, node_id: str, action: str) -> None:
    events = [json.loads(line) for line in runner.paths.events_file.read_text().splitlines()]
    matches = [event for event in events
               if event.get("kind") == "host_authority_mismatch"
               and event.get("node") == node_id]
    assert len(matches) == 1, matches
    assert matches[0].get("action") == action, matches
    assert "worktree" in matches[0].get("fields", []), matches


def test_ha_r2_resume_unrecorded_node_cannot_commit_recorded_worktree(
        project, monkeypatch):
    victim = asyncio.run(start(project))
    checkout = Path(victim.worktree)
    marker = checkout / "private-uncommitted.txt"
    marker.write_text("must remain uncommitted\n")
    before = git(checkout, "rev-parse", "HEAD")

    # The forged node appears only after Runner's one-time migration seed.
    git(project.paths.root, "branch", "agents/worker/later-resume", "main")
    intruder = unrecorded(project, "ag-later-resume", branch="agents/worker/later-resume",
                          worktree=checkout, status="running")
    project.tree.update(intruder.id, pid=999999999, pid_start="")
    monkeypatch.setattr(cli, "_executor_problems", lambda *args: [])

    cli.cmd_resume(argparse.Namespace(path=str(project.paths.root), no_launch=True,
                                      resume=True, wait=False, unattended=0,
                                      team="", supervise=True))

    assert marker.read_text() == "must remain uncommitted\n"
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "status", "--porcelain", "--", marker.name) == "?? private-uncommitted.txt"
    mismatch(project, intruder.id, "resume")


def test_ha_r2_steer_unrecorded_node_cannot_move_recorded_worktree(project):
    victim = asyncio.run(start(project))
    checkout = Path(victim.worktree)
    before = git(checkout, "rev-parse", "HEAD")
    marker = checkout / "private-uncommitted.txt"
    marker.write_text("keep this checkout intact\n")
    branch = "agents/worker/later-steer"
    git(project.paths.root, "branch", branch, "main")
    intruder = unrecorded(project, "ag-later-steer", branch=branch,
                          worktree=checkout, status="running",
                          session_id="session-for-test")

    asyncio.run(project.steer(intruder.id, "continue"))

    assert checkout.is_dir()
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "symbolic-ref", "--short", "HEAD") == victim.branch
    assert marker.read_text() == "keep this checkout intact\n"
    assert git(checkout, "status", "--porcelain", "--", marker.name) == "?? private-uncommitted.txt"
    mismatch(project, intruder.id, "steer")


def test_ha_r5_nested_worktree_symlink_swap_cannot_remove_outside_directory(
        project, tmp_path):
    parent = asyncio.run(start(project))
    node_id = "ag-later-swap"
    branch = "agents/worker/later-swap"
    checkout = project.paths.worktree(node_id)
    gitops.create_worktree(project.paths.root, checkout, branch, unique=False)
    intruder = unrecorded(project, node_id, branch=branch, worktree=checkout,
                          parent=parent.id)

    # It was a valid nested path when recorded in the tree. Replace that path
    # before the host performs cleanup; containment must hold at removal time.
    assert checkout.resolve().is_relative_to(project.paths.worktrees.resolve())
    git(project.paths.root, "worktree", "remove", "--force", str(checkout))
    outside = tmp_path / "outside-swap-target"
    outside.mkdir()
    marker = outside / "must-survive.txt"
    marker.write_text("outside the container domain\n")
    checkout.symlink_to(outside, target_is_directory=True)

    project.discard_agent(intruder.id, force=True)

    assert marker.read_text() == "outside the container domain\n"
    assert outside.is_dir()
    assert checkout.is_symlink()
    mismatch(project, intruder.id, "discard_agent")
