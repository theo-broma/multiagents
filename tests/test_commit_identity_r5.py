"""CI-R5 — a hook failure is fed back to the agent, a bounded number of times.

Contract: context/specs/commit-identity.md, CI-R5 plus "Decisions, 2026-09-27
(orchestrator, after advisor ag-25c350 on CI-R5/R6)" and "Decisions,
2026-09-27 (orchestrator, after tester ag-c057bd on CI-R5)".

Black box. A real Runner runs a scripted fake agent CLI (a small Python script
standing in for the provider binary), and the project repository carries a
REAL git hook that refuses the end-of-run commit while a file named `BLOCK`
is in the worktree. What is observed:

- what the fake CLI was invoked with, each time (argv, cwd), which it records
  itself — the resume flag and the message are the provider contract
  (`spawn.resume`, `{prompt}`, `{timeout}`), not runner internals;
- git's own view of the agent's branch;
- the node as the tree reports it, `result.json`, and the project event log.

The agent "fixes" the hook by deleting `BLOCK` during a fix turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h                                # noqa: E402
from multiagents.tree import TERMINAL                              # noqa: E402

SID = "sess-ci-r5"
ORIGINAL = "CI_R5_ORIGINAL_ANSWER the work is complete"
HOOK_MARKER = "CI_R5_HOOK_REFUSED_THIS_COMMIT"
GIT = shutil.which("git") or "git"


# ==========================================================================
# The fake CLI
# ==========================================================================

# Plays plans[n] on its n-th invocation (the last plan repeats). Records its
# argv and cwd to <probe>/argv-<n>.json before doing anything. Steps:
#   ["emit", {...}]        print one NDJSON line
#   ["sleep", s]           sleep s seconds
#   ["gate", name]         block until <probe>/<name> exists (max 60 s)
#   ["touch", rel, text]   write a file relative to the cwd (the worktree)
#   ["rm", rel]            delete a file relative to the cwd
#   ["git", *args]         run git in the cwd
#   ["touch_probe", name]  write <probe>/<name>
#   ["exit", code]         exit with code
_CLI = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path

PROBE = Path({probe!r})
PLANS = json.loads({plans!r})
GIT = {git!r}
count_file = PROBE / "invocations"
n = int(count_file.read_text()) if count_file.exists() else 0
count_file.write_text(str(n + 1))
(PROBE / f"argv-{{n}}.json").write_text(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}))
plan = PLANS[min(n, len(PLANS) - 1)]
for step in plan:
    op = step[0]
    if op == "emit":
        print(json.dumps(step[1]))
        sys.stdout.flush()
    elif op == "sleep":
        time.sleep(step[1])
    elif op == "gate":
        deadline = time.time() + 60
        while not (PROBE / step[1]).exists() and time.time() < deadline:
            time.sleep(0.05)
    elif op == "touch":
        Path(step[1]).write_text(step[2])
    elif op == "rm":
        Path(step[1]).unlink()
    elif op == "git":
        subprocess.run([GIT, *step[1:]], capture_output=True)
    elif op == "touch_probe":
        (PROBE / step[1]).write_text("x")
    elif op == "exit":
        sys.exit(step[1])
sys.exit(0)
'''


def text(words: str, session: str | None = SID) -> list:
    payload = {"type": "text", "text": words}
    if session:
        payload["session_id"] = session
    return ["emit", payload]


def usage(input_tokens: int) -> list:
    return ["emit", {"type": "usage", "session_id": SID,
                     "usage": {"input_tokens": input_tokens}}]


# The first turn: does its work, leaves `BLOCK` behind (so the hook refuses),
# answers, exits cleanly.
FIRST = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
         text(ORIGINAL), ["exit", 0]]
FIX = [["rm", "BLOCK"], text("removed the offending file"), ["exit", 0]]
NO_FIX = [text("I could not work out what the hook wants"), ["exit", 0]]


def fake_provider(base: Path, plans, *, resume: bool = True, **extra) -> tuple[dict, Path]:
    probe = base / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    script = base / "cli.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe),
                                  plans=json.dumps(plans), git=GIT))
    script.chmod(0o755)
    spawn = {"args": ["--fake", "--timeout", "{timeout}", "--prompt", "{prompt}"]}
    if resume:
        spawn["resume"] = ["--resume", "{session_id}"]
    provider = {
        "bin": str(script),
        "spawn": spawn,
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [
                       {"match": {"type": "text"}, "as": "text",
                        "fields": {"text": "text"}},
                       {"match": {"type": "usage"}, "as": "step",
                        "fields": {"tokens": "usage"}},
                   ]},
        **extra,
    }
    return provider, probe


# ==========================================================================
# The project and its hook
# ==========================================================================

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def hooks_dir(project: Path) -> Path:
    d = project / ".git" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    # Pinned locally so a hooksPath in the machine's own git config cannot add
    # or hide hooks. Linked worktrees share the repository's config and hooks.
    git(project, "config", "core.hooksPath", str(d))
    return d


def install_hook(project: Path, *, name: str = "pre-commit",
                 output: str = HOOK_MARKER, executable: bool = True,
                 always_pass: bool = False) -> None:
    """A real hook: refuses while `BLOCK` exists in the worktree, printing
    `output` (verbatim) to stderr."""
    d = hooks_dir(project)
    out_file = project.parent / f"{name}-output.txt"
    out_file.write_text(output)
    body = "exit 0\n" if always_pass else (
        f'if [ -e BLOCK ]; then cat "{out_file}" >&2; exit 1; fi\nexit 0\n')
    hook = d / name
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755 if executable else 0o644)


def make(tmp_path, monkeypatch, provider: dict, *, limits: dict | None = None,
         hook: bool = True, **spec_kw):
    project = tmp_path / "proj"
    r = h.make_runner(project, monkeypatch,
                      agents={"worker": h.AgentSpec("worker", "p", "m", **spec_kw)},
                      providers={"p": provider},
                      project={"limits": dict(limits or {})})
    if hook:
        install_hook(project)
    else:
        hooks_dir(project)
    return r, project


# ==========================================================================
# Driving and observing
# ==========================================================================

def invocations(probe: Path) -> list[dict]:
    out = []
    n = 0
    while (probe / f"argv-{n}.json").exists():
        out.append(json.loads((probe / f"argv-{n}.json").read_text()))
        n += 1
    return out


def prompt_of(call: dict) -> str:
    argv = call["argv"]
    return argv[argv.index("--prompt") + 1]


def resumed_with(call: dict) -> str | None:
    argv = call["argv"]
    return argv[argv.index("--resume") + 1] if "--resume" in argv else None


def timeout_of(call: dict) -> str:
    argv = call["argv"]
    return argv[argv.index("--timeout") + 1]


def events(r, agent_id: str, kind: str | None = None) -> list[dict]:
    path = r.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("agent") == agent_id and (kind is None or e.get("kind") == kind):
            out.append(e)
    return out


def result_of(r, agent_id: str) -> dict:
    return json.loads((r.paths.run_dir(agent_id) / "result.json").read_text())


def on_branch(project: Path, branch: str, rel: str) -> str | None:
    shown = git(project, "show", f"{branch}:{rel}", check=False)
    return shown.stdout if shown.returncode == 0 else None


def ended(r, agent_id: str) -> bool:
    node = r.tree.get(agent_id)
    return (node is not None and node.status in TERMINAL
            and (r.paths.run_dir(agent_id) / "result.json").exists())


async def until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def settle(r, agent_id: str, timeout: float = 60) -> None:
    """Until the run is over: terminal, with its result written, and still so
    a moment later (a fix loop in flight must not be mistaken for the end)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ended(r, agent_id):
            await asyncio.sleep(0.75)
            if ended(r, agent_id):
                return
        await asyncio.sleep(0.05)
    raise AssertionError(f"run did not end within {timeout}s: "
                         f"{r.tree.get(agent_id).status}")


async def stop_all(r) -> None:
    for agent_id in list(r.runs):
        run = r.runs[agent_id]
        if run.task and not run.task.done():
            try:
                await r.stop(agent_id)
            except Exception:
                pass


def run_to_end(r, timeout: float = 60) -> str:
    async def go():
        started = await r.start("worker", "go")
        try:
            await settle(r, started["agent_id"], timeout)
        finally:
            await stop_all(r)
        return started["agent_id"]
    return asyncio.run(go())


def attempt_numbers(evts: list[dict]) -> list:
    """The attempt number each `commit_fix_attempt` event carries, in the field
    `attempt`, counted from 1 (decision after tester ag-c057bd)."""
    return [e.get("attempt") for e in evts]


def appended(result_text: str) -> str:
    return result_text.replace(ORIGINAL, "")


# ==========================================================================
# Controls — the fixture really does what the tests below assume
# ==========================================================================

def test_control_the_hook_refuses_while_block_exists_and_passes_without(tmp_path, monkeypatch):
    r, project = make(tmp_path, monkeypatch, fake_provider(tmp_path, [NO_FIX])[0])
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    (project / "BLOCK").write_text("x")
    git(project, "add", "-A")
    refused = subprocess.run([GIT, "-C", str(project), "commit", "-m", "x"],
                             capture_output=True, text=True, env=env)
    assert refused.returncode != 0 and HOOK_MARKER in refused.stderr, refused
    (project / "BLOCK").unlink()
    git(project, "add", "-A")
    (project / "ok.txt").write_text("y")
    git(project, "add", "-A")
    ok = subprocess.run([GIT, "-C", str(project), "commit", "-m", "x"],
                        capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr


def test_control_a_run_whose_commit_the_hook_refuses_reports_it_today(tmp_path, monkeypatch):
    """CI-R2's path, with CI-R5 disabled: the fixture produces a hook-refused
    end-of-run commit, recorded as `commit_failed`."""
    prov, probe = fake_provider(tmp_path, [FIRST])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 0})
    agent = run_to_end(r)
    assert len(invocations(probe)) == 1
    assert events(r, agent, "commit_failed"), "fixture: the commit must have failed"
    assert HOOK_MARKER in result_of(r, agent)["text"]


# ==========================================================================
# CI-R5 — Verified by
# ==========================================================================

def test_ci_r5_hook_satisfied_on_the_first_fix_turn_commits_with_one_attempt(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [FIRST, FIX])
    r, project = make(tmp_path, monkeypatch, prov)

    agent = run_to_end(r)

    calls = invocations(probe)
    assert len(calls) == 2, f"expected one fix turn after the refused commit, got {len(calls)} runs"
    node = r.tree.get(agent)
    # Resume: the same session, in the same worktree.
    assert resumed_with(calls[1]) == SID, calls[1]["argv"]
    assert Path(calls[1]["cwd"]).resolve() == Path(node.worktree).resolve()
    # Retry succeeded: the work is on the branch, nothing left behind.
    assert on_branch(project, node.branch, "work.txt") == "agent output\n"
    assert on_branch(project, node.branch, "BLOCK") is None
    assert git(Path(node.worktree), "status", "--porcelain").stdout.strip() == ""
    # Recorded: one attempt, and no failure.
    fixes = events(r, agent, "commit_fix_attempt")
    assert len(fixes) == 1, fixes
    assert attempt_numbers(fixes) == [1], f"the attempt number must be recorded: {fixes}"
    assert events(r, agent, "commit_failed") == []
    # Status and result text.
    assert node.status == "done", (node.status, node.reason)
    result = result_of(r, agent)
    assert result["status"] == "done"
    assert ORIGINAL in result["text"], "the agent's original answer must be kept"
    extra = appended(result["text"])
    assert re.search(r"\b1\b", extra) and "commit" in extra.lower(), (
        f"the result text must say how many fix attempts were made: {result['text']!r}")
    assert HOOK_MARKER not in extra and "commit failed" not in extra.lower(), (
        f"a commit that finally succeeded must not be reported as failed: {result['text']!r}")


@pytest.mark.parametrize("configured, expected", [(None, 2), (1, 1), (3, 3)],
                         ids=["default-2", "one", "three"])
def test_ci_r5_never_satisfied_resumes_exactly_n_times_then_commit_failed(
        tmp_path, monkeypatch, configured, expected):
    prov, probe = fake_provider(tmp_path, [FIRST, NO_FIX])
    limits = {} if configured is None else {"commit_fix_attempts": configured}
    r, project = make(tmp_path, monkeypatch, prov, limits=limits)

    agent = run_to_end(r)

    calls = invocations(probe)
    assert len(calls) == 1 + expected, (
        f"commit_fix_attempts={configured}: expected {expected} resumes, "
        f"got {len(calls) - 1}")
    assert all(resumed_with(c) == SID for c in calls[1:])
    fixes = events(r, agent, "commit_fix_attempt")
    assert len(fixes) == expected, fixes
    assert attempt_numbers(fixes) == list(range(1, expected + 1)), \
        f"attempts numbered 1..{expected} in the field `attempt`: {fixes}"
    assert events(r, agent, "commit_failed"), "the final outcome must be commit_failed"
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") is None
    result = result_of(r, agent)
    assert ORIGINAL in result["text"]
    assert HOOK_MARKER in result["text"], "the failure is reported as in CI-R2"
    assert re.search(rf"\b{expected}\b", appended(result["text"])), (
        f"the result text must say {expected} fix attempts were made: {result['text']!r}")
    # CI-R2 decision: a failed commit does not by itself change the status.
    assert node.status == "done", (node.status, node.reason)


def test_ci_r5_zero_attempts_disables_the_resume(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [FIRST, FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 0})

    agent = run_to_end(r)

    assert len(invocations(probe)) == 1, "commit_fix_attempts: 0 must never resume"
    assert events(r, agent, "commit_fix_attempt") == []
    assert events(r, agent, "commit_failed")
    assert HOOK_MARKER in result_of(r, agent)["text"]


def _lock_index(worktree: Path) -> None:
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "index.lock").write_text("")


@pytest.mark.parametrize("hooks", ["none", "sample-only", "not-executable", "passing"])
def test_ci_r5_a_non_hook_failure_is_never_fed_back(tmp_path, monkeypatch, hooks):
    """The commit fails on a held index lock. Whatever hooks the repository
    has that did not cause it — none, git's `.sample` files, a failing hook
    git ignores because it is not executable, a hook that passes — no resume."""
    # Staged by the agent, so the commit itself (not `git add`) is what the
    # lock refuses.
    first = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
             ["git", "add", "-A"],
             ["touch_probe", "worked"], ["gate", "locked"], text(ORIGINAL), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [first, FIX])
    r, project = make(tmp_path, monkeypatch, prov, hook=False)
    d = hooks_dir(project)
    if hooks == "sample-only":
        (d / "pre-commit.sample").write_text(f"#!/bin/sh\necho {HOOK_MARKER} >&2\nexit 1\n")
        (d / "pre-commit.sample").chmod(0o755)
    elif hooks == "not-executable":
        install_hook(project, executable=False)
    elif hooks == "passing":
        install_hook(project, always_pass=True)

    async def go():
        started = await r.start("worker", "go")
        agent = started["agent_id"]
        try:
            assert await until(lambda: (probe / "worked").exists(), 30)
            _lock_index(Path(r.tree.get(agent).worktree))
            (probe / "locked").write_text("go")
            await settle(r, agent)
        finally:
            await stop_all(r)
        return agent
    agent = asyncio.run(go())

    assert len(invocations(probe)) == 1, f"a non-hook failure ({hooks}) must never resume"
    assert events(r, agent, "commit_fix_attempt") == []
    assert events(r, agent, "commit_failed"), "it goes straight to CI-R2"
    assert "index.lock" in result_of(r, agent)["text"]


def test_ci_r5_the_resume_message_carries_the_hook_output_tail(tmp_path, monkeypatch):
    lines = [f"hook-line-{i:05d} the formatter disagrees with src/mod_{i}.py" for i in range(300)]
    output = "CI_R5_HOOK_HEAD\n" + "\n".join(lines) + "\nCI_R5_HOOK_TAIL\n"
    assert len(output) > 12000
    prov, probe = fake_provider(tmp_path, [FIRST, FIX])
    r, project = make(tmp_path, monkeypatch, prov)
    install_hook(project, output=output)

    agent = run_to_end(r)

    calls = invocations(probe)
    assert len(calls) >= 2, "no fix turn was started"
    message = prompt_of(calls[1])
    assert "hook" in message.lower(), f"the message must say a hook refused the commit: {message[:500]!r}"
    tail = output.rstrip("\n")[-4000:]
    assert tail in message, (
        "the resume message must carry at least the last 4000 characters of the "
        f"hook's output verbatim (not the CI-R4 500-char copy); got {len(message)} chars")
    # The event's copy is truncated.
    fixes = events(r, agent, "commit_fix_attempt")
    assert fixes and all(len(json.dumps(e)) < len(output) // 2 for e in fixes), \
        "commit_fix_attempt must carry a truncated detail, not the whole hook output"


# ==========================================================================
# CI-R5 — the rest of the behaviour
# ==========================================================================

def test_ci_r5_a_commit_msg_hook_is_a_hook_failure_too(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [FIRST, FIX])
    r, project = make(tmp_path, monkeypatch, prov, hook=False)
    install_hook(project, name="commit-msg")

    agent = run_to_end(r)

    assert len(invocations(probe)) == 2
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") == "agent output\n"
    assert len(events(r, agent, "commit_fix_attempt")) == 1
    assert events(r, agent, "commit_failed") == []


def test_ci_r5_an_agent_that_commits_during_the_fix_turn_is_a_success(tmp_path, monkeypatch):
    """"If the agent committed during the turn and nothing is left, that
    counts as success.\""""
    fix_and_commit = [["rm", "BLOCK"], ["git", "add", "-A"],
                      ["git", "-c", "user.name=agent", "-c", "user.email=a@e.invalid",
                       "commit", "-m", "satisfy the hook"],
                      text("committed it myself"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, fix_and_commit, NO_FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 2})

    agent = run_to_end(r)

    assert len(invocations(probe)) == 2, "success after one fix turn; no second resume"
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") == "agent output\n"
    assert events(r, agent, "commit_failed") == []
    assert len(events(r, agent, "commit_fix_attempt")) == 1
    assert node.status == "done"


def test_ci_r5_a_provider_without_resume_is_never_resumed(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [FIRST, FIX], resume=False)
    r, project = make(tmp_path, monkeypatch, prov)

    agent = run_to_end(r)

    assert len(invocations(probe)) == 1, "a provider with no resume cannot be fed back"
    assert events(r, agent, "commit_failed")
    assert HOOK_MARKER in result_of(r, agent)["text"]
    assert ORIGINAL in result_of(r, agent)["text"]


def test_ci_r5_a_run_with_no_session_id_is_never_resumed(tmp_path, monkeypatch):
    first = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
             text(ORIGINAL, session=None), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [first, FIX])
    r, project = make(tmp_path, monkeypatch, prov)

    agent = run_to_end(r)

    calls = invocations(probe)
    assert all(resumed_with(c) for c in calls[1:]), \
        "a fix turn must resume the session, never start a fresh one"
    assert len(calls) == 1, "no session to resume: reported as CI-R2 instead"
    assert events(r, agent, "commit_failed")


def test_ci_r5_a_run_that_ended_limited_is_not_resumed(tmp_path, monkeypatch):
    """"or the session cannot be resumed (... quota)": the first turn itself
    hit the provider's limit."""
    marker = "You've hit your usage limit for CI-R5"
    first = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
             text(ORIGINAL), text(marker), ["exit", 1]]
    prov, probe = fake_provider(tmp_path, [first, FIX], transcript={"limit_markers": [
        {"match": marker, "resets": True, "detail": "usage limit"}]})
    r, project = make(tmp_path, monkeypatch, prov)

    agent = run_to_end(r)

    assert r.tree.get(agent).status == "limited", "fixture: the run must end limited"
    assert len(invocations(probe)) == 1
    assert events(r, agent, "commit_fix_attempt") == []
    assert HOOK_MARKER in result_of(r, agent)["text"]


# ==========================================================================
# Decisions, 2026-09-27 (CI-R5/R6)
# ==========================================================================

def test_ci_r5_quota_during_a_fix_turn_ends_limited_not_failed(tmp_path, monkeypatch):
    marker = "You've hit your usage limit for CI-R5"
    quota_fix = [text("looking at the hook"), text(marker), ["exit", 1]]
    prov, probe = fake_provider(tmp_path, [FIRST, quota_fix, FIX],
                                transcript={"limit_markers": [
                                    {"match": marker, "resets": True, "detail": "usage limit"}]})
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 3})

    agent = run_to_end(r)

    assert len(invocations(probe)) == 2, \
        "a quota cut ends the loop: no further resume on a limited provider"
    node = r.tree.get(agent)
    assert node.status == "limited", (node.status, node.reason)
    result = result_of(r, agent)
    assert result["status"] == "limited"
    assert ORIGINAL in result["text"]
    assert HOOK_MARKER in result["text"], "the commit failure is still appended"
    assert events(r, agent, "commit_failed")


def test_ci_r5_one_result_and_no_terminal_status_during_the_loop(tmp_path, monkeypatch):
    held_fix = [["touch_probe", "in-fix"], ["gate", "release"], ["rm", "BLOCK"],
                text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, held_fix])
    r, project = make(tmp_path, monkeypatch, prov)
    seen: list[str] = []

    async def go():
        started = await r.start("worker", "go")
        agent = started["agent_id"]
        stop = asyncio.Event()

        async def watch():
            while not stop.is_set():
                node = r.tree.get(agent)
                if node is not None and (not seen or seen[-1] != node.status):
                    seen.append(node.status)
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch())
        try:
            assert await until(lambda: (probe / "in-fix").exists(), 30), "no fix turn started"
            await asyncio.sleep(0.3)
            node = r.tree.get(agent)
            assert node.status == "running", \
                f"the node must stay running during a fix turn, was {node.status}"
            assert not (r.paths.run_dir(agent) / "result.json").exists(), \
                "result.json must not be written before the fix loop ends"
            (probe / "release").write_text("go")
            await settle(r, agent)
        finally:
            stop.set()
            await watcher
            await stop_all(r)
        return agent
    agent = asyncio.run(go())

    first_terminal = next((i for i, s in enumerate(seen) if s in TERMINAL), None)
    assert first_terminal is not None, seen
    assert all(s in TERMINAL for s in seen[first_terminal:]), \
        f"done-then-running flicker: {seen}"
    assert result_of(r, agent)["status"] == "done"


def test_ci_r5_a_fix_turn_is_not_bound_by_the_spent_original_wall_clock(tmp_path, monkeypatch):
    """The agent's own timeout is 3 s and the first turn spends most of it.
    The fix turn takes 4 s, well inside the default commit_fix_timeout."""
    first = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
             ["sleep", 2], text(ORIGINAL), ["exit", 0]]
    slow_fix = [["sleep", 4], ["rm", "BLOCK"], text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [first, slow_fix])
    r, project = make(tmp_path, monkeypatch, prov, timeout=3, silence_timeout=60)

    agent = run_to_end(r)

    calls = invocations(probe)
    assert len(calls) == 2
    assert timeout_of(calls[1]) == "300", \
        f"a fix turn's wall clock is commit_fix_timeout (default 300 s): {calls[1]['argv']}"
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") == "agent output\n", \
        "the fix turn was cut short by the original run's wall clock"
    assert events(r, agent, "commit_failed") == []


def test_ci_r5_commit_fix_timeout_bounds_each_fix_turn(tmp_path, monkeypatch):
    hung_fix = [["touch_probe", "in-fix"], ["gate", "never"], ["rm", "BLOCK"],
                text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, hung_fix])
    r, project = make(tmp_path, monkeypatch, prov,
                      limits={"commit_fix_attempts": 1, "commit_fix_timeout": 2},
                      silence_timeout=120)

    began = time.monotonic()
    agent = run_to_end(r, timeout=45)
    took = time.monotonic() - began

    calls = invocations(probe)
    assert len(calls) == 2, "fixture: one fix turn"
    assert timeout_of(calls[1]) == "2", calls[1]["argv"]
    assert took < 30, f"a fix turn bounded at 2 s held the run for {took:.0f}s"
    assert events(r, agent, "commit_failed"), \
        "the hook was never satisfied, so the failure is reported"
    # Decision after tester ag-c057bd: the cut-off turn is one attempt, and the
    # run keeps the status it ended with, not `timeout`.
    assert attempt_numbers(events(r, agent, "commit_fix_attempt")) == [1]
    node = r.tree.get(agent)
    assert node.status == "done", (node.status, node.reason)
    result = result_of(r, agent)
    assert result["status"] == "done", result["status"]
    assert ORIGINAL in result["text"]
    assert HOOK_MARKER in result["text"]
    assert on_branch(project, node.branch, "work.txt") is None


def test_ci_r5_a_fix_turn_cut_off_by_commit_fix_timeout_counts_and_the_loop_goes_on(
        tmp_path, monkeypatch):
    """Decision after tester ag-c057bd: the timed-out fix turn is attempt 1,
    and the loop continues to attempt 2, which fixes the hook."""
    hung_fix = [["touch_probe", "in-fix"], ["gate", "never"], ["rm", "BLOCK"],
                text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, hung_fix, FIX])
    r, project = make(tmp_path, monkeypatch, prov,
                      limits={"commit_fix_attempts": 2, "commit_fix_timeout": 2},
                      silence_timeout=120)

    agent = run_to_end(r, timeout=45)

    calls = invocations(probe)
    assert len(calls) == 3, f"a timed-out fix turn, then one more: {len(calls)} runs"
    assert all(resumed_with(c) == SID for c in calls[1:])
    assert attempt_numbers(events(r, agent, "commit_fix_attempt")) == [1, 2]
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") == "agent output\n"
    assert events(r, agent, "commit_failed") == []
    assert node.status == "done", (node.status, node.reason)
    assert result_of(r, agent)["status"] == "done"


def test_ci_r5_stop_during_a_fix_turn_ends_the_loop(tmp_path, monkeypatch):
    held_fix = [["touch_probe", "in-fix"], ["gate", "never"], text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, held_fix])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 3})

    async def go():
        started = await r.start("worker", "go")
        agent = started["agent_id"]
        try:
            assert await until(lambda: (probe / "in-fix").exists(), 30), "no fix turn started"
            await r.stop(agent)
            await asyncio.sleep(3)
        finally:
            await stop_all(r)
        return agent
    agent = asyncio.run(go())

    assert len(invocations(probe)) == 2, "a stop ends the loop: no further fix turn"
    assert len(events(r, agent, "commit_fix_attempt")) == 1, \
        "the stop does not count as another attempt"
    assert r.tree.get(agent).status == "cancelled", r.tree.get(agent).status


def test_ci_r5_steer_during_a_fix_turn_takes_over(tmp_path, monkeypatch):
    held_fix = [["touch_probe", "in-fix"], ["gate", "never"], text("fixed"), ["exit", 0]]
    steered = [["rm", "BLOCK"], text("did what the orchestrator asked"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, held_fix, steered, NO_FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 3})

    async def go():
        started = await r.start("worker", "go")
        agent = started["agent_id"]
        try:
            assert await until(lambda: (probe / "in-fix").exists(), 30), "no fix turn started"
            res = await r.steer(agent, "CI_R5_ORCHESTRATOR_STEER change of plan")
            assert res.get("steered") is True, res
            await settle(r, agent)
        finally:
            await stop_all(r)
        return agent
    agent = asyncio.run(go())

    calls = invocations(probe)
    assert len(calls) == 3, f"fix turn, then the steer, then nothing: {len(calls)} runs"
    assert prompt_of(calls[2]) == "CI_R5_ORCHESTRATOR_STEER change of plan"
    assert resumed_with(calls[2]) == SID
    assert len(events(r, agent, "commit_fix_attempt")) == 1, \
        "the steer does not count as another attempt"
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "work.txt") == "agent output\n"
    assert node.status == "done", (node.status, node.reason)


def test_ci_r5_fix_turns_count_toward_the_runs_usage(tmp_path, monkeypatch):
    """Cumulative provider: a resumed session reports its running total, so
    after a fix turn the node must show the fix turn's figure, not only the
    first turn's."""
    first = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
             usage(100), text(ORIGINAL), ["exit", 0]]
    fix = [usage(1100), ["rm", "BLOCK"], text("fixed"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [first, fix])
    r, project = make(tmp_path, monkeypatch, prov)

    agent = run_to_end(r)

    assert len(invocations(probe)) == 2
    total = r.tree.rollup_usage().get("total", 0)
    assert total >= 1100, f"the fix turn's usage was not counted: {total}"


def test_ci_r5_a_steered_runs_own_commit_gets_a_fresh_loop(tmp_path, monkeypatch):
    """Decision after tester ag-c057bd: a steered run is a new end of run. The
    original run uses up both attempts (the second fixes the hook). The steered
    turn leaves `BLOCK` again; its commit gets its own loop, counted from 1 —
    with a shared count, the budget of 2 would already be spent."""
    steered = [["touch", "more.txt", "steered output\n"], ["touch", "BLOCK", "x"],
               text("CI_R5_STEERED_ANSWER"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, NO_FIX, FIX, steered, FIX, NO_FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 2})

    async def go():
        started = await r.start("worker", "go")
        agent = started["agent_id"]
        try:
            await settle(r, agent)
            assert len(invocations(probe)) == 3, "CI-R5: the original run's loop takes two attempts"
            node = r.tree.get(agent)
            assert on_branch(project, node.branch, "work.txt") == "agent output\n", \
                "CI-R5: the original run's commit succeeds on its second attempt"
            res = await r.steer(agent, "CI_R5_ORCHESTRATOR_STEER one more thing")
            assert res.get("steered") is True, res
            await until(lambda: len(invocations(probe)) >= 5, 30)
            await settle(r, agent)
        finally:
            await stop_all(r)
        return agent
    agent = asyncio.run(go())

    calls = invocations(probe)
    assert prompt_of(calls[3]) == "CI_R5_ORCHESTRATOR_STEER one more thing"
    assert len(calls) == 5, (
        f"the steered run's refused commit must get its own fix turn: {len(calls)} runs")
    assert resumed_with(calls[4]) == SID
    assert attempt_numbers(events(r, agent, "commit_fix_attempt")) == [1, 2, 1], \
        "the steered run's loop counts its attempts from 1"
    node = r.tree.get(agent)
    assert on_branch(project, node.branch, "more.txt") == "steered output\n"
    assert on_branch(project, node.branch, "BLOCK") is None
    assert events(r, agent, "commit_failed") == []
    assert node.status == "done", (node.status, node.reason)
