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


def repo_root(path: Path) -> Path | None:
    """The top level of the repository containing `path`, or None.

    Distinct from :func:`is_repo`, which answers "is there a repository above
    me" — inside a monorepo that is true of every subdirectory, and a project
    rooted at one would get worktrees of the whole repository without saying so.
    """
    result = run(path, "rev-parse", "--show-toplevel")
    return Path(result.out) if result.ok and result.out else None


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


def attach_worktree(repo: Path, path: Path, branch: str) -> None:
    """Check out the EXISTING `branch` at `path` as a worktree, commits intact.

    The counterpart of `create_worktree` for a run that already has a branch
    and lost its checkout: cutting a new one would fork the node's work
    (SP-R4). A registration left behind by a deleted directory is pruned
    first, or git refuses the branch as still checked out there. A branch
    genuinely checked out somewhere else is refused, never forced away.
    """
    ensure_repo(repo)
    if not branch_exists(repo, branch):
        raise GitError(f"branch {branch!r} no longer exists")
    run(repo, "worktree", "prune")
    path.parent.mkdir(parents=True, exist_ok=True)
    run(repo, "worktree", "add", str(path), branch, check=True, timeout=300)


def remove_worktree(repo: Path, path: Path, force: bool = False) -> GitResult:
    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    result = run(repo, *args, timeout=180)
    if not result.ok:
        # A directory deleted by hand leaves a stale registration behind.
        run(repo, "worktree", "prune")
    return result


def owning_repo(worktree: Path) -> Path | None:
    """The repository a linked worktree belongs to, read from its `.git` file.

    A linked worktree stores `gitdir: <repo>/.git/worktrees/<id>`. Reading it
    is how a teardown can find every repository it is about to leave a stale
    worktree registration in — the directory has to be inspected *before* it is
    deleted, because afterwards there is nothing left to ask.
    """
    marker = worktree / ".git"
    if not marker.is_file():
        return None
    text = marker.read_text().strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    # <repo>/.git/worktrees/<id> -> <repo>
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return gitdir.parent.parent.parent


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


def resolve_commit(repo: Path, ref: str) -> str:
    """The full sha `ref` names, or "" when it names no commit."""
    if not ref:
        return ""
    result = run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.out if result.ok else ""


def short_sha(repo: Path, ref: str) -> str:
    result = run(repo, "rev-parse", "--short", ref)
    return result.out if result.ok else ""


def holds_unmerged_commits(repo: Path, head: str, base: str, since: str = "") -> bool:
    """Whether `head` has commits whose changes `base` does not already hold.

    Absorbed means merging `head` into `base` would change no file, which is
    what a squash merge leaves behind: the branch's own shas never reach base,
    but its content does. A conflicting merge, or a git too old to answer, is
    counted as holding work — the safe side of this question is "yes".

    `since` is where the branch started, when that is known. Only commits
    after it are the branch's own: without it, a base that was amended or
    moved backwards leaves the commit the branch was cut from looking like
    work of its own.
    """
    if run(repo, "merge-base", "--is-ancestor", head, base).ok:
        return False
    args = ["merge-tree", "--write-tree"]
    if since:
        own = run(repo, "rev-list", "--count", head, "--not", base, since)
        if own.ok and own.out == "0":
            return False
        if run(repo, "merge-base", "--is-ancestor", since, head).ok:
            args.append(f"--merge-base={since}")
    merged = run(repo, *args, base, head)
    if not merged.ok or not merged.out:
        return True
    base_tree = run(repo, "rev-parse", f"{base}^{{tree}}")
    return not base_tree.ok or merged.out.splitlines()[0] != base_tree.out


def untracked_in_the_way(worktree: Path, head: str, target: str) -> str:
    """A path moving `worktree` from `head` to `target` would overwrite, or "".

    Such a path is one `target` tracks and `head` does not, which exists in
    the worktree as something git is not tracking — an ignored file, most
    often — with other content than `target` gives it. `reset --keep` refuses
    an untracked file there but overwrites an ignored one. A file (or a
    directory) standing where `target` needs a directory counts too. When git
    cannot answer, the first path it could not rule out is returned.
    """
    diff = run(worktree, "diff", "--raw", "--no-abbrev", "--no-renames", "-z",
               head, target)
    if not diff.ok:
        return diff.err or "git diff failed"
    fields = diff.out.split("\0")
    added: dict[str, str] = {}
    removed: set[str] = set()
    for meta, path in zip(fields[0::2], fields[1::2]):
        parts = meta.split()
        if len(parts) < 5:
            continue
        if parts[4] == "A":
            added[path] = parts[3]
        elif parts[4] == "D":
            removed.add(path)
    for path, blob in added.items():
        segments = path.split("/")
        for depth in range(1, len(segments)):
            prefix = "/".join(segments[:depth])
            spot = worktree / prefix
            if prefix not in removed and (spot.is_symlink() or
                                          (spot.exists() and not spot.is_dir())):
                return prefix
        spot = worktree / path
        if not (spot.exists() or spot.is_symlink()):
            continue
        if spot.is_symlink() or not spot.is_file():
            return path
        same = run(worktree, "hash-object", "--", path)
        if not same.ok or same.out != blob:
            return path
    return ""


def diff_stat(repo: Path, branch: str, base: str) -> str:
    result = run(repo, "diff", "--stat", f"{base}...{branch}")
    return result.out if result.ok else ""


def changed_paths(repo: Path, branch: str, base: str,
                  filters: str = "MDR") -> list[str]:
    """Repo-relative paths `branch` changed, restricted to those change kinds.

    The default excludes additions on purpose. A protected file that an agent
    ADDS cannot weaken anything — a new test is a new test — while modifying,
    deleting or renaming one is how a contract gets quietly edited to fit the
    code. That distinction is the whole reason this is a diff filter and not a
    filesystem permission: `chmod -w` cannot express "you may add but not
    rewrite", and this can.
    """
    result = run(repo, "diff", "--name-only", f"--diff-filter={filters}",
                 f"{base}...{branch}")
    if not result.ok:
        return []
    return [line.strip() for line in result.out.splitlines() if line.strip()]


def restore_paths(worktree: Path, base: str, paths: list[str],
                  message: str) -> GitResult:
    """Put these paths back to their `base` content and commit, in a worktree.

    Used to undo an agent's edits to files it was not allowed to modify, before
    its branch is merged. Runs in the agent's own worktree because that is
    where its branch is checked out; the caller has already established that
    the worktree still exists.
    """
    if not paths:
        return GitResult(True, "nothing to restore", "", 0)
    restore = run(worktree, "checkout", base, "--", *paths)
    if not restore.ok:
        return restore
    if run(worktree, "diff", "--cached", "--quiet").ok:
        return GitResult(True, "paths already matched base", "", 0)
    return run(worktree, "commit", "-m", message)


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
