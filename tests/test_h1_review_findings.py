"""HA-R2a regressions: unrecorded operands and worktree path races."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from multiagents import gitops
from multiagents.config import AgentSpec
from multiagents.executor.docker import DockerExecutor
from test_h1_host_authority import (  # noqa: F401  (pytest fixture import)
    IDENTITY, commit, exists, git, project, start,
)
from test_h1_unrecorded_nodes import unrecorded
import c3_harness as h3


def mismatch(runner, node_id: str, action: str, fields: list[str]) -> None:
    events = [json.loads(line) for line in runner.paths.events_file.read_text().splitlines()]
    matches = [event for event in events if event.get("kind") == "host_authority_mismatch"
               and event.get("node") == node_id and event.get("action") == action]
    assert len(matches) == 1, matches
    assert matches[0].get("fields") == fields, matches


def test_ha_r2a_unrecorded_main_is_not_pushed(project, tmp_path):
    remote = tmp_path.parent / "review-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    project.config.project.setdefault("git", {}).update(
        remote=str(remote), push_agent_branches=True)
    node = unrecorded(project, "ag-forged-main", branch="main",
                      worktree=project.paths.worktree("ag-forged-main"))

    pushed = project.push_branch(node.id)
    assert pushed.get("pushed") is False, pushed
    assert not exists(remote, "main"), pushed
    mismatch(project, node.id, "push_branch", ["branch"])


def test_ha_r2a_unrecorded_main_is_not_merged(project):
    target = asyncio.run(start(project))
    target_head = git(Path(target.worktree), "rev-parse", "HEAD")
    (project.paths.root / "main-only.txt").write_text("main only\n")
    git(project.paths.root, "add", "main-only.txt")
    git(project.paths.root, "commit", "-qm", "advance main")
    node = unrecorded(project, "ag-forged-main", branch="main",
                      worktree=project.paths.worktree("ag-forged-main"))
    before = git(project.paths.root, "rev-parse", "main")

    merged = project.merge_agent(node.id, into=target.worktree)

    assert merged.get("result") != "merged", merged
    assert git(project.paths.root, "rev-parse", "main") == before
    assert git(Path(target.worktree), "rev-parse", "HEAD") == target_head
    assert not (Path(target.worktree) / "main-only.txt").exists()
    mismatch(project, node.id, "merge_agent", ["branch"])


def test_ha_r2a_unrecorded_node_cannot_name_recorded_branch(project):
    owner = asyncio.run(start(project))
    commit(owner, "owner-only.txt")
    node = unrecorded(project, "ag-forged-owner", branch=owner.branch,
                      worktree=project.paths.worktree("ag-forged-owner"))
    before = git(project.paths.root, "rev-parse", "main")

    result = project.merge_agent(node.id)
    assert result.get("result") != "merged", result
    assert git(project.paths.root, "rev-parse", "main") == before
    assert exists(project.paths.root, owner.branch)
    assert Path(owner.worktree).is_dir()
    mismatch(project, node.id, "merge_agent", ["branch"])


def test_ha_r2a_steer_rechecks_swapped_parent_before_move_aside(project, tmp_path,
                                                                   monkeypatch):
    node_id = "ag-steer-swap"
    branch = "agents/worker/steer-swap"
    git(project.paths.root, "branch", branch, "main")
    parent = project.paths.worktrees / "nested-steer"
    parent.mkdir()
    checkout = parent / node_id
    checkout.mkdir()
    node = unrecorded(project, node_id, branch=branch, worktree=checkout,
                      status="running", session_id="session-for-test")
    outside_parent = tmp_path / "outside-steer"
    outside = outside_parent / node_id
    outside.mkdir(parents=True)
    marker = outside / "keep.txt"
    marker.write_text("untouched\n")
    original = project.authority.safe_nested_path
    swapped = False

    def swap_after_check(path):
        nonlocal swapped
        allowed = original(path)
        if swapped or not allowed:
            return allowed
        # The original empty checkout stays inside the domain, at a different name.
        if checkout.exists():
            checkout.rename(project.paths.worktrees / "saved-steer-checkout")
            parent.rmdir()
        parent.symlink_to(outside_parent, target_is_directory=True)
        swapped = True
        return allowed

    monkeypatch.setattr(project.authority, "safe_nested_path", swap_after_check)
    asyncio.run(project.steer(node.id, "continue"))

    assert swapped, "the valid nested path was not checked"
    assert outside.is_dir()
    assert marker.read_text() == "untouched\n"
    assert not (outside_parent / f"{node_id}.1").exists()


def test_ha_r2a_refresh_rechecks_swapped_parent_before_reset(project, tmp_path,
                                                                monkeypatch):
    project.config.agents["worker"] = AgentSpec("worker", "fake", "m", conversational=True)
    node_id = "ag-refresh-swap"
    branch = "agents/worker/refresh-swap"
    parent = project.paths.worktrees / "nested-refresh"
    parent.mkdir()
    checkout = parent / node_id
    gitops.create_worktree(project.paths.root, checkout, branch, unique=False)
    node = unrecorded(project, node_id, branch=branch, worktree=checkout,
                      status="idle", session_id="session-for-test")
    project.tree.update(node.id, conversation=True)
    before = git(checkout, "rev-parse", "HEAD")
    (project.paths.root / "base-advance.txt").write_text("new base\n")
    git(project.paths.root, "add", "base-advance.txt")
    git(project.paths.root, "commit", "-qm", "advance base")
    outside_parent = tmp_path / "outside-refresh"
    outside_parent.mkdir()
    outside = outside_parent / node_id
    original = project.authority.safe_nested_path
    swapped = False

    def swap_after_check(path):
        nonlocal swapped
        allowed = original(path)
        if swapped or not allowed:
            return allowed
        git(project.paths.root, "worktree", "move", str(checkout), str(outside))
        parent.rmdir()
        parent.symlink_to(outside_parent, target_is_directory=True)
        swapped = True
        return allowed

    monkeypatch.setattr(project.authority, "safe_nested_path", swap_after_check)
    asyncio.run(project.consult("worker", "next turn"))

    assert swapped, "the valid nested path was not checked"
    assert outside.is_dir()
    assert git(outside, "rev-parse", "HEAD") == before
    assert not (outside / "base-advance.txt").exists()


def test_ha_r2a_symlinked_state_root_allows_recorded_merge(tmp_path, monkeypatch):
    physical = tmp_path.parent / "physical-state"
    physical.mkdir()
    link = tmp_path.parent / "linked-state"
    link.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(link))
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    provider = h3.fake_cli(tmp_path.parent, events=[
        {"type": "result", "subtype": "success", "result": "done"}])
    runner = h3.make_runner(tmp_path, monkeypatch,
        agents={"worker": AgentSpec("worker", "fake", "m")},
        providers={"fake": provider})
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / ".gitignore").write_text(".multiagents/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore project state")
    node = asyncio.run(start(runner))
    commit(node, "symlinked-root.txt")
    checkout = Path(node.worktree)

    result = runner.merge_agent(node.id)

    assert result.get("result") == "merged", result
    assert (tmp_path / "symlinked-root.txt").read_text() == "symlinked-root.txt"
    assert not checkout.exists()
    assert not exists(tmp_path, node.branch)


def test_ha_r2a_drop_if_empty_remains_branchless_when_steered(project):
    project.config.agents["worker"] = AgentSpec("worker", "fake", "m", writes=False)
    node = asyncio.run(start(project))
    original_branch = f"agents/worker/{node.id.removeprefix('ag-')}"
    project.tree.update(node.id, session_id="session-for-test")
    cleared = project.tree.get(node.id)
    assert cleared.branch == "" and cleared.worktree == "", cleared
    assert not exists(project.paths.root, original_branch)

    result = asyncio.run(project.steer(node.id, "continue"))
    resumed = project.tree.get(node.id)

    assert result.get("steered") is True, result
    assert resumed.worktree and Path(resumed.worktree).is_dir()
    assert resumed.branch == "", resumed
    assert not exists(project.paths.root, original_branch)
