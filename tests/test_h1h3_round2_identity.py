"""Adversary round 2: HG-R11 identity under conditional-include variants.

The author of a host checkpoint must be the identity plain git resolves in
that worktree, except that `config.worktree` (agent-written, HG-R1) is never
a source.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from multiagents import gitops
from multiagents.paths import ProjectPaths

from test_h3_host_git import git, isolated_git, repo  # noqa: F401

WORK = "work@example.invalid"


def _setup(tmp_path, monkeypatch):
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    root = repo(tmp_path)
    git(root, "config", "--unset", "user.name")
    git(root, "config", "--unset", "user.email")
    ident = tmp_path / "work-identity.gitconfig"
    ident.write_text(f"[user]\n\tname = Work Person\n\temail = {WORK}\n")
    return root, ident, Path(os.environ["GIT_CONFIG_GLOBAL"])


def _checkpoint_author(root: Path) -> tuple[str, str]:
    wt = ProjectPaths(root).worktree("ag-one")
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    plain = git(wt, "config", "user.email", check=False).stdout.strip()
    (wt / "pending.txt").write_text("pending\n")
    result = gitops.commit_all(wt, "checkpoint", role="worker", agent_id="ag-one",
                               root=root, branch="agents/worker/one")
    assert result.ok, result
    return plain, git(root, "log", "-1", "--format=%ae", "agents/worker/one").stdout.strip()


@pytest.mark.parametrize("condition", ["gitdir/i", "onbranch", "hasconfig"])
def test_checkpoint_identity_matches_plain_git_under_conditional_includes(
        tmp_path, monkeypatch, condition):
    root, ident, glob = _setup(tmp_path, monkeypatch)
    if condition == "gitdir/i":
        header = f'includeIf "gitdir/i:{str(root.resolve()).upper()}/"'
    elif condition == "onbranch":
        header = 'includeIf "onbranch:agents/**"'
    else:
        git(root, "remote", "add", "origin", "https://example.invalid/team/repo.git")
        header = 'includeIf "hasconfig:remote.*.url:https://example.invalid/team/**"'
    glob.write_text(f"[{header}]\n\tpath = {ident}\n")
    plain, author = _checkpoint_author(root)
    assert plain == WORK, f"precondition: plain git should see the {condition} identity"
    assert author == plain, f"host checkpoint authored as {author}, plain git uses {plain}"


def test_checkpoint_identity_from_git_config_system(tmp_path, monkeypatch):
    root, ident, glob = _setup(tmp_path, monkeypatch)
    system = tmp_path / "system.gitconfig"
    system.write_text(f"[include]\n\tpath = {ident}\n")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
    plain, author = _checkpoint_author(root)
    assert plain == WORK
    assert author == plain, f"host checkpoint authored as {author}, plain git uses {plain}"


def test_checkpoint_identity_ignores_config_worktree(tmp_path, monkeypatch):
    root, ident, glob = _setup(tmp_path, monkeypatch)
    git(root, "config", "extensions.worktreeConfig", "true")
    wt = ProjectPaths(root).worktree("ag-one")
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    # From the container: an identity only in the agent-written config.worktree.
    (root / ".git" / "worktrees" / wt.name / "config.worktree").write_text(
        "[user]\n\tname = Forged\n\temail = forged@example.invalid\n")
    (wt / "pending.txt").write_text("pending\n")
    result = gitops.commit_all(wt, "checkpoint", role="worker", agent_id="ag-one",
                               root=root, branch="agents/worker/one")
    assert result.ok, result
    author = git(root, "log", "-1", "--format=%ae", "agents/worker/one").stdout.strip()
    assert author != "forged@example.invalid"
