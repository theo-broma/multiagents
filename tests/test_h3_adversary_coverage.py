"""Adversary coverage for H3: behaviours that hold today but that no existing
test pins, found by mutation (each test names the mutation it kills).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from multiagents import cli, gitops
from multiagents.paths import ProjectPaths
from multiagents.tree import Node

from test_h3_host_git import agent_branch, git, isolated_git, program, repo  # noqa: F401


def _node(wt: Path, branch: str = "agents/worker/one") -> Node:
    return Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
                depth=1, status="cancelled", pid=0, branch=branch,
                worktree=str(wt), task="work")


def test_resume_checkpoint_skips_hooks(tmp_path):
    """Kills: `_save_interrupted` calling commit_all without root (unscoped)."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "resume-hook-ran"
    program(root / ".git" / "hooks" / "pre-commit", sentinel)
    (wt / "pending.txt").write_text("pending\n")
    assert cli._save_interrupted(_node(wt), root=root)
    assert not sentinel.exists(), "resume checkpoint ran a host hook"
    assert git(root, "show", "agents/worker/one:pending.txt").stdout == "pending\n"


def test_resume_checkpoint_refuses_selected_clean_filter(tmp_path):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "clean-ran"
    program(tmp_path / "clean", sentinel)
    git(root, "config", "filter.probe.clean", str(tmp_path / "clean"))
    (wt / ".gitattributes").write_text("payload.bin filter=probe\n")
    (wt / "payload.bin").write_bytes(b"raw")
    before = git(root, "rev-parse", "agents/worker/one").stdout.strip()
    assert not cli._save_interrupted(_node(wt), root=root)
    assert not sentinel.exists()
    assert git(root, "rev-parse", "agents/worker/one").stdout.strip() == before


@pytest.mark.parametrize("nested", [False, True])
def test_filtered_refusal_covers_staged_and_nested_paths(tmp_path, nested):
    """Kills: `_filtered_paths` ignoring `diff --cached`, or nested paths."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    git(root, "config", "filter.probe.clean", "cat")
    name = "deep/dir/payload.bin" if nested else "payload.bin"
    (wt / ".gitattributes").write_text("*.bin filter=probe\n")
    git(wt, "add", ".gitattributes")
    git(wt, "commit", "-q", "-m", "attrs")
    (wt / name).parent.mkdir(parents=True, exist_ok=True)
    (wt / name).write_bytes(b"raw")
    # The agent staged it itself, so only `diff --cached` lists it.
    git(wt, "-c", "filter.probe.clean=", "add", name)
    before = git(root, "rev-parse", "agents/worker/one").stdout.strip()
    result = gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    assert not result.ok and name in result.err, result
    assert git(root, "rev-parse", "agents/worker/one").stdout.strip() == before


def test_filtered_refusal_counts_a_bare_set_attribute(tmp_path):
    """`payload.bin filter` (set, no value) still selects a filter attribute."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    (wt / ".gitattributes").write_text("payload.bin filter\n")
    (wt / "payload.bin").write_bytes(b"raw")
    result = gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    assert not result.ok and "payload.bin" in result.err, result


def _sibling(root: Path) -> Path:
    other = ProjectPaths(root).worktree("ag-two")
    gitops.create_worktree(root, other, "agents/worker/two", base="main", unique=False)
    return other


@pytest.mark.parametrize("entry", ["resume", "commit_all"])
def test_rewritten_worktree_head_is_refused(tmp_path, entry):
    """Kills: dropping the HEAD-vs-record check in `_host_scope`, or a caller
    that stops passing the recorded branch. The agent rewrites its own
    registration HEAD to a sibling's branch; the host must not commit there."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    _sibling(root)
    sibling_before = git(root, "rev-parse", "agents/worker/two").stdout.strip()
    (root / ".git" / "worktrees" / wt.name / "HEAD").write_text(
        "ref: refs/heads/agents/worker/two\n")
    (wt / "pending.txt").write_text("pending\n")
    if entry == "resume":
        assert not cli._save_interrupted(_node(wt), root=root)
    else:
        with pytest.raises(gitops.GitError, match="host_authority_mismatch"):
            gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    assert git(root, "rev-parse", "agents/worker/two").stdout.strip() == sibling_before


def test_merge_into_parent_with_rewritten_head_is_refused(tmp_path):
    root = repo(tmp_path)
    paths = ProjectPaths(root)
    parent = paths.worktree("ag-parent")
    gitops.create_worktree(root, parent, "agents/parent/one", base="main", unique=False)
    wt = agent_branch(root, tmp_path)
    _sibling(root)
    sibling_before = git(root, "rev-parse", "agents/worker/two").stdout.strip()
    (root / ".git" / "worktrees" / parent.name / "HEAD").write_text(
        "ref: refs/heads/agents/worker/two\n")
    status, detail = gitops.merge(parent, "agents/worker/one", "m", root=root,
                                  target_branch="agents/parent/one")
    assert status == "failed" and "host_authority_mismatch" in detail, (status, detail)
    assert git(root, "rev-parse", "agents/worker/two").stdout.strip() == sibling_before


def test_remove_worktree_refuses_forged_sibling_registration(tmp_path):
    """HG-R1's own verification: a forged gitdir in another node's registration
    is not acted on. Kills: dropping `_registration_branch` from remove."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    other = _sibling(root)
    # Another node's registration now claims this node's path.
    (root / ".git" / "worktrees" / other.name / "gitdir").write_text(f"{wt / '.git'}\n")
    with pytest.raises(gitops.GitError, match="host_authority_mismatch"):
        gitops.remove_worktree(root, wt, force=True)
    assert (root / ".git" / "worktrees" / other.name).is_dir()


def test_scoped_prune_refuses_branch_mismatch_and_duplicates(tmp_path):
    """Kills: `_prune_path` skipping a branch mismatch, or dropping its
    duplicate check."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    other = _sibling(root)
    import shutil
    shutil.rmtree(wt)
    # A mismatching branch for the stale entry is refused, not pruned.
    with pytest.raises(gitops.GitError, match="host_authority_mismatch"):
        gitops.prune_worktree(root, wt, "agents/worker/zzz")
    assert (root / ".git" / "worktrees" / wt.name).is_dir()
    # A second registration claiming the same dead path is refused, too.
    (root / ".git" / "worktrees" / other.name / "gitdir").write_text(f"{wt / '.git'}\n")
    with pytest.raises(gitops.GitError, match="host_authority_mismatch"):
        gitops.prune_worktree(root, wt, "agents/worker/one")
    assert (root / ".git" / "worktrees" / other.name).is_dir()
    assert (root / ".git" / "worktrees" / wt.name).is_dir()

