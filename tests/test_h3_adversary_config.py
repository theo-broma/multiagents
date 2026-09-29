"""Adversary probes for HG-R4: which config the disabling list is read from.

`_content_disable_args` lists the content programs to empty by running
`git config` in the base checkout. The host call it protects then runs with a
private git dir, on the target worktree's branch. Conditional includes
evaluate differently in those two places, so a program defined under an
include that applies only to the call is never listed and never disabled.
"""
from __future__ import annotations

from pathlib import Path

from multiagents import gitops
from multiagents.paths import ProjectPaths

from test_h3_host_git import git, isolated_git, repo  # noqa: F401


def _parent_merge_with_driver(tmp_path: Path, include_header: str) -> tuple[Path, Path, Path]:
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("shared.txt merge=probe\n")
    (root / "shared.txt").write_text("common\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "shared")
    paths = ProjectPaths(root)
    parent = paths.worktree("ag-parent")
    gitops.create_worktree(root, parent, "agents/parent/one", base="main", unique=False)
    (parent / "shared.txt").write_text("parent change\n")
    git(parent, "commit", "-q", "-am", "parent")
    child = paths.worktree("ag-child")
    gitops.create_worktree(root, child, "agents/worker/child", base="main", unique=False)
    (child / "shared.txt").write_text("child change\n")
    git(child, "commit", "-q", "-am", "child")

    sentinel = tmp_path / "driver-ran"
    driver = tmp_path / "driver"
    driver.write_text(f"#!/bin/sh\n: > '{sentinel}'\ncp \"$3\" \"$2\"\n")
    driver.chmod(0o755)
    # Trusted, user-written config: the driver is defined in an included file.
    included = tmp_path / "drivers.gitconfig"
    included.write_text(f"[merge \"probe\"]\n\tdriver = {driver} %O %A %B\n")
    with (root / ".git" / "config").open("a") as fh:
        fh.write(f"[includeIf \"{include_header}\"]\n\tpath = {included}\n")
    return root, parent, sentinel


def test_onbranch_included_merge_driver_not_run_for_parent_merge(tmp_path):
    root, parent, sentinel = _parent_merge_with_driver(tmp_path, "onbranch:agents/**")
    status, detail = gitops.merge(parent, "agents/worker/child", "merge child",
                                  style="no-ff", root=root,
                                  target_branch="agents/parent/one")
    assert not sentinel.exists(), \
        f"host ran a trusted merge driver on a parent-worktree merge ({status}: {detail})"


def test_worktree_config_merge_driver_not_run_for_parent_merge(tmp_path):
    """HG-R1 for merges: a driver the parent agent defines in its own
    config.worktree. (Holds today; kills a mutation that hands git the real
    worktree git dir, which no existing test notices.)"""
    root = repo(tmp_path)
    git(root, "config", "extensions.worktreeConfig", "true")
    (root / "shared.txt").write_text("common\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "shared")
    paths = ProjectPaths(root)
    parent = paths.worktree("ag-parent")
    gitops.create_worktree(root, parent, "agents/parent/one", base="main", unique=False)
    (parent / ".gitattributes").write_text("shared.txt merge=evil\n")
    (parent / "shared.txt").write_text("parent change\n")
    git(parent, "add", "-A")
    git(parent, "commit", "-q", "-m", "parent")
    child = paths.worktree("ag-child")
    gitops.create_worktree(root, child, "agents/worker/child", base="main", unique=False)
    (child / "shared.txt").write_text("child change\n")
    git(child, "commit", "-q", "-am", "child")
    sentinel = tmp_path / "evil-driver-ran"
    evil = tmp_path / "evil"
    evil.write_text(f"#!/bin/sh\n: > '{sentinel}'\nexit 0\n")
    evil.chmod(0o755)
    git(parent, "config", "--worktree", "merge.evil.driver", f"{evil} %O %A %B")
    gitops.merge(parent, "agents/worker/child", "merge child", style="no-ff",
                 root=root, target_branch="agents/parent/one")
    assert not sentinel.exists(), "host honoured the parent agent's config.worktree"


def test_host_checkpoint_keeps_identity_from_gitdir_conditional_include(tmp_path, monkeypatch):
    """HG-R7 / CI-R*: a user whose identity comes from the common
    `[includeIf "gitdir:<project>/"]` pattern. Before H3 the host checkpoint
    commit in an agent worktree (git dir <root>/.git/worktrees/<id>) picked it
    up; it must keep doing so."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    root = repo(tmp_path)
    git(root, "config", "--unset", "user.name")
    git(root, "config", "--unset", "user.email")
    ident = tmp_path / "work-identity.gitconfig"
    ident.write_text("[user]\n\tname = Work Person\n\temail = work@example.invalid\n")
    glob = Path(__import__("os").environ["GIT_CONFIG_GLOBAL"])
    glob.write_text(f"[includeIf \"gitdir:{root.resolve()}/\"]\n\tpath = {ident}\n")
    paths = ProjectPaths(root)
    wt = paths.worktree("ag-one")
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    # Plain git in the worktree sees the identity...
    assert git(wt, "config", "user.email").stdout.strip() == "work@example.invalid"
    (wt / "pending.txt").write_text("pending\n")
    result = gitops.commit_all(wt, "checkpoint", role="worker", agent_id="ag-one",
                               root=root, branch="agents/worker/one")
    assert result.ok, result
    author = git(root, "log", "-1", "--format=%ae", "agents/worker/one").stdout.strip()
    assert author == "work@example.invalid", \
        f"host checkpoint lost the user's conditional identity: authored as {author}"
