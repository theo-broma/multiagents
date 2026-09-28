"""Host authority contract, HA-R1 through HA-R7.

The fake provider is only the agent process. Runner, Tree, git, the local
executor, and the Docker mount calculation are the public surfaces exercised.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

from multiagents import cli, gitops
from multiagents.config import AgentSpec
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import state_root
from multiagents.runner import Runner, reap_pending_branches
from multiagents.tree import Node


IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True,
                            capture_output=True, env={**os.environ, **IDENTITY}, check=True)
    return result.stdout.strip()


def exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
                             f"refs/heads/{branch}"], capture_output=True)
    return result.returncode == 0


def mismatch(r: Runner, node_id: str, action: str, *fields: str) -> None:
    rows = []
    for line in r.paths.events_file.read_text().splitlines():
        row = json.loads(line)
        if row.get("kind") == "host_authority_mismatch" and row.get("node") == node_id:
            rows.append(row)
    assert len(rows) == 1, rows
    assert isinstance(rows[0].get("action"), str) and rows[0]["action"], (action, rows)
    assert isinstance(rows[0].get("fields"), list), rows
    assert set(rows[0]["fields"]) == set(fields), rows


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    provider = h3.fake_cli(tmp_path.parent, events=[{"type": "result",
        "subtype": "success", "result": "done"}], delay=0.35)
    runner = h3.make_runner(tmp_path, monkeypatch,
        agents={"worker": AgentSpec("worker", "fake", "m")},
        providers={"fake": provider})
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / ".gitignore").write_text(".multiagents/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore project state")
    return runner


async def start(r: Runner, *, wait: bool = True) -> Node:
    result = await r.start("worker", "work")
    assert result.get("agent_id"), result
    node = r.tree.get(result["agent_id"])
    if wait:
        await asyncio.wait_for(r.runs[node.id].done.wait(), timeout=20)
        node = r.tree.get(node.id)
    return node


def commit(node: Node, filename: str) -> None:
    path = Path(node.worktree)
    (path / filename).write_text(filename)
    git(path, "add", filename)
    git(path, "commit", "-qm", filename)


def forge(r: Runner, node: Node, **fields) -> None:
    r.tree.update(node.id, **fields)


def nested(r: Runner, parent: Node, node_id: str = "ag-nest01") -> Node:
    branch = f"agents/worker/{node_id.removeprefix('ag-')}"
    path = r.paths.worktree(node_id)
    gitops.create_worktree(r.paths.root, path, branch, unique=False)
    node = Node(id=node_id, agent="worker", provider="fake", model="m",
                parent=parent.id, depth=2, status="done", task="nested",
                branch=branch, worktree=str(path))
    r.tree.add(node)
    commit(node, f"{node_id}.txt")
    return node


def new_runner(r: Runner) -> Runner:
    return Runner(r.paths, r.config)


def test_ha_r1_record_survives_restart_and_tree_rewrite(project):
    node = asyncio.run(start(project))
    original_branch, original_worktree = node.branch, node.worktree
    commit(node, "original.txt")
    decoy = "agents/worker/decoy"
    git(project.paths.root, "branch", decoy, "main")
    forge(project, node, branch=decoy, worktree=str(project.paths.root), parent="ag-forged")
    restarted = new_runner(project)
    result = restarted.merge_agent(node.id)
    assert result.get("result") != "empty", result
    assert exists(project.paths.root, decoy)
    if result.get("result") == "merged":
        assert (project.paths.root / "original.txt").exists()
        assert not Path(original_worktree).exists()
    else:
        assert exists(project.paths.root, original_branch)
    mismatch(restarted, node.id, "merge_agent", "branch", "worktree", "parent")


def test_ha_r1_host_record_is_outside_all_docker_mounts(project):
    def host_files():
        return {p: p.read_bytes() for p in state_root().rglob("*") if p.is_file()
                and not any(part in {"worktrees", "homes"} for part in p.parts)}
    before = host_files()
    asyncio.run(start(project))
    changed = [p for p, content in host_files().items() if before.get(p) != content]
    assert changed, "spawn must update durable host state outside worktrees and homes"
    executor = DockerExecutor({"image": "unused", "mount_cli_from_host": False},
                              project.paths, {}, state_root())
    mounted = [path.resolve() for path, _ in executor.mounts()]
    assert all(not any(p.resolve() == mount or mount in p.resolve().parents
                       for mount in mounted) for p in changed), (changed, mounted)


def test_ha_r2_discard_uses_recorded_worktree_and_branch(project):
    live = asyncio.run(start(project))
    other = asyncio.run(start(project))
    forge(project, live, branch=other.branch, worktree=other.worktree)
    result = project.discard_agent(live.id, force=True)
    assert "discarded" in result, result
    assert exists(project.paths.root, other.branch)
    assert Path(other.worktree).is_dir()
    mismatch(project, live.id, "discard_agent", "branch", "worktree")


def test_ha_r2_push_uses_recorded_ref(project, tmp_path):
    node = asyncio.run(start(project))
    commit(node, "original.txt")
    decoy = "agents/worker/decoy"
    git(project.paths.root, "branch", decoy, "main")
    remote = tmp_path.parent / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    project.config.project.setdefault("git", {}).update(
        remote=str(remote), push_agent_branches=True)
    forge(project, node, branch=decoy)
    result = project.push_branch(node.id)
    pushed_decoy = subprocess.run(["git", "-C", str(remote), "show-ref", "--verify",
        "--quiet", f"refs/heads/{decoy}"], capture_output=True).returncode == 0
    assert not pushed_decoy, result
    if result.get("pushed"):
        assert git(remote, "rev-parse", f"refs/heads/{node.branch}") == git(
            project.paths.root, "rev-parse", f"refs/heads/{node.branch}")
    mismatch(project, node.id, "push_branch", "branch")


def test_ha_r2_steer_does_not_move_another_agents_worktree(project):
    async def run():
        target = await start(project, wait=False)
        victim = await start(project, wait=False)
        project.tree.update(target.id, session_id="session-for-test")
        forge(project, target, worktree=victim.worktree, branch=target.branch)
        await project.steer(target.id, "continue")
        return target, victim
    target, victim = asyncio.run(run())
    assert Path(victim.worktree).is_dir()
    assert git(Path(victim.worktree), "symbolic-ref", "--short", "HEAD") == victim.branch
    mismatch(project, target.id, "steer", "worktree")


def test_ha_r2_conversation_refresh_does_not_move_another_branch(
        tmp_path, monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)
    provider = h3.fake_cli(tmp_path.parent, events=[
        {"type": "text", "text": "reply", "session": "s-refresh"},
        {"type": "result", "subtype": "success", "result": "reply"}])
    provider["stream"]["session_id_paths"] = ["session"]
    runner = h3.make_runner(tmp_path, monkeypatch,
        agents={"worker": AgentSpec("worker", "fake", "m", conversational=True)},
        providers={"fake": provider})
    git(tmp_path, "branch", "-M", "main")
    (tmp_path / ".gitignore").write_text(".multiagents/\n")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore state")
    first = asyncio.run(runner.consult("worker", "first"))
    conversation = runner.tree.get(first["agent_id"])
    victim = asyncio.run(start(runner))
    victim_head = git(Path(victim.worktree), "rev-parse", "HEAD")
    (tmp_path / "new-base.txt").write_text("base advances\n")
    git(tmp_path, "add", "new-base.txt")
    git(tmp_path, "commit", "-qm", "advance base")
    forge(runner, conversation, branch=victim.branch, worktree=victim.worktree)
    asyncio.run(runner.consult("worker", "second"))
    assert git(Path(victim.worktree), "rev-parse", "HEAD") == victim_head
    assert git(Path(victim.worktree), "symbolic-ref", "--short", "HEAD") == victim.branch
    mismatch(runner, conversation.id, "conversation_refresh", "branch", "worktree")


def test_ha_r2_resume_does_not_commit_another_agents_worktree(project, monkeypatch):
    victim = asyncio.run(start(project))
    attacker = asyncio.run(start(project))
    marker = Path(victim.worktree) / "private-uncommitted.txt"
    marker.write_text("keep uncommitted\n")
    before = git(Path(victim.worktree), "rev-parse", "HEAD")
    for name in ("output.ndjson", "exit_status"):
        (project.paths.run_dir(attacker.id) / name).unlink(missing_ok=True)
    forge(project, attacker, status="running", worktree=victim.worktree,
          branch=victim.branch, pid=999999999, pid_start="")
    monkeypatch.setattr(cli, "_executor_problems", lambda *args: [])
    result = cli.cmd_resume(argparse.Namespace(path=str(project.paths.root),
        no_launch=True, resume=True, wait=False, unattended=0, team="", supervise=True))
    assert result == 0
    assert git(Path(victim.worktree), "rev-parse", "HEAD") == before
    assert marker.is_file()
    mismatch(project, attacker.id, "resume", "branch", "worktree")


def test_ha_r2_clean_branches_protects_live_host_node(project):
    victim = asyncio.run(start(project))
    attacker = asyncio.run(start(project))
    forge(project, victim, status="running")
    forge(project, attacker, branch=victim.branch, worktree=victim.worktree)
    result = cli.cmd_clean(argparse.Namespace(path=str(project.paths.root),
        branches=True, homes=False, tree=False, force=True))
    assert result == 0
    assert exists(project.paths.root, victim.branch)
    assert Path(victim.worktree).is_dir()
    mismatch(project, attacker.id, "clean", "branch", "worktree")


def test_ha_r3_forged_parent_never_merges_into_root(project):
    async def run():
        node = await start(project, wait=False)
        commit(node, "attack.txt")
        forge(project, node, parent="ag-nonexistent")
        await asyncio.wait_for(project.runs[node.id].done.wait(), timeout=20)
        return node
    base = git(project.paths.root, "rev-parse", "HEAD")
    node = asyncio.run(run())
    assert git(project.paths.root, "rev-parse", "HEAD") == base
    assert not (project.paths.root / "attack.txt").exists()
    assert exists(project.paths.root, node.branch)
    assert project.tree.get(node.id).status == "done"
    mismatch(project, node.id, "auto_merge", "parent")


@pytest.mark.parametrize("target", ["host", "main", "feature"])
def test_ha_r4_forged_pending_delete_protects_outside_domain(project, target):
    protected = asyncio.run(start(project))
    forged = asyncio.run(start(project))
    branch = {"host": protected.branch, "main": "main", "feature": "feature/safe"}[target]
    if target == "feature":
        git(project.paths.root, "branch", branch, "main")
    if target == "host":
        # A forged terminal status on the victim must not release a branch
        # whose host completion never occurred.
        forge(project, protected, status="merged", worktree="")
        git(project.paths.root, "worktree", "remove", "--force", protected.worktree)
    forge(project, forged, status="discarded", branch=branch,
          branch_pending_delete=branch, worktree="")
    reap_pending_branches(project.paths.root, project.tree)
    new_runner(project)
    assert exists(project.paths.root, branch)


def test_ha_r4_nested_pending_delete_still_works(project):
    parent = asyncio.run(start(project))
    child = nested(project, parent)
    git(project.paths.root, "worktree", "remove", "--force", child.worktree)
    forge(project, child, status="merged", worktree="",
          branch_pending_delete=child.branch)
    new_runner(project)
    assert not exists(project.paths.root, child.branch)


def test_ha_r4_host_completion_deletes_its_exact_branch(project):
    node = asyncio.run(start(project))
    result = project.discard_agent(node.id, force=True)
    assert result.get("discarded") is True, result
    assert not exists(project.paths.root, node.branch)
    assert not Path(node.worktree).exists()
    assert new_runner(project).tree.get(node.id).status == "discarded"


@pytest.mark.parametrize("target", ["outside", "symlink", "other_host"])
def test_ha_r5_forged_worktree_cannot_remove_protected_path(project, target, tmp_path):
    victim = asyncio.run(start(project))
    attacker = asyncio.run(start(project))
    outside = tmp_path.parent / "outside" / "keep"
    outside.mkdir(parents=True, exist_ok=True)
    if target == "outside":
        path = outside
    elif target == "other_host":
        path = Path(victim.worktree)
    else:
        link = project.paths.worktrees / "escape"
        link.symlink_to(outside, target_is_directory=True)
        path = link
    forge(project, attacker, worktree=str(path))
    project.discard_agent(attacker.id, force=True)
    assert path.exists()
    assert outside.is_dir()
    assert Path(victim.worktree).is_dir()
    mismatch(project, attacker.id, "discard_agent", "worktree")


@pytest.mark.parametrize("branch_kind", ["main", "other_host", "feature"])
def test_ha_r6_forged_pending_child_is_never_merged(project, branch_kind):
    async def run():
        parent = await start(project, wait=False)
        other = await start(project, wait=False)
        branch = {"main": "main", "other_host": other.branch,
                  "feature": "feature/safe"}[branch_kind]
        if branch_kind == "feature":
            git(project.paths.root, "branch", branch, "main")
        child = Node(id="ag-forged", agent="worker", provider="fake", model="m",
                     parent=parent.id, depth=2, status="done", task="forged",
                     branch=branch)
        project.tree.add(child)
        await asyncio.wait_for(project.runs[parent.id].done.wait(), timeout=20)
        await asyncio.wait_for(project.runs[other.id].done.wait(), timeout=20)
        return parent, other, child
    parent, other, child = asyncio.run(run())
    assert exists(project.paths.root, child.branch)
    assert exists(project.paths.root, other.branch)
    assert project.tree.get(child.id).status == "done"
    mismatch(project, child.id, "merge_pending_children", "branch")


def test_ha_r6_genuine_deferred_nested_child_merges_into_recorded_parent(project):
    async def run():
        parent = await start(project, wait=False)
        child = nested(project, parent)
        await asyncio.wait_for(project.runs[parent.id].done.wait(), timeout=20)
        return parent, child
    parent, child = asyncio.run(run())
    assert (Path(parent.worktree) / f"{child.id}.txt").is_file()
    assert project.tree.get(child.id).status == "merged"


def test_ha_r7_existing_tree_node_is_seeded_and_protected(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    paths = h3.make_paths(tmp_path)
    h3.make_git_repo(tmp_path)
    git(tmp_path, "branch", "-M", "main")
    branch = "agents/worker/legacy"
    worktree = paths.worktree("ag-legacy")
    gitops.create_worktree(tmp_path, worktree, branch, unique=False)
    from multiagents.tree import Tree
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-legacy", agent="worker", provider="fake", model="m",
                  parent=None, depth=1, status="merged", branch=branch,
                  worktree=str(worktree)))
    tree.update("ag-legacy", branch_pending_delete=branch)
    git(tmp_path, "worktree", "remove", "--force", str(worktree))
    config = h3.make_config()
    Runner(paths, config)
    Runner(paths, config)
    assert exists(tmp_path, branch)
    assert tree.read()["nodes"]["ag-legacy"]["branch_pending_delete"] == branch


def test_ha_r7_seeded_outside_worktree_is_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    paths = h3.make_paths(tmp_path)
    h3.make_git_repo(tmp_path)
    git(tmp_path, "branch", "-M", "main")
    outside = tmp_path.parent / "legacy-outside"
    branch = "agents/worker/legacy-outside"
    gitops.create_worktree(tmp_path, outside, branch, unique=False)
    from multiagents.tree import Tree
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-legacy", agent="worker", provider="fake", model="m",
                  parent=None, depth=1, status="done", branch=branch,
                  worktree=str(outside)))
    runner = Runner(paths, h3.make_config())
    runner.discard_agent("ag-legacy", force=True)
    assert outside.is_dir()
    assert exists(tmp_path, branch)
    mismatch(runner, "ag-legacy", "discard_agent", "worktree")


def test_ha_r7_interrupted_seed_blocks_reap_then_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    paths = h3.make_paths(tmp_path)
    h3.make_git_repo(tmp_path)
    git(tmp_path, "branch", "-M", "main")
    from multiagents.tree import Tree
    tree = Tree(paths.tree_file, paths.events_file)
    protected = "agents/worker/preupgrade"
    git(tmp_path, "branch", protected, "main")
    tree.add(Node(id="ag-legacy", agent="worker", provider="fake", model="m",
                  parent=None, depth=1, status="merged", branch=protected))
    tree.update("ag-legacy", branch_pending_delete=protected)
    actual_read = Tree.read
    attempted = False

    def fault_once(self):
        nonlocal attempted
        if self.path == paths.tree_file and not attempted:
            attempted = True
            raise OSError("injected seed read interruption")
        return actual_read(self)

    with monkeypatch.context() as fault:
        fault.setattr(Tree, "read", fault_once)
        try:
            Runner(paths, h3.make_config())
        except OSError:
            pass
    assert attempted, "the first host start must try to seed from the tree"
    assert exists(tmp_path, protected), "no deletion before a complete seed"

    Runner(paths, h3.make_config())
    assert exists(tmp_path, protected), "a retried seed must protect the pre-upgrade branch"
    assert tree.read()["nodes"]["ag-legacy"]["branch_pending_delete"] == protected

    later = "agents/worker/after-seed"
    git(tmp_path, "branch", later, "main")
    tree.add(Node(id="ag-later", agent="worker", provider="fake", model="m",
                  parent=None, depth=1, status="merged", branch=later))
    tree.update("ag-later", branch_pending_delete=later)
    Runner(paths, h3.make_config())
    assert not exists(tmp_path, later), "reaping resumes once seeding completes"


def test_ha_r7_seeding_does_not_repeat_for_later_tree_nodes(project):
    first = new_runner(project)
    parent = asyncio.run(start(first))
    later = nested(first, parent, "ag-later1")
    git(first.paths.root, "worktree", "remove", "--force", later.worktree)
    forge(first, later, status="merged", worktree="",
          branch_pending_delete=later.branch)
    new_runner(first)
    assert not exists(first.paths.root, later.branch)
