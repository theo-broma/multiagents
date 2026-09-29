"""Adversary probe: one agent's forged registration during `multiagents stop`.

`cmd_stop` checkpoints every stopped agent's worktree with a host
bookkeeping commit. `commit_all` raises `GitError(host_authority_mismatch)`
when the worktree's registration HEAD no longer names the recorded branch --
and an agent can rewrite its own `.git/worktrees/<id>/HEAD` from its
container. Nothing in the stop loop catches that, so one agent aborts the
checkpoint of every agent after it (and the container teardown).
"""
from __future__ import annotations

import argparse

from multiagents import cli, gitops
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Node, Tree

from test_h3_host_git import agent_branch, git, isolated_git, repo  # noqa: F401


def test_stop_survives_one_agents_forged_head_and_saves_the_others(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    bad = agent_branch(root, tmp_path)                       # agents/worker/one
    good = ProjectPaths(root).worktree("ag-two")
    gitops.create_worktree(root, good, "agents/worker/two", base="main", unique=False)
    (good / "honest.txt").write_text("honest work in progress\n")

    # The first agent, from its container, points its registration's HEAD at
    # the base branch.
    (root / ".git" / "worktrees" / bad.name / "HEAD").write_text("ref: refs/heads/main\n")
    (bad / "pending.txt").write_text("pending\n")
    main_before = git(root, "rev-parse", "main").stdout.strip()

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(argparse.Namespace(path=str(root), force=False, nested=False)) == 0
    paths = ProjectPaths(root)
    tree = Tree(paths.tree_file, paths.events_file)
    for node_id, branch, wt in (("ag-one", "agents/worker/one", bad),
                                ("ag-two", "agents/worker/two", good)):
        tree.add(Node(id=node_id, agent="worker", provider="p", model="m", parent=None,
                      depth=1, status="running", pid=0, branch=branch,
                      worktree=str(wt), task="work"))

    async def stopped(self, agent_id):
        self.tree.set_status(agent_id, "cancelled", "stopped")
        return {"agent_id": agent_id, "status": "cancelled"}

    monkeypatch.setattr(Runner, "stop", stopped)
    try:
        code = cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))
    except gitops.GitError as exc:
        raise AssertionError(f"`multiagents stop` crashed on one agent's registration: {exc}")
    assert code == 0
    assert git(root, "rev-parse", "main").stdout.strip() == main_before
    assert git(root, "show", "agents/worker/two:honest.txt", check=False).stdout == \
        "honest work in progress\n", "the honest agent's work was never checkpointed"
