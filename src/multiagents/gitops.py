"""Branch and worktree lifecycle.

A branch alone cannot isolate parallel agents: ``git checkout`` is global to a
working tree, so two agents on two branches in one directory overwrite each
other within seconds. Every writing agent therefore gets a real ``git worktree``
— its own checkout of its own branch, sharing the repository's object store.

Those worktrees live outside the project (see :mod:`multiagents.paths`) so an
agent's file search cannot reach a sibling's checkout. That has one consequence
worth knowing: a linked worktree's ``.git`` file records an **absolute** path
back to the main repository, and the repository records an absolute path back to
the worktree. Any future containerisation must mount both at their exact host
paths or git breaks in confusing ways.

The parent performs every operation here. Subagents only commit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class GitResult:
    ok: bool
    out: str
    err: str
    code: int


def run(repo: Path, *args: str, check: bool = False, timeout: int = 120) -> GitResult:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    result = GitResult(proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip(), proc.returncode)
    if check and not result.ok:
        raise GitError(f"git {' '.join(args)} failed: {result.err or result.out}")
    return result


def is_repo(path: Path) -> bool:
    return run(path, "rev-parse", "--git-dir").ok


def init_repo(path: Path) -> GitResult:
    """``git init`` in an existing directory."""
    return run(path, "init")


def initial_commit(repo: Path, message: str = "initial commit") -> GitResult:
    """The first commit, which every agent branch is cut from.

    ``--allow-empty`` so a brand-new project with no files yet still gets a
    commit: without one there is nothing to branch a worktree from.
    """
    run(repo, "add", "-A")
    return run(repo, "commit", "--allow-empty", "-m", message)


def uncommitted_entries(repo: Path) -> list[str]:
    """Paths ``git status`` reports, directories collapsed to one entry.

    Collapsing matters for the caller: a first commit of a project with
    ``node_modules`` is one line to show the user, not forty thousand.
    """
    result = run(repo, "status", "--porcelain", "-unormal")
    return [line[3:].strip().strip('"') for line in result.out.splitlines() if line[3:].strip()]


def ensure_repo(path: Path) -> None:
    if not is_repo(path):
        raise GitError(
            f"{path} is not a git repository. Agents work on branches, so this "
            f"project needs one — run `git init` (and make at least one commit)."
        )


def has_commits(repo: Path) -> bool:
    return run(repo, "rev-parse", "--verify", "HEAD").ok


def current_branch(repo: Path) -> str:
    result = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.out if result.ok else ""


def head_sha(repo: Path) -> str:
    result = run(repo, "rev-parse", "HEAD")
    return result.out if result.ok else ""


def is_dirty(repo: Path) -> bool:
    result = run(repo, "status", "--porcelain")
    return bool(result.out.strip())


def branch_exists(repo: Path, branch: str) -> bool:
    return run(repo, "rev-parse", "--verify", f"refs/heads/{branch}").ok


def unique_branch(repo: Path, desired: str) -> str:
    """Avoid colliding with a branch left behind by an earlier run."""
    if not branch_exists(repo, desired):
        return desired
    for suffix in range(2, 100):
        candidate = f"{desired}-{suffix}"
        if not branch_exists(repo, candidate):
            return candidate
    raise GitError(f"could not find a free branch name near {desired!r}")


def create_worktree(repo: Path, path: Path, branch: str, base: str = "") -> str:
    """Create `path` as a new worktree on a fresh `branch` cut from `base`."""
    ensure_repo(repo)
    if not has_commits(repo):
        raise GitError(
            "This repository has no commits yet. Make an initial commit before "
            "spawning agents — a worktree cannot be branched from nothing."
        )
    branch = unique_branch(repo, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", str(path), "-b", branch]
    if base:
        args.append(base)
    run(repo, *args, check=True, timeout=300)
    return branch


def remove_worktree(repo: Path, path: Path, force: bool = False) -> GitResult:
    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    result = run(repo, *args, timeout=180)
    if not result.ok:
        # A directory deleted by hand leaves a stale registration behind.
        run(repo, "worktree", "prune")
    return result


def prune_worktrees(repo: Path) -> GitResult:
    return run(repo, "worktree", "prune")


def delete_branch(repo: Path, branch: str, force: bool = False) -> GitResult:
    return run(repo, "branch", "-D" if force else "-d", branch)


def commits_on(repo: Path, branch: str, base: str) -> int:
    """How many commits `branch` has that `base` does not."""
    result = run(repo, "rev-list", "--count", f"{base}..{branch}")
    try:
        return int(result.out) if result.ok else 0
    except ValueError:
        return 0


def diff_stat(repo: Path, branch: str, base: str) -> str:
    result = run(repo, "diff", "--stat", f"{base}...{branch}")
    return result.out if result.ok else ""


def commit_all(worktree: Path, message: str) -> GitResult:
    """Commit whatever an agent left uncommitted, so no work is stranded."""
    run(worktree, "add", "-A")
    if not run(worktree, "diff", "--cached", "--quiet").ok:
        return run(worktree, "commit", "-m", message)
    return GitResult(True, "nothing to commit", "", 0)


def merge(repo: Path, branch: str, message: str, style: str = "squash") -> tuple[str, str]:
    """Merge `branch` into whatever `repo` currently has checked out.

    Returns ``(status, detail)`` where status is ``merged``, ``empty``,
    ``conflict`` or ``failed``. A conflict is aborted cleanly and reported —
    the branch survives so the caller can decide what to do with it.
    """
    if is_dirty(repo):
        return "failed", "target worktree has uncommitted changes; commit or stash first"

    if style == "squash":
        result = run(repo, "merge", "--squash", branch, timeout=300)
        if not result.ok:
            run(repo, "merge", "--abort")
            run(repo, "reset", "--hard")
            return "conflict", result.err or result.out
        if run(repo, "diff", "--cached", "--quiet").ok:
            return "empty", "branch introduced no changes"
        commit = run(repo, "commit", "-m", message, timeout=120)
        if not commit.ok:
            return "failed", commit.err or commit.out
        return "merged", commit.out

    result = run(repo, "merge", "--no-ff", "-m", message, branch, timeout=300)
    if result.ok:
        return "merged", result.out
    run(repo, "merge", "--abort")
    return "conflict", result.err or result.out


def push(repo: Path, remote: str, branch: str) -> GitResult:
    """Push a branch. Only ever called explicitly — never as a side effect of
    finishing a run, because publishing is not reversible."""
    if not remote:
        return GitResult(False, "", "no remote configured (git.remote is empty)", 1)
    return run(repo, "push", "-u", remote, branch, timeout=600)
