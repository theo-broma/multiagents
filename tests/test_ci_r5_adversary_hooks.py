"""CI-R5 adversary — an agent controls its worktree's git hooks.

Contract: context/specs/commit-identity.md, CI-R5 and the "Decisions,
2026-09-27 (orchestrator, after implementer ag-b7c2ff on CI-R5)" section,
whose last paragraph specifies hook detection via a `GIT_TRACE2_EVENT` trace
and the fallback on a git too old to trace.

The threat these tests exercise: the failing hook is the AGENT'S. It runs, as
a child of the runner's `git commit`, with the runner's `GIT_TRACE2_EVENT`
pointing at a file the runner will read back. So the hook can do whatever it
likes to that file before it exits — and `commit_all` must survive it.

Black box: `gitops.commit_all`'s result and, crucially, whether it *returns at
all*. `commit_all` is the runner's own end-of-run commit path; the existing
`tests/test_commit_identity*.py` suite drives it directly the same way.

`commit_all` is synchronous and, in the runner, is called on the event loop
(`runner._finalize`, not in a thread). A `commit_all` that never returns is
therefore a hung runner — the whole server, every other agent with it — so
"does it return" is a real, contract-relevant question, not a nicety.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from multiagents import gitops

GIT = "git"


def sh(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    sh(GIT, "init", "-q", "-b", "main", str(root))
    sh(GIT, "-C", str(root), "-c", "user.name=seed", "-c", "user.email=s@example.invalid",
       "commit", "-q", "--allow-empty", "-m", "seed")
    return root


def install_hook(repo: Path, body: str, *, name: str = "pre-commit") -> Path:
    d = repo / ".git" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    hook = d / name
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)
    return hook


def call_commit_all_bounded(worktree: Path, seconds: float = 8.0):
    """Run `commit_all` in a daemon thread and wait `seconds` for it.

    Returns (finished, result). If it never returns — a hung runner — finished
    is False and the caller is responsible for releasing the thread so the test
    process can exit cleanly.
    """
    box: dict = {}

    def go() -> None:
        box["result"] = gitops.commit_all(worktree, "end of run",
                                          role="worker", agent_id="ag-adv")

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    return (not t.is_alive()), box.get("result"), t


# ==========================================================================
# Control — the fixture: a hook refusal is detected as a hook refusal
# ==========================================================================

def test_control_a_plain_failing_hook_is_reported_with_its_name(tmp_path):
    repo = make_repo(tmp_path / "r")
    install_hook(repo, 'echo REFUSED_BY_HOOK >&2\nexit 1\n')
    (repo / "work.txt").write_text("x")

    result = gitops.commit_all(repo, "m", role="worker", agent_id="ag-1")

    assert result.ok is False
    assert result.hook == "pre-commit", result
    assert "REFUSED_BY_HOOK" in result.err


# ==========================================================================
# The agent's reach — can a hook block the runner?
# ==========================================================================

def test_ci_r5_a_hook_that_turns_the_trace_into_a_fifo_does_not_hang_commit_all(tmp_path):
    """A hook exits (so `git commit` returns), but first replaces the trace
    file the runner handed it with a FIFO. `commit_all` then reads that file
    to find the refusing hook — and a blind `read_text()` on a FIFO blocks
    forever with no writer.

    The commit is refused, so `commit_all` must return a not-ok result naming
    the hook. What it must never do is fail to return. The runner calls this on
    its event loop, so a `commit_all` that blocks here is a server that stops
    servicing every agent it supervises — an agent hanging the runner from
    inside its own sandbox.
    """
    repo = make_repo(tmp_path / "r")
    marker = tmp_path / "trace-path.txt"
    # The hook records where the trace is, replaces it with a FIFO, then exits
    # non-zero so the commit is refused.
    install_hook(repo, (
        f'printf "%s" "$GIT_TRACE2_EVENT" > "{marker}"\n'
        'if [ -n "$GIT_TRACE2_EVENT" ]; then\n'
        '  rm -f "$GIT_TRACE2_EVENT"\n'
        '  mkfifo "$GIT_TRACE2_EVENT" 2>/dev/null || true\n'
        'fi\n'
        'echo REFUSED >&2\n'
        'exit 1\n'
    ))
    (repo / "work.txt").write_text("x")

    finished, result, thread = call_commit_all_bounded(repo, seconds=8.0)

    try:
        assert finished, (
            "commit_all did not return within 8s: a hook replaced the trace "
            "with a FIFO and the trace read blocks forever, hanging the runner")
        assert result is not None and result.ok is False
        assert result.hook, "the refusal is still a hook refusal"
    finally:
        # Release the blocked reader (if any) so this process can exit: open the
        # FIFO's write end, which unblocks the pending open-for-read.
        if not finished:
            try:
                path = marker.read_text().strip()
                if path and os.path.exists(path):
                    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                    os.close(fd)
            except OSError:
                pass
            thread.join(5)


def test_ci_r5_a_hook_that_makes_the_trace_unreadable_still_reports_a_hook(tmp_path):
    """A milder tamper: the hook truncates the trace to an unreadable/empty
    state (here: removes it). Detection must degrade to the fallback — the
    hook's presence alone — not to "no hook", which would send a real hook
    refusal straight to CI-R2 with no fix loop.
    """
    repo = make_repo(tmp_path / "r")
    install_hook(repo, (
        '[ -n "$GIT_TRACE2_EVENT" ] && rm -f "$GIT_TRACE2_EVENT"\n'
        'echo REFUSED >&2\n'
        'exit 1\n'
    ))
    (repo / "work.txt").write_text("x")

    result = gitops.commit_all(repo, "m", role="worker", agent_id="ag-1")

    assert result.ok is False
    assert result.hook == "pre-commit", (
        "with the trace gone, the hook's presence alone must still name it a "
        f"hook refusal (CI-R5 fallback), got hook={result.hook!r}")
