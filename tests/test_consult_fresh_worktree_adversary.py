"""Adversary tests attacking Runner.consult() and gitops helpers (ticket bug-7f6ba7).

Contract: context/specs/consult-fresh-worktree.md (CF-R1 to CF-R7).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import gitops  # noqa: E402

GIT = shutil.which("git")
SESSION = "s-cf-adv"

_CLI = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

GIT = {git!r}
PROBE = Path({probe!r})
args = sys.argv[1:]
prompt, workdir = args[0], args[1]
resumed = args[args.index("--resume") + 1] if "--resume" in args else None

def git(*a):
    p = subprocess.run([GIT, "-C", workdir, *a], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

def snap():
    f = Path(workdir) / "notes.txt"
    return {{"notes": f.read_text() if f.exists() else None,
             "head": git("rev-parse", "HEAD"),
             "symref": git("symbolic-ref", "-q", "HEAD"),
             "status": git("status", "--porcelain")}}

busy = PROBE / "busy"
overlap = busy.exists()
busy.write_text(str(os.getpid()))
started = time.time_ns()
start = snap()

if "HANG" in prompt:
    time.sleep(30)
elif "SLOW" in prompt:
    time.sleep(1)
elif "FAIL" in prompt:
    sys.exit(1)
elif "ASK" in prompt:
    print(json.dumps({{"type": "text", "text": "NEED_DECISION(test): which way?\nDEFAULT: left\n"}}))
    sys.stdout.flush()
    sys.exit(0)

end = snap()
(PROBE / f"turn-{{started}}-{{os.getpid()}}.json").write_text(json.dumps({{
    "started": started, "prompt": prompt, "workdir": workdir,
    "resumed": resumed, "overlap": overlap, "start": start, "end": end}}))
try:
    busy.unlink()
except FileNotFoundError:
    pass

print(json.dumps({{"type": "text", "text": "answered", "session": {session!r}}}))
sys.stdout.flush()
'''


def git_cmd(cwd, *args, check=True):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    p = subprocess.run([GIT, "-C", str(cwd), *args], capture_output=True,
                       text=True, env={**os.environ, **env})
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr}")
    return p.stdout.strip()


class Project:
    def __init__(self, tmp_path, monkeypatch, *, base_branch=None, timeout=60):
        self.root = tmp_path / "proj"
        self.probe = tmp_path / "probe"
        self.probe.mkdir(exist_ok=True)
        script = tmp_path / "fake-agent"
        script.write_text(_CLI.format(python=sys.executable, git=GIT,
                                      probe=str(self.probe), session=SESSION))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        provider = {
            "bin": str(script),
            "spawn": {"args": ["{prompt}", "{workdir}"],
                      "resume": ["--resume", "{session_id}"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session"],
                       "rules": [{"match": {"type": "text"}, "as": "text",
                                  "fields": {"text": "text"}}]},
        }
        spec = h.AgentSpec("advisor", "fake", "m", conversational=True, timeout=timeout)
        self.root.mkdir(exist_ok=True)
        h.make_git_repo(self.root)
        (self.root / "notes.txt").write_text("v1\n")
        (self.root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
        git_cmd(self.root, "add", "notes.txt", ".gitignore")
        git_cmd(self.root, "commit", "-m", "base v1")
        self.root_branch = git_cmd(self.root, "symbolic-ref", "--short", "HEAD")
        if base_branch:
            git_cmd(self.root, "branch", base_branch)
        self.base = base_branch or self.root_branch
        # In multiagents config, base_branch must be under git:
        git_cfg = {"base_branch": base_branch} if base_branch else False
        self.runner = h.make_runner(self.root, monkeypatch,
                                    agents={"advisor": spec},
                                    providers={"fake": provider},
                                    git=git_cfg)

    def consult(self, message, timeout=60):
        return asyncio.run(self.runner.consult("advisor", message, timeout=timeout))

    def turns(self):
        records = [json.loads(p.read_text()) for p in self.probe.glob("turn-*.json")]
        return sorted(records, key=lambda r: r["started"])

    def last(self):
        turns = self.turns()
        assert turns, "the agent never ran"
        return turns[-1]

    @property
    def worktree(self):
        return Path(self.turns()[0]["workdir"])

    def advance_base(self, content, path="notes.txt", message="base moves"):
        if self.base == self.root_branch:
            (self.root / path).write_text(content)
            git_cmd(self.root, "add", path)
            git_cmd(self.root, "commit", "-m", message)
        else:
            blob = subprocess.run([GIT, "-C", str(self.root), "hash-object", "-w",
                                   "--stdin"], input=content, text=True,
                                   capture_output=True, check=True).stdout.strip()
            tmp_index = self.root / ".git" / "cf-adv-index"
            env = {**os.environ, "GIT_INDEX_FILE": str(tmp_index)}
            run = lambda *a: subprocess.run([GIT, "-C", str(self.root), *a],
                                            env=env, text=True, capture_output=True,
                                            check=True).stdout.strip()
            run("read-tree", self.base)
            run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
            tree = run("write-tree")
            commit = run("commit-tree", tree, "-p", self.base, "-m", message)
            run("update-ref", f"refs/heads/{self.base}", commit)
            tmp_index.unlink(missing_ok=True)
        return self.base_sha()

    def base_sha(self):
        return git_cmd(self.root, "rev-parse", f"refs/heads/{self.base}")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    return Project(tmp_path, monkeypatch)


# ==========================================================================
# ATTACK 1: CF-R2 OWN WORK DESTROYED (Empty commit on node's branch wiped out)
# ==========================================================================

def test_cf_r2_adversary_empty_commit_on_nodes_branch_must_not_be_destroyed(proj):
    """CF-R2: own work is never destroyed.

    Terms define own work as: "commits on the node's branch that are not on base".
    If an agent records an empty commit (e.g., git commit --allow-empty as a
    checkpoint, marker, or signal), that commit exists on the node's branch and
    is NOT on base.
    However, holds_unmerged_commits() uses `merge-tree --write-tree base head`,
    which produces base's tree because the empty commit has no tree diff.
    holds_unmerged_commits() therefore returns False!
    Then Runner._refresh_conversation() executes `git reset --keep base_sha`,
    which resets the node's branch and destroys the agent's commit!
    """
    proj.consult("turn one")
    agent_worktree = proj.worktree

    # Agent records an empty commit on its branch (own work)
    git_cmd(agent_worktree, "commit", "--allow-empty", "-m", "agent checkpoint commit")
    agent_commit = git_cmd(agent_worktree, "rev-parse", "HEAD")

    # Base advances with a new commit
    proj.advance_base("v2\n")

    # Turn two runs
    second = proj.consult("turn two")

    # CF-R2 assertion: the turn must run on the worktree as it is, and
    # own work must never be destroyed or reset.
    current_head = git_cmd(agent_worktree, "rev-parse", "HEAD")
    branch_commits = git_cmd(agent_worktree, "log", "--oneline", "-n", "5")

    assert current_head == agent_commit, (
        f"CF-R2 VIOLATION: Agent's commit {agent_commit[:8]} was destroyed! "
        f"Worktree HEAD was reset to {current_head[:8]}.\n"
        f"Branch log:\n{branch_commits}"
    )


# ==========================================================================
# ATTACK 2: CF-R2 OWN WORK DESTROYED (Ignored file overwritten by base)
# ==========================================================================

def test_cf_r2_adversary_ignored_file_silently_overwritten_by_base(proj):
    """CF-R2: An agent creates an ignored file in its worktree.

    Because git status --porcelain excludes ignored files, status is clean.
    If base subsequently adds and commits a file at that exact same path,
    `git reset --keep base_sha` will overwrite the agent's file with base's content,
    destroying the agent's untracked file without warning.
    """
    proj.consult("turn one")
    agent_worktree = proj.worktree

    # Worktree has .gitignore containing __pycache__/ and .pytest_cache/
    # Agent creates a file in .pytest_cache/
    cache_file = agent_worktree / ".pytest_cache" / "agent_cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text('{"agent_state": "critical_data"}')

    # Base force-adds a file at the same location and advances
    base_cache_file = proj.root / ".pytest_cache" / "agent_cache.json"
    base_cache_file.parent.mkdir(parents=True, exist_ok=True)
    base_cache_file.write_text('{"base": "overwritten"}')
    git_cmd(proj.root, "add", "-f", ".pytest_cache/agent_cache.json")
    git_cmd(proj.root, "commit", "-m", "base adds pytest_cache file")

    proj.consult("turn two")

    # If the file was overwritten by base, data was destroyed:
    content = cache_file.read_text()
    assert content == '{"agent_state": "critical_data"}', (
        f"CF-R2 VIOLATION: Agent's file in worktree was overwritten by base: {content!r}"
    )


# ==========================================================================
# ATTACK 3: CF-R4 RESULT FIELDS ON LOCK TIMEOUT
# ==========================================================================

def test_cf_r4_adversary_lock_timeout_missing_result_fields(proj, monkeypatch):
    """CF-R4: `consult`'s result gains `commit`, `base_commit`, and `behind`.
    The fields exist on turn 1 as well.

    When `consult` times out waiting for the per-agent lock
    (`.multiagents/consult-advisor.lock`), it returns:
        {"agent": agent_name, "error": f"... was still answering ..."}
    It completely omits `commit`, `base_commit`, and `behind`!
    The caller cannot inspect what commit the agent worktree is on or how far
    behind base it is.
    """
    proj.consult("turn one")

    # Simulate another process holding the flock on consult-advisor.lock
    lock_file = proj.runner.paths.data / "consult-advisor.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    held_fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        orig_turn = proj.runner._conversation_turn

        def short_wait_turn(agent_name, wait):
            return orig_turn(agent_name, 0.05)

        monkeypatch.setattr(proj.runner, "_conversation_turn", short_wait_turn)

        res = proj.consult("second consult")

        # The consult timed out waiting for the lock
        assert "error" in res, res
        # CF-R4 requires result to carry commit, base_commit, behind
        assert "commit" in res, (
            f"CF-R4 VIOLATION: 'commit' is missing from consult lock timeout result: {res}"
        )
        assert "base_commit" in res, (
            f"CF-R4 VIOLATION: 'base_commit' is missing from consult lock timeout result: {res}"
        )
        assert "behind" in res, (
            f"CF-R4 VIOLATION: 'behind' is missing from consult lock timeout result: {res}"
        )
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)


# ==========================================================================
# ATTACK 4: AGENT TIMEOUT OMITS `agent` FIELD
# ==========================================================================

def test_cf_r4_adversary_agent_timeout_omits_agent_field(proj, monkeypatch):
    """When an agent times out (asyncio.TimeoutError in _consult_turn),
    the returned dictionary at line 2488 is:
        {"agent_id": node_id, "turn": turn, "timed_out": True, "error": ..., **view}
    Notice that "agent" (the agent name, e.g. "advisor") is MISSING!
    Every other response from consult() includes "agent": agent_name:
      - normal: {"agent_id": node_id, "agent": agent_name, ...}
      - awaiting: {"agent_id": node_id, "agent": agent_name, ...}
      - lock timeout: {"agent": agent_name, "error": ...}
    Only agent timeout drops "agent"!
    """
    proj.consult("turn one")

    orig_wait_for = asyncio.wait_for

    async def mock_wait_for(fut, timeout=None):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)
    res = proj.consult("turn two timeout")

    assert res.get("timed_out") is True, res
    assert "agent" in res, (
        f"DEFECT: 'agent' field is missing from timed_out consult result: {res}"
    )


# ==========================================================================
# ATTACK 5: CF-R3 FALSE STALENESS NOTICE WHEN BASE REWRITTEN
# ==========================================================================

def test_cf_r3_adversary_base_amended_falsely_claims_agent_holds_own_work(proj):
    """When base is amended or rewritten (e.g. git commit --amend or rebase),
    the agent made NO commits and has NO uncommitted changes.
    However, holds_unmerged_commits() tests whether worktree HEAD is an ancestor of
    the new base commit. Because base was rewritten, HEAD is NOT an ancestor, and
    merging produces a tree difference.
    holds_unmerged_commits() therefore returns True!
    _refresh_conversation() then tells the agent:
      "[system] Your worktree is 1 commit behind base and was not updated,
       because it holds work of your own (uncommitted changes or commits not on base)."
    This statement is FALSE: the agent holds no work of its own.
    """
    first = proj.consult("turn one")

    # Base amends its last commit (rewritten history)
    (proj.root / "notes.txt").write_text("v1 amended\n")
    git_cmd(proj.root, "commit", "--amend", "-am", "base v1 amended")

    second = proj.consult("turn two")
    t2 = proj.last()

    # The prompt given to the agent
    prompt = t2["prompt"]
    assert "holds work of your own" not in prompt, (
        f"CF-R3 DEFECT: Agent was told its worktree was not updated because it "
        f"'holds work of your own', but the agent never made any changes!\n"
        f"Prompt was:\n{prompt}"
    )


# ==========================================================================
# ATTACK 6: BASE FORCE-MOVED BACKWARDS SUPPRESSES STALENESS NOTICE
# ==========================================================================

def test_cf_r3_adversary_base_moved_backwards_gives_no_stale_warning(proj):
    """When base is force-moved backwards (e.g. reset --hard HEAD~1):
    Turn 1 ran on commit C1.
    Now base is force-moved backwards to C0.
    In _refresh_conversation:
      behind = commits_on(worktree, base_sha, head)  # rev-list --count C1..C0 -> 0
      if not behind: return ""
    Because `behind` is 0, _refresh_conversation returns ""!
    The agent is given NO notice that base has moved backwards or that its worktree
    diverged from base!
    """
    # Create C0 and C1 on base
    proj.advance_base("v2\n")
    first = proj.consult("turn one")
    t1 = proj.last()
    assert t1["start"]["notes"] == "v2\n"

    # Now force-move base backwards to the initial commit
    git_cmd(proj.root, "reset", "--hard", "HEAD~1")

    # Turn two runs
    second = proj.consult("turn two")
    t2 = proj.last()

    # The agent's prompt has no notice line:
    assert t2["prompt"] != "turn two", (
        "CF-R3 DEFECT: Base was force-moved backwards, but no notice was given to the agent; "
        f"prompt was untouched: {t2['prompt']!r}"
    )


# ==========================================================================
# ATTACK 7: NESTED CONSULT CAUSES UNBOUNDED DEADLOCK
# ==========================================================================

def test_cf_r7_adversary_nested_consult_causes_deadlock(proj):
    """CF-R7: A nested consult from inside a turn deadlocks for 60+ seconds.

    Because Runner._conversation_turn uses `(data / f'consult-{name}.lock').open('a+')`
    and `fcntl.flock(handle.fileno(), LOCK_EX | LOCK_NB)`, each invocation creates a
    new file description.
    In Linux, fcntl.flock on different file descriptions within the same process
    is non-reentrant.
    If a turn attempts to consult the same agent (e.g. an agent tool or sub-step
    consulting the advisor), the nested call hangs in a 0.1s sleep loop until
    `wait = (timeout or spec.timeout) + 60` expires.
    """
    async def run_nested():
        async with proj.runner._conversation_turn("advisor", wait=0.2) as outer:
            assert outer is True
            # Nested call to the same conversation turn
            t0 = time.monotonic()
            async with proj.runner._conversation_turn("advisor", wait=0.2) as inner:
                elapsed = time.monotonic() - t0
                return inner, elapsed

    inner, elapsed = asyncio.run(run_nested())
    # The inner lock acquisition fails and returns False after waiting full wait duration
    assert inner is False, "Nested lock unexpectedly succeeded"
    assert elapsed >= 0.2, f"Expected nested lock to block for at least 0.2s, took {elapsed}s"
