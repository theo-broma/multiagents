"""Adversary round 2: `merge_agent(into=...)` for a target the record does not name.

`merge_agent` passes `target_branch` to `gitops.merge` only when `into` is,
as a string, some record's worktree. For any other target `_host_scope` gets
`branch=""`, skips HG-R8's comparison, and follows whichever ref the
target's registration HEAD names -- a file in `.git/worktrees/<id>/`, which
the container writes. The merge commit is then put on that ref with
`update-ref`, which may be the base branch.
"""
from __future__ import annotations

from multiagents import gitops
from multiagents.paths import ProjectPaths
from multiagents.tree import Node

from test_h3_host_git import git, isolated_git, repo, runner  # noqa: F401


def _host_child(r, root, paths):
    wt = paths.worktree("ag-one")
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    (wt / "agent.txt").write_text("agent\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "agent")
    n = Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
             depth=1, branch="agents/worker/one", worktree=str(wt),
             status="done", task="work")
    r.authority.add(n)
    r.tree.add(n)


def test_merge_into_a_nested_worktree_whose_head_names_main_never_moves_main(tmp_path):
    root = repo(tmp_path)
    paths = ProjectPaths(root)
    r = runner(root)
    _host_child(r, root, paths)
    # A checkout spawned inside the container (unrecorded, inside D).
    nested = paths.worktree("ag-nested")
    gitops.create_worktree(root, nested, "agents/nested/x", base="main", unique=False)
    main_before = git(root, "rev-parse", "main").stdout.strip()

    # From the container: the nested registration's HEAD now names main.
    (root / ".git" / "worktrees" / nested.name / "HEAD").write_text("ref: refs/heads/main\n")

    result = r.merge_agent("ag-one", into=str(nested))

    assert git(root, "rev-parse", "main").stdout.strip() == main_before, (
        f"merge_agent into {nested} advanced the base branch: {result}")


def test_merge_into_a_recorded_worktree_reached_through_a_symlinked_root_keeps_hg_r8(tmp_path):
    root = repo(tmp_path)
    paths = ProjectPaths(root)
    r = runner(root)
    _host_child(r, root, paths)
    parent = paths.worktree("ag-parent")
    gitops.create_worktree(root, parent, "agents/parent/p", base="main", unique=False)
    p = Node(id="ag-parent", agent="lead", provider="p", model="m", parent=None,
             depth=1, branch="agents/parent/p", worktree=str(parent),
             status="done", task="lead")
    r.authority.add(p)
    r.tree.add(p)
    main_before = git(root, "rev-parse", "main").stdout.strip()
    (root / ".git" / "worktrees" / parent.name / "HEAD").write_text("ref: refs/heads/main\n")

    # The same recorded checkout, named through a symlinked directory (as when
    # ~/.multiagents is itself a link, HA-R2a).
    alias = tmp_path / "alias"
    alias.symlink_to(parent.parent, target_is_directory=True)
    spelled = alias / parent.name
    result = r.merge_agent("ag-one", into=str(spelled))

    assert git(root, "rev-parse", "main").stdout.strip() == main_before, (
        f"merge_agent into the recorded parent advanced the base branch: {result}")
