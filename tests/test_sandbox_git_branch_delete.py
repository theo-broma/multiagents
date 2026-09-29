"""SG-R2, "Branch deletion, after tester ag-438693".

Contract: `context/specs/sandbox-git.md`, the last bullet of "Decisions,
2026-09-27 (orchestrator, after the live smoke test)". With `.git` read-only
in the container, git cannot take `.git/packed-refs.lock`, which it takes for
EVERY ref deletion, loose or packed. So:

- **Inside the container**, a branch deletion that fails for that reason is
  not an error. `merge_agent` / `discard_agent` still succeed, the branch is
  recorded on the node as `branch_pending_delete`, and an event is emitted.
- **The host** deletes every `agents/*` branch recorded as
  `branch_pending_delete` whose node is merged or discarded, on its own
  `merge_agent`, `discard_agent`, reconcile and runner start. Never a live
  node's branch, never a branch outside `refs/heads/agents/`, never a branch
  that does not belong to a node in its tree with a terminal status.

How the container is simulated: `chmod a-w` on the project's `.git`, which is
exactly the condition the contract names. Its subdirectories keep their own
permissions, as the writable directory mounts do in the real container
(`objects`, `refs`, `logs`, `worktrees`), so a merge into a parent's worktree
and `git worktree remove` still work, and only `packed-refs.lock` fails.
Checked by hand with git 2.x before writing this. The permission is restored in
the fixture's teardown, whatever the test did.

Choices the contract left open, stated so they can be corrected:

- `branch_pending_delete` is read from the node as the tree persists it
  (`tree.json`), and its value is the branch name. The host-cleanup tests
  write it that way. (The contract says "the branch is recorded as
  `branch_pending_delete` on the node".)
- "The event" is not named. It is recognised as an event for that agent,
  other than `created` and `merge`, whose content names the branch.
- "Reconcile" is `multiagents run`'s reconciliation pass, `cmd_resume` with
  `--no-launch` (its docstring: "Reconcile state after a crash or restart").
- "Runner start" is constructing a `Runner` over the project.
- The consult worktree cleanup is not driven here: reaching it needs a full
  agent run through a fake CLI, and it shares the deletion step with
  `merge_agent` and `discard_agent`.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as c3  # noqa: E402
from multiagents import cli, gitops  # noqa: E402
from multiagents.config import load as load_config  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node, Tree  # noqa: E402

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env={**os.environ, **IDENTITY})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{branch}", check=False).returncode == 0


class Project:
    """A multiagents project with a real repository and a tree."""

    def __init__(self, tmp_path: Path, monkeypatch):
        for key, value in IDENTITY.items():
            monkeypatch.setenv(key, value)
        c3.as_root(monkeypatch)
        monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
        monkeypatch.setattr(cli, "_executor_problems", lambda *a: [])
        self.root = tmp_path / "proj"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "base.txt").write_text("base\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        cli.cmd_init(argparse.Namespace(path=str(self.root), force=False, nested=False))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "init")
        self.paths = cli._resolve(str(self.root))
        self.runner = self.new_runner()

    @property
    def tree(self) -> Tree:
        return Tree(self.paths.tree_file, self.paths.events_file)

    def new_runner(self) -> Runner:
        return Runner(self.paths, load_config(self.paths))

    def agent(self, node_id: str, *, status: str, branch: str = "",
              worktree: bool = True, commit: bool = True, parent: str | None = None,
              depth: int = 1, **extra) -> Node:
        """A node on its own branch, with a worktree and one commit on it."""
        branch = branch or f"agents/worker/{node_id[3:]}"
        path = self.paths.worktree(node_id)
        actual = gitops.create_worktree(self.root, path, branch, unique=False)
        assert actual == branch
        if commit:
            (path / f"{node_id}.txt").write_text(f"{node_id}\n")
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", f"{node_id} work")
        if not worktree:
            git(self.root, "worktree", "remove", "--force", str(path))
        node = Node(id=node_id, agent="worker", provider="p", model="m",
                    parent=parent, depth=depth, task=f"task of {node_id}",
                    status=status, branch=branch,
                    worktree=str(path) if worktree else "")
        self.tree.add(node)
        if extra:
            self.tree.update(node_id, **extra)
        return node

    def branch_only(self, branch: str) -> None:
        """A branch that exists in the repository but belongs to no node."""
        git(self.root, "branch", branch, "main")

    def raw(self, node_id: str) -> dict:
        return self.tree.read()["nodes"][node_id]

    def events(self) -> list[dict]:
        path = self.paths.events_file
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def reconcile(self) -> None:
        cli.cmd_resume(argparse.Namespace(
            path=str(self.root), no_launch=True, resume=True, wait=False,
            unattended=0, team="", supervise=True))


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
    return Project(tmp_path, monkeypatch)


@pytest.fixture
def readonly_git(project):
    """The container's `.git`: the directory itself not writable."""
    dot_git = project.root / ".git"
    mode = stat.S_IMODE(dot_git.stat().st_mode)
    dot_git.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        probe = dot_git / "probe.lock"
        try:
            probe.write_text("")
        except PermissionError:
            pass
        else:
            probe.unlink()
            pytest.skip("chmod a-w does not stop this user writing .git (root?)")
        # The premise, checked: git itself cannot delete a branch here.
        git(project.root, "branch", "agents/probe/0", "main")
        refused = git(project.root, "branch", "-D", "agents/probe/0", check=False)
        assert refused.returncode != 0 and "packed-refs.lock" in refused.stderr, refused
        yield dot_git
    finally:
        dot_git.chmod(mode)
        git(project.root, "branch", "-D", "agents/probe/0", check=False)


def pending_events(project: Project, node_id: str, branch: str) -> list[dict]:
    return [e for e in project.events()
            if e.get("agent") == node_id and e.get("kind") not in {"created", "merge"}
            and branch in json.dumps(e)]


# ==========================================================================
# 1. Inside the container: the deletion fails on packed-refs.lock
# ==========================================================================

@pytest.fixture
def nested(project):
    """A nested orchestrator's child, merged into the orchestrator's worktree."""
    parent = project.agent("ag-aaaa01", status="running", branch="agents/lead/aaaa01",
                           commit=False)
    child = project.agent("ag-bbbb02", status="done", parent=parent.id, depth=2)
    return parent, child


def test_sg_r2_merge_agent_succeeds_when_the_branch_cannot_be_deleted(
        project, nested, readonly_git):
    parent, child = nested
    result = project.runner.merge_agent(child.id, into=parent.worktree)

    assert result.get("result") == "merged", result
    assert "error" not in result, result
    assert project.raw(child.id)["status"] == "merged"
    # The merge landed in the parent's worktree.
    assert (Path(parent.worktree) / f"{child.id}.txt").is_file()
    # The worktree removal does not need packed-refs.lock, and still happens.
    assert not Path(child.worktree).exists()


def test_sg_r2_merge_agent_records_branch_pending_delete(project, nested, readonly_git):
    parent, child = nested
    project.runner.merge_agent(child.id, into=parent.worktree)

    assert branch_exists(project.root, child.branch), "premise: git could not delete it"
    assert project.raw(child.id).get("branch_pending_delete") == child.branch, \
        project.raw(child.id)


def test_sg_r2_merge_agent_emits_an_event_for_the_pending_delete(
        project, nested, readonly_git):
    parent, child = nested
    project.runner.merge_agent(child.id, into=parent.worktree)

    assert pending_events(project, child.id, child.branch), project.events()


def test_sg_r2_discard_agent_succeeds_when_the_branch_cannot_be_deleted(
        project, nested, readonly_git):
    _, child = nested
    result = project.runner.discard_agent(child.id, force=True)

    assert result.get("discarded") is True, result
    assert "error" not in result, result
    assert project.raw(child.id)["status"] == "discarded"
    assert not Path(child.worktree).exists()


def test_sg_r2_discard_agent_records_branch_pending_delete(project, nested, readonly_git):
    _, child = nested
    project.runner.discard_agent(child.id, force=True)

    assert branch_exists(project.root, child.branch), "premise: git could not delete it"
    assert project.raw(child.id).get("branch_pending_delete") == child.branch, \
        project.raw(child.id)


def test_sg_r2_discard_agent_emits_an_event_for_the_pending_delete(
        project, nested, readonly_git):
    _, child = nested
    project.runner.discard_agent(child.id, force=True)

    assert pending_events(project, child.id, child.branch), project.events()


@pytest.fixture
def commitless(project):
    return project.agent("ag-cccc03", status="done", commit=False)


def test_sg_r2_discard_of_a_commitless_branch_also_records_it(
        project, commitless, readonly_git):
    """No force needed and nothing unmerged: the deletion still fails the same way."""
    node = commitless
    result = project.runner.discard_agent(node.id)

    assert result.get("discarded") is True, result
    assert project.raw(node.id).get("branch_pending_delete") == node.branch


# ==========================================================================
# 2. On the host: pending deletions are carried out, and only the right ones
# ==========================================================================

def _trigger_merge(project: Project) -> None:
    node = project.agent("ag-7777a1", status="done")
    result = project.new_runner().merge_agent(node.id)
    assert result.get("result") == "merged", result


def _trigger_discard(project: Project) -> None:
    node = project.agent("ag-7777a2", status="done")
    result = project.new_runner().discard_agent(node.id, force=True)
    assert result.get("discarded") is True, result


def _trigger_reconcile(project: Project) -> None:
    project.reconcile()


def _trigger_runner_start(project: Project) -> None:
    project.new_runner()


TRIGGERS = {
    "merge_agent": _trigger_merge,
    "discard_agent": _trigger_discard,
    "reconcile": _trigger_reconcile,
    "runner_start": _trigger_runner_start,
}


@pytest.fixture
def left_behind(project):
    """The tree a container leaves: two pending deletions, each on a finished
    node, whose worktrees the container already removed."""
    merged = project.agent("ag-dddd04", status="merged", worktree=False)
    discarded = project.agent("ag-eeee05", status="discarded", worktree=False)
    for node in (merged, discarded):
        project.tree.update(node.id, branch_pending_delete=node.branch)
    return merged, discarded


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_sg_r2_host_deletes_pending_branches_of_finished_nodes(project, left_behind, trigger):
    TRIGGERS[trigger](project)

    for node in left_behind:
        assert not branch_exists(project.root, node.branch), (trigger, node.id)


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_sg_r2_host_never_deletes_a_live_nodes_branch(project, trigger):
    """The tree is container-writable: a pending mark on a live node is refused."""
    live = {status: project.agent(f"ag-{i}{i}{i}{i}f0", status=status,
                                  branch_pending_delete=f"agents/worker/{i}{i}{i}{i}f0")
            for i, status in enumerate(("running", "pending", "done", "stuck"), start=1)}
    TRIGGERS[trigger](project)

    for status, node in live.items():
        assert branch_exists(project.root, node.branch), (trigger, status)


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_sg_r2_host_never_deletes_outside_refs_heads_agents(project, trigger):
    """A finished node whose branch, and pending mark, is not an agents/ branch."""
    outside = {
        "feature/x": project.agent("ag-9999a1", status="merged", branch="feature/x",
                                   worktree=False, branch_pending_delete="feature/x"),
        "agentsX/y": project.agent("ag-9999a2", status="merged", branch="agentsX/y",
                                   worktree=False, branch_pending_delete="agentsX/y"),
    }
    main = project.agent("ag-9999a3", status="discarded", worktree=False)
    project.tree.update(main.id, branch_pending_delete="main")
    tags_like = project.agent("ag-9999a4", status="discarded", worktree=False)
    project.tree.update(tags_like.id,
                        branch_pending_delete="refs/heads/agents/../../heads/main")
    TRIGGERS[trigger](project)

    for branch in outside:
        assert branch_exists(project.root, branch), (trigger, branch)
    assert branch_exists(project.root, "main"), trigger


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_sg_r2_host_never_deletes_a_branch_no_tree_node_owns(project, trigger):
    """A finished node's pending mark naming someone else's branch: another
    tree's (no node here has it) or a live node's here."""
    project.branch_only("agents/stranger/abcdef")
    live = project.agent("ag-1212a1", status="running")
    liar = project.agent("ag-1212a2", status="merged", worktree=False)
    project.tree.update(liar.id, branch_pending_delete="agents/stranger/abcdef")
    liar2 = project.agent("ag-1212a3", status="discarded", worktree=False)
    project.tree.update(liar2.id, branch_pending_delete=live.branch)
    TRIGGERS[trigger](project)

    assert branch_exists(project.root, "agents/stranger/abcdef"), trigger
    assert branch_exists(project.root, live.branch), trigger


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_sg_r2_host_cleanup_of_an_already_gone_branch_is_harmless(project, trigger):
    """Deleted by hand, or by an earlier pass that died before clearing the mark:
    the operation that triggers cleanup still succeeds (asserted in the trigger)."""
    gone = project.agent("ag-3434a1", status="merged", worktree=False)
    project.tree.update(gone.id, branch_pending_delete=gone.branch)
    git(project.root, "branch", "-D", gone.branch)
    other = project.agent("ag-3434a2", status="discarded", worktree=False)
    project.tree.update(other.id, branch_pending_delete=other.branch)

    TRIGGERS[trigger](project)

    assert not branch_exists(project.root, other.branch), trigger


def test_sg_r2_container_then_host_end_to_end(project, nested, readonly_git):
    """The live-test shape, locally: the deletion fails in the container, then
    the host's next reconcile removes the branch."""
    parent, child = nested
    project.runner.merge_agent(child.id, into=parent.worktree)
    assert branch_exists(project.root, child.branch)

    readonly_git.chmod(readonly_git.stat().st_mode | stat.S_IWUSR)
    project.reconcile()

    assert not branch_exists(project.root, child.branch)
    assert branch_exists(project.root, parent.branch), "the live parent keeps its branch"


# ==========================================================================
# 3. Controls: with a writable .git, deletion is immediate and nothing is recorded
# ==========================================================================

def test_sg_r2_control_merge_agent_deletes_at_once_and_records_nothing(project, nested):
    parent, child = nested
    result = project.runner.merge_agent(child.id, into=parent.worktree)

    assert result.get("result") == "merged", result
    assert not branch_exists(project.root, child.branch)
    assert not project.raw(child.id).get("branch_pending_delete"), project.raw(child.id)


def test_sg_r2_control_discard_agent_deletes_at_once_and_records_nothing(project, nested):
    _, child = nested
    result = project.runner.discard_agent(child.id, force=True)

    assert result.get("discarded") is True, result
    assert not branch_exists(project.root, child.branch)
    assert not project.raw(child.id).get("branch_pending_delete"), project.raw(child.id)
