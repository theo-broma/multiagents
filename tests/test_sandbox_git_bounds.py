"""Sandbox and git — SG-R4, bounds on what a pinned read takes from the agent
(`context/specs/sandbox-git.md`, Decisions "after the stopped adversarial
reading ag-044d7b": "Pinned-read copy cap").

A pinned read (`gitops.is_dirty(path, root=...)` and friends) uses the
worktree's metadata dir `<root>/.git/worktrees/<node id>/`, which the
container can write. Its `index` is the agent's to shape, so a pinned read
must not take whatever it finds there:

- an index larger than 128 MiB makes the read raise `gitops.GitError` (the
  runner turns that into `git_unreadable`), instead of being copied at every
  3 s status poll;
- an index that is a FIFO or a symlink is refused the same way, and the read
  returns promptly rather than blocking on the FIFO.

The oversized index is a sparse file, so it costs no disk. Only the reads
that consult the index are exercised (`is_dirty`, `uncommitted_entries`,
`status`); what `commits_on` or `diff_stat` do with a bad index the Decision
does not say.

Black box: only the public read functions and the files the agent could
write. The control (a normal small index) passes today and must stay green.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from multiagents import gitops

IDENT = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid"]
NODE_ID = "ag-b0a4d1"
BRANCH = "agents/tester/b0a4d1"
CAP = 128 * 1024 * 1024
PROMPT = 15.0       # seconds; a read that blocks on a FIFO never returns


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def world(tmp_path):
    """(root, worktree, index path) — a clean agent worktree of a project."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a").write_text("a\n")
    git(root, "add", "a")
    git(root, *IDENT, "commit", "-q", "--no-verify", "-m", "seed")
    wt = tmp_path / "worktrees" / NODE_ID
    gitops.create_worktree(root, wt, BRANCH, base="main", unique=False)
    index = root / ".git" / "worktrees" / NODE_ID / "index"
    assert index.is_file(), "fixture: the worktree has its own index"
    assert gitops.is_dirty(wt, root=root) is False, "fixture: clean worktree"
    return root, wt, index


READS = {
    "is_dirty": lambda wt, root: gitops.is_dirty(wt, root=root),
    "uncommitted_entries": lambda wt, root: gitops.uncommitted_entries(wt, root=root),
    "status": lambda wt, root: gitops.status(wt, root=root),
}


def call_promptly(fn, unblock=None):
    """`fn()`'s result, or the exception it raised; fails the test if it has
    not returned within PROMPT seconds. `unblock` is run on a timeout so a
    thread stuck on a FIFO can finish."""
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:       # noqa: BLE001 — handed to the test
            box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(PROMPT)
    if thread.is_alive():
        if unblock:
            unblock()
        thread.join(5)
        pytest.fail(f"the read did not return within {PROMPT}s")
    return box


def open_fifo_both_ends(path: Path) -> None:
    """Give a FIFO a reader and a writer, then close them: whoever blocked in
    open() on it wakes up and sees EOF."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", READS)
def test_sg_r4_control_a_normal_small_index_is_read(world, name):
    root, wt, _ = world
    (wt / "a").write_text("changed\n")
    box = call_promptly(lambda: READS[name](wt, root))
    assert "error" not in box, f"{name} raised {box.get('error')!r}"
    if name == "is_dirty":
        assert box["value"] is True
    elif name == "uncommitted_entries":
        assert box["value"] == ["a"]
    else:
        assert box["value"].ok and "a" in box["value"].out


# ---------------------------------------------------------------------------
# the cap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", READS)
def test_sg_r4_index_over_128_mib_raises_git_error(world, name):
    root, wt, index = world
    index.unlink()
    with open(index, "wb") as f:
        f.truncate(CAP + 1)                 # sparse: no blocks allocated
    box = call_promptly(lambda: READS[name](wt, root))
    assert isinstance(box.get("error"), gitops.GitError), (
        f"{name} with a {CAP + 1}-byte index returned {box.get('value')!r} / "
        f"raised {box.get('error')!r}, expected GitError (cap is 128 MiB)")


# ---------------------------------------------------------------------------
# FIFO and symlink in place of the index
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", READS)
def test_sg_r4_index_fifo_raises_git_error_promptly(world, name):
    root, wt, index = world
    index.unlink()
    os.mkfifo(index)
    try:
        box = call_promptly(lambda: READS[name](wt, root),
                            unblock=lambda: open_fifo_both_ends(index))
    finally:
        open_fifo_both_ends(index)
    assert isinstance(box.get("error"), gitops.GitError), (
        f"{name} with a FIFO index returned {box.get('value')!r} / "
        f"raised {box.get('error')!r}, expected GitError")


@pytest.mark.parametrize("name", READS)
def test_sg_r4_index_symlink_raises_git_error(world, name, tmp_path):
    """The symlink points at a perfectly valid index (the worktree's own,
    moved aside), so only the refusal to follow it can make the read fail."""
    root, wt, index = world
    outside = tmp_path / "outside-index"
    index.rename(outside)
    index.symlink_to(outside)
    before = outside.read_bytes()
    box = call_promptly(lambda: READS[name](wt, root))
    assert isinstance(box.get("error"), gitops.GitError), (
        f"{name} with a symlinked index returned {box.get('value')!r} / "
        f"raised {box.get('error')!r}, expected GitError")
    assert outside.read_bytes() == before, "the symlink's target was written"


@pytest.mark.parametrize("name", READS)
def test_sg_r4_index_symlink_to_fifo_raises_git_error_promptly(world, name, tmp_path):
    root, wt, index = world
    fifo = tmp_path / "outside-fifo"
    os.mkfifo(fifo)
    index.unlink()
    index.symlink_to(fifo)
    try:
        box = call_promptly(lambda: READS[name](wt, root),
                            unblock=lambda: open_fifo_both_ends(fifo))
    finally:
        open_fifo_both_ends(fifo)
    assert isinstance(box.get("error"), gitops.GitError), (
        f"{name} returned {box.get('value')!r} / raised {box.get('error')!r}")
