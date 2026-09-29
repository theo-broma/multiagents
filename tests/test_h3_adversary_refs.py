"""Adversary probes for H3/H1: the host's own commits in an agent worktree.

`_host_scope` checks that the worktree's HEAD file names the recorded branch.
The branch ref itself lives in `refs/heads/agents/`, which SG-R2 leaves
writable from the container, so an agent can turn its own branch ref into a
symbolic ref. A host bookkeeping commit then follows that ref wherever it
points.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from multiagents import cli, gitops
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Node, Tree

from test_h3_host_git import agent_branch, git, isolated_git, repo  # noqa: F401


def _symref_branch_to_main(root: Path) -> str:
    """What the agent does from inside its container: rewrite its own loose
    branch ref (under the writable refs/heads/agents) as a symref to main."""
    main_before = git(root, "rev-parse", "main").stdout.strip()
    ref = root / ".git" / "refs" / "heads" / "agents" / "worker" / "one"
    ref.write_text("ref: refs/heads/main\n")
    return main_before


def test_host_commit_all_never_moves_the_base_branch(tmp_path):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    main_before = _symref_branch_to_main(root)
    (wt / "base.txt").write_text("rewritten by the agent\n")
    try:
        gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    except gitops.GitError:
        pass
    assert git(root, "rev-parse", "main").stdout.strip() == main_before, \
        "a host bookkeeping commit in an agent worktree moved the base branch"


def test_stop_checkpoint_never_moves_the_base_branch(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    main_before = _symref_branch_to_main(root)
    (wt / "base.txt").write_text("rewritten by the agent\n")
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(argparse.Namespace(path=str(root), force=False, nested=False)) == 0
    paths = ProjectPaths(root)
    Tree(paths.tree_file, paths.events_file).add(
        Node(id="ag-stop", agent="worker", provider="p", model="m", parent=None,
             depth=1, status="running", pid=0, branch="agents/worker/one",
             worktree=str(wt), task="work"))

    async def stopped(self, agent_id):
        self.tree.set_status(agent_id, "cancelled", "stopped")
        return {"agent_id": agent_id, "status": "cancelled"}

    monkeypatch.setattr(Runner, "stop", stopped)
    cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))
    assert git(root, "rev-parse", "main").stdout.strip() == main_before, \
        "`multiagents stop` committed the agent's worktree onto the base branch"
    assert (root / "base.txt").read_text() == "base\n" or \
        git(root, "show", "main:base.txt").stdout == "base\n"


def test_merge_into_parent_worktree_never_moves_the_base_branch(tmp_path):
    root = repo(tmp_path)
    paths = ProjectPaths(root)
    parent = paths.worktree("ag-parent")
    gitops.create_worktree(root, parent, "agents/parent/one", base="main", unique=False)
    child = paths.worktree("ag-child")
    gitops.create_worktree(root, child, "agents/worker/child", base="main", unique=False)
    (child / "child.txt").write_text("child\n")
    git(child, "add", "-A")
    git(child, "commit", "-q", "-m", "child")
    main_before = git(root, "rev-parse", "main").stdout.strip()
    # The parent agent, from its container, points its own branch ref at main.
    (root / ".git" / "refs" / "heads" / "agents" / "parent" / "one").write_text(
        "ref: refs/heads/main\n")
    gitops.merge(parent, "agents/worker/child", "merge child", root=root,
                 target_branch="agents/parent/one")
    assert git(root, "rev-parse", "main").stdout.strip() == main_before, \
        "a host merge into a parent worktree moved the base branch"
