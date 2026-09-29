"""Adversary round 2 (held): conversation refresh's `reset --keep` and a branch symref.

The agent's recorded branch ref sits under the container-writable
`refs/heads/agents/` and can be made a symref to a sibling's host branch;
`placed_on` in tree.json can be forged to defeat the own-work guard.
`reset_keep` still refuses, because `git symbolic-ref HEAD` resolves the
whole chain and so no longer equals the recorded branch. Kept as regression
coverage for that property.
"""
from __future__ import annotations

from multiagents import gitops
from multiagents.paths import ProjectPaths
from multiagents.tree import Node

from test_h3_host_git import git, isolated_git, repo, runner  # noqa: F401


def _two_agents(root):
    paths = ProjectPaths(root)
    own = paths.worktree("ag-conv")
    gitops.create_worktree(root, own, "agents/advisor/conv", base="main", unique=False)
    sibling = paths.worktree("ag-two")
    gitops.create_worktree(root, sibling, "agents/worker/two", base="main", unique=False)
    (sibling / "work.txt").write_text("the sibling's only copy of its work\n")
    git(sibling, "add", "-A")
    git(sibling, "commit", "-q", "-m", "sibling work")
    # Base moves on, so the conversation is "behind" and gets refreshed.
    (root / "later.txt").write_text("later\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "later on main")
    return paths, own, sibling


def _forge(root, own):
    """From the container: the conversation's own branch ref becomes a symref
    to the sibling's branch, and its checkout is made to match that tip."""
    (root / ".git" / "refs" / "heads" / "agents" / "advisor" / "conv").write_text(
        "ref: refs/heads/agents/worker/two\n")
    git(own, "reset", "-q", "--hard", "HEAD")


def test_reset_keep_never_moves_a_ref_behind_the_recorded_branch(tmp_path):
    root = repo(tmp_path)
    paths, own, sibling = _two_agents(root)
    sibling_tip = git(root, "rev-parse", "agents/worker/two").stdout.strip()
    main_sha = git(root, "rev-parse", "main").stdout.strip()
    _forge(root, own)

    try:
        gitops.reset_keep(own, main_sha, "agents/advisor/conv", root=root)
    except gitops.GitError:
        pass

    assert git(root, "rev-parse", "agents/worker/two").stdout.strip() == sibling_tip, (
        "reset_keep on ag-conv moved the sibling's branch agents/worker/two")


def test_conversation_refresh_never_resets_a_sibling_host_branch(tmp_path):
    root = repo(tmp_path)
    paths, own, sibling = _two_agents(root)
    r = runner(root)
    conv = Node(id="ag-conv", agent="advisor", provider="p", model="m", parent=None,
                depth=1, branch="agents/advisor/conv", worktree=str(own),
                status="idle", task="advise", conversation=True)
    two = Node(id="ag-two", agent="worker", provider="p", model="m", parent=None,
               depth=1, branch="agents/worker/two", worktree=str(sibling),
               status="done", task="work")
    for n in (conv, two):
        r.authority.add(n)
        r.tree.add(n)
    sibling_tip = git(root, "rev-parse", "agents/worker/two").stdout.strip()
    main_sha = git(root, "rev-parse", "main").stdout.strip()
    _forge(root, own)
    # tree.json's placed_on, container-written, now names the sibling's tip.
    r.tree.update("ag-conv", placed_on=sibling_tip)

    node = r.authoritative(r.tree.get("ag-conv"), "conversation_refresh")
    r._refresh_conversation(node, own, "main", main_sha)

    assert git(root, "rev-parse", "agents/worker/two").stdout.strip() == sibling_tip, (
        "a conversation refresh reset the host-created branch of ag-two to base")
