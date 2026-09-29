"""Adversary round 2: `multiagents stop` takes its checkpoint operands from tree.json.

`cmd_stop` commits each stopped node's worktree with `node.worktree` and
`node.branch` read from tree.json, which every agent can write. The host
record (HA-R1) is never consulted, unlike the resume path. HA-R2 lists the
stop/resume reconciliation among the host mutations whose operands come from
the record, and H1's adversary brief names "change the base branch or
`paths.root`" as the first goal.
"""
from __future__ import annotations

import argparse
import json

from multiagents import cli
from multiagents.authority import HostAuthority
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Node, Tree

from test_h3_host_git import git, isolated_git, repo  # noqa: F401
from multiagents import gitops


def _project(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(argparse.Namespace(path=str(root), force=False, nested=False)) == 0
    paths = ProjectPaths(root)
    tree = Tree(paths.tree_file, paths.events_file)
    authority = HostAuthority(paths, tree)          # seeds from an empty tree

    async def stopped(self, agent_id):
        self.tree.set_status(agent_id, "cancelled", "stopped")
        return {"agent_id": agent_id, "status": "cancelled"}

    monkeypatch.setattr(Runner, "stop", stopped)
    return root, paths, tree, authority


def _spawn(root, paths, tree, authority, node_id, branch):
    wt = paths.worktree(node_id)
    gitops.create_worktree(root, wt, branch, base="main", unique=False)
    node = Node(id=node_id, agent="worker", provider="p", model="m", parent=None,
                depth=1, status="running", pid=0, branch=branch,
                worktree=str(wt), task="work")
    authority.add(node)
    tree.add(node)
    return wt


def _forge(paths, node_id, **fields):
    data = json.loads(paths.tree_file.read_text())
    data["nodes"][node_id].update(fields)
    paths.tree_file.write_text(json.dumps(data))


def test_stop_never_commits_the_main_checkout_onto_the_base_branch(tmp_path, monkeypatch):
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    _spawn(root, paths, tree, authority, "ag-one", "agents/worker/one")
    # The user's own uncommitted work in the main checkout.
    (root / "base.txt").write_text("user's half-finished edit\n")
    main_before = git(root, "rev-parse", "main").stdout.strip()

    # From the container: point its own node at the main checkout and base branch.
    _forge(paths, "ag-one", worktree=str(root), branch="main")

    cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))

    assert git(root, "rev-parse", "main").stdout.strip() == main_before, (
        "the host committed the user's main checkout onto the base branch: "
        + git(root, "log", "-1", "--format=%s", "main").stdout)
    assert (root / "base.txt").read_text() == "user's half-finished edit\n"


def test_stop_never_commits_into_a_sibling_host_branch(tmp_path, monkeypatch):
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    _spawn(root, paths, tree, authority, "ag-one", "agents/worker/one")
    sibling = _spawn(root, paths, tree, authority, "ag-two", "agents/worker/two")
    # The sibling is not being stopped (it already ended), its checkout is dirty.
    tree.set_status("ag-two", "done", "")
    (sibling / "draft.txt").write_text("sibling's uncommitted draft\n")
    before = git(root, "rev-parse", "agents/worker/two").stdout.strip()

    # ag-one names the sibling's checkout, with no branch (so no HEAD check).
    _forge(paths, "ag-one", worktree=str(sibling), branch="")

    cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))

    assert git(root, "rev-parse", "agents/worker/two").stdout.strip() == before, (
        "stop of ag-one moved the host-created branch of ag-two")


def test_stop_survives_a_non_string_worktree_and_still_checkpoints_the_others(tmp_path, monkeypatch):
    """HG-R9: one node's checkpoint failure never prevents the others'."""
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    _spawn(root, paths, tree, authority, "ag-one", "agents/worker/one")
    good = _spawn(root, paths, tree, authority, "ag-two", "agents/worker/two")
    (good / "honest.txt").write_text("honest work in progress\n")

    # From the container: its own entry's worktree is no longer a string.
    _forge(paths, "ag-one", worktree=1)

    try:
        cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))
    except Exception as exc:                  # noqa: BLE001
        raise AssertionError(f"`multiagents stop` crashed on one entry: "
                             f"{type(exc).__name__}: {exc}")
    assert git(root, "show", "agents/worker/two:honest.txt", check=False).stdout == \
        "honest work in progress\n", "the honest agent's work was never checkpointed"


def test_resume_survives_a_non_string_worktree_and_still_checkpoints_the_others(tmp_path, monkeypatch):
    """HG-R9: the resume path checkpoints each node independently."""
    root, paths, tree, authority = _project(tmp_path, monkeypatch)
    good = _spawn(root, paths, tree, authority, "ag-two", "agents/worker/two")
    (good / "honest.txt").write_text("honest work in progress\n")
    tree.update("ag-two", pid=999999999, pid_start="")
    # A node the container wrote itself (unrecorded), listed first, with a
    # worktree that is not a string.
    data = json.loads(paths.tree_file.read_text())
    forged = dict(data["nodes"]["ag-two"], id="ag-nested", branch="agents/nested/x",
                  worktree=1, children=[])
    data["nodes"] = {"ag-nested": forged, **data["nodes"]}
    paths.tree_file.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "_executor_problems", lambda *args: [])

    try:
        cli.cmd_resume(argparse.Namespace(
            path=str(root), no_launch=True, resume=True, wait=False,
            unattended=0, team="", supervise=True))
    except Exception as exc:                  # noqa: BLE001
        raise AssertionError(f"`multiagents resume` crashed on one entry: "
                             f"{type(exc).__name__}: {exc}")
    assert git(root, "show", "agents/worker/two:honest.txt", check=False).stdout == \
        "honest work in progress\n", "the honest agent's work was never checkpointed"
