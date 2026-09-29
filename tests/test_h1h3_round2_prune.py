"""Adversary round 2: `HostAuthority.remove_worktree` ends with a global prune.

HG-R1: "There is no unscoped `git worktree prune`. Pruning touches only the
registration of the node being cleaned, never entries beyond it. Verified by:
a forged `gitdir` in another node's registration is not acted on, and a
cleanup prunes only its own entry."

`authority.py` runs `git worktree prune --expire now` in the base repository
after every anchored removal, which drops every registration whose `gitdir`
names a missing path -- including another live node's.
"""
from __future__ import annotations

from multiagents import gitops
from multiagents.authority import HostAuthority
from multiagents.paths import ProjectPaths
from multiagents.tree import Node, Tree

from test_h3_host_git import isolated_git, repo  # noqa: F401


def test_recorded_cleanup_prunes_only_its_own_registration(tmp_path):
    root = repo(tmp_path)
    paths = ProjectPaths(root)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    authority = HostAuthority(paths, tree)
    nodes = {}
    for node_id in ("ag-one", "ag-two"):
        wt = paths.worktree(node_id)
        branch = f"agents/worker/{node_id}"
        gitops.create_worktree(root, wt, branch, base="main", unique=False)
        node = Node(id=node_id, agent="worker", provider="p", model="m", parent=None,
                    depth=1, status="done", pid=0, branch=branch, worktree=str(wt),
                    task="t")
        authority.add(node)
        nodes[node_id] = wt

    # From the container: the sibling's registration names a path that does not exist.
    sibling_reg = root / ".git" / "worktrees" / nodes["ag-two"].name
    (sibling_reg / "gitdir").write_text(str(tmp_path / "gone" / ".git") + "\n")

    assert authority.remove_worktree(nodes["ag-one"], recorded=True)

    assert not (root / ".git" / "worktrees" / nodes["ag-one"].name).exists()
    assert sibling_reg.is_dir(), (
        "cleaning ag-one pruned ag-two's registration (an entry beyond its own)")
