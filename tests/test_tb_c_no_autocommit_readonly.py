"""TB-R5 — no automatic commit for agents that do not write
(context/specs/tooling-batch-2026-10.md, package C).

Contract: for a run whose launch spec has `writes=False`, the end-of-run
automatic WIP commit and the commit-fix turn are both skipped. Commits the
agent made itself are kept. Writing agents keep today's behaviour. A
non-writing run's worktree with no commits is dropped as today, its scratch
files with it.

Black box: a real `Runner` runs a scripted fake CLI in a real git project;
what is observed is the branch's commits, the worktree on disk, the tree node,
`result.json`, the event log and how many times the CLI was invoked.

Silences (not tested, not invented): conversational non-writing agents (their
worktree is retained across turns and the contract does not say whether a
consult turn ever commits), and a steered non-writing run.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h                                # noqa: E402
from multiagents.tree import TERMINAL                              # noqa: E402

GIT = shutil.which("git") or "git"
SID = "sess-tb-r5"
HOOK_MARKER = "TB_R5_HOOK_REFUSED"
WIP = "work in progress"
SETTLE_SECONDS = 20

_CLI = r'''#!{python}
import json, os, subprocess, sys
from pathlib import Path
PROBE = Path({probe!r})
PLANS = json.loads({plans!r})
count = PROBE / "invocations"
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
for step in PLANS[min(n, len(PLANS) - 1)]:
    op = step[0]
    if op == "emit":
        print(json.dumps(step[1])); sys.stdout.flush()
    elif op == "touch":
        Path(step[1]).write_text(step[2])
    elif op == "rm":
        Path(step[1]).unlink()
    elif op == "git":
        subprocess.run(["git", "-c", "user.name=agent", "-c", "user.email=a@example.invalid",
                        *step[1:]], check=True, capture_output=True)
sys.exit(0)
'''


def say(words: str) -> list:
    return ["emit", {"type": "text", "text": words, "session_id": SID}]


# First turns. Each leaves something untracked (and BLOCK, which the hook
# refuses while it exists).
SCRATCH = [["touch", "scratch.txt", "scratch\n"], say("looked around")]
BLOCKED = [["touch", "scratch.txt", "scratch\n"], ["touch", "BLOCK", "x"], say("done")]
FIX = [["rm", "BLOCK"], say("removed the offending file")]
OWN_COMMIT = [["touch", "mine.txt", "mine\n"], ["git", "add", "mine.txt"],
              ["git", "commit", "-q", "-m", "agent: my own commit"],
              ["touch", "leftover.txt", "left behind\n"], say("committed one file")]
NOTHING = [say("nothing to do")]


def sh(repo: Path, *args: str, check: bool = True):
    p = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {args}: {p.stderr}")
    return p


def make(tmp_path, monkeypatch, plans, *, writes, hook=False):
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "cli.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe),
                                  plans=json.dumps(plans)))
    script.chmod(0o755)
    provider = {
        "bin": str(script),
        "spawn": {"args": ["--fake", "--timeout", "{timeout}", "--prompt", "{prompt}"],
                  "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
    }
    project = tmp_path / "proj"
    runner = h.make_runner(project, monkeypatch,
                           agents={"worker": h.AgentSpec("worker", "p", "m", writes=writes)},
                           providers={"p": provider},
                           project={"limits": {"commit_fix_attempts": 2}})
    if hook:
        hooks = project / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        sh(project, "config", "core.hooksPath", str(hooks))
        pre = hooks / "pre-commit"
        pre.write_text(f"#!/bin/sh\nif [ -e BLOCK ]; then echo {HOOK_MARKER} >&2; exit 1; fi\n"
                       "exit 0\n")
        pre.chmod(0o755)
    return runner, project, probe


def invocations(probe: Path) -> int:
    f = probe / "invocations"
    return int(f.read_text()) if f.exists() else 0


def run_to_end(runner) -> str:
    async def go():
        started = await runner.start("worker", "go")
        agent_id = started["agent_id"]
        try:
            deadline = time.monotonic() + SETTLE_SECONDS
            while time.monotonic() < deadline:
                node = runner.tree.get(agent_id)
                if (node and node.status in TERMINAL
                        and (runner.paths.run_dir(agent_id) / "result.json").exists()):
                    await asyncio.sleep(0.3)        # let the final bookkeeping land
                    return agent_id
                await asyncio.sleep(0.05)
            raise AssertionError(f"run did not end in {SETTLE_SECONDS}s")
        finally:
            for run in list(runner.runs.values()):
                if run.task and not run.task.done():
                    try:
                        await runner.stop(run.node_id if hasattr(run, "node_id") else agent_id)
                    except Exception:
                        pass
    return asyncio.run(go())


def events(runner, agent_id: str, kind: str) -> list[dict]:
    path = runner.paths.events_file
    out = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("agent") == agent_id and e.get("kind") == kind:
            out.append(e)
    return out


def result_of(runner, agent_id: str) -> dict:
    return json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())


def wip_commits_anywhere(project: Path) -> list[str]:
    return sh(project, "log", "--all", "--format=%s", f"--grep={WIP}").stdout.split("\n")[:-1] \
        if sh(project, "log", "--all", "--format=%s", f"--grep={WIP}").stdout.strip() else []


# ---------------------------------------------------------------------------
# R5: the end-of-run commit
# ---------------------------------------------------------------------------

def test_tb_r5_a_non_writing_run_leaving_an_untracked_file_produces_no_commit(
        tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [SCRATCH], writes=False)

    agent_id = run_to_end(runner)

    assert result_of(runner, agent_id)["status"] == "done"
    assert wip_commits_anywhere(project) == [], "an automatic WIP commit was made"
    node = runner.tree.get(agent_id)
    if node.branch:
        base = sh(project, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        ahead = sh(project, "rev-list", "--count", f"{base}..{node.branch}").stdout.strip()
        assert ahead == "0", f"the branch gained {ahead} commit(s)"


def test_tb_r5_a_writing_run_leaving_an_untracked_file_still_commits_it(
        tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [SCRATCH], writes=True)

    agent_id = run_to_end(runner)

    branch = runner.tree.get(agent_id).branch
    assert branch, "a writing run keeps its branch"
    assert sh(project, "show", f"{branch}:scratch.txt").stdout == "scratch\n"
    assert any(WIP in s for s in wip_commits_anywhere(project)), "no WIP commit on the branch"


def test_tb_r5_a_non_writing_run_without_commits_is_dropped_with_its_scratch_files(
        tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [SCRATCH], writes=False)

    agent_id = run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert not node.branch, f"the empty branch was kept: {node.branch!r}"
    assert not node.worktree, f"the worktree is still recorded: {node.worktree!r}"
    assert not runner.paths.worktree(agent_id).exists(), "the worktree directory survives"
    branches = sh(project, "branch", "--list", "--format=%(refname:short)").stdout.split()
    assert [b for b in branches if agent_id.removeprefix("ag-") in b] == []


def test_tb_r5_a_non_writing_run_with_nothing_to_commit_is_dropped_as_before(
        tmp_path, monkeypatch):
    # Control: unchanged behaviour for a clean read-only run.
    runner, project, probe = make(tmp_path, monkeypatch, [NOTHING], writes=False)

    agent_id = run_to_end(runner)

    assert not runner.tree.get(agent_id).branch
    assert not runner.paths.worktree(agent_id).exists()


def test_tb_r5_a_commit_the_non_writing_agent_made_itself_is_kept_and_nothing_is_added(
        tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [OWN_COMMIT], writes=False)

    agent_id = run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert node.branch, "the agent's own commit must keep its branch"
    base = sh(project, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    subjects = sh(project, "log", "--format=%s", f"{base}..{node.branch}").stdout.split("\n")[:-1]
    assert subjects == ["agent: my own commit"], subjects
    assert sh(project, "show", f"{node.branch}:mine.txt").stdout == "mine\n"
    left = sh(project, "cat-file", "-e", f"{node.branch}:leftover.txt", check=False)
    assert left.returncode != 0, "the leftover file was swept into a commit"
    assert events(runner, agent_id, "unexpected_commits"), (
        "keeping a non-writing agent's own commit is still reported, as before")


def test_tb_r5_a_writing_run_that_committed_itself_still_gets_its_leftovers_committed(
        tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [OWN_COMMIT], writes=True)

    agent_id = run_to_end(runner)

    branch = runner.tree.get(agent_id).branch
    assert sh(project, "show", f"{branch}:leftover.txt").stdout == "left behind\n"


# ---------------------------------------------------------------------------
# R5: the commit-fix turn
# ---------------------------------------------------------------------------

def test_tb_r5_a_non_writing_run_gets_no_commit_fix_turn(tmp_path, monkeypatch):
    # The hook would refuse a commit; none is attempted, so there is nothing
    # to fix and the session is not resumed.
    runner, project, probe = make(tmp_path, monkeypatch, [BLOCKED, FIX],
                                  writes=False, hook=True)

    agent_id = run_to_end(runner)

    assert invocations(probe) == 1, "the agent was resumed to fix a commit nobody made"
    result = result_of(runner, agent_id)
    assert result["status"] == "done"
    assert HOOK_MARKER not in result["text"]
    assert "refused" not in result["text"]
    assert not events(runner, agent_id, "commit_failed")
    assert wip_commits_anywhere(project) == []


def test_tb_r5_a_writing_run_still_gets_its_commit_fix_turn(tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [BLOCKED, FIX],
                                  writes=True, hook=True)

    agent_id = run_to_end(runner)

    assert invocations(probe) == 2, "the fix turn did not run for a writing agent"
    branch = runner.tree.get(agent_id).branch
    assert sh(project, "show", f"{branch}:scratch.txt").stdout == "scratch\n"
    assert result_of(runner, agent_id)["status"] == "done"


def test_tb_r5_a_writing_run_whose_commit_stays_refused_still_reports_it(
        tmp_path, monkeypatch):
    # Control for the error path: today's report is unchanged for writers.
    runner, project, probe = make(tmp_path, monkeypatch, [BLOCKED], writes=True, hook=True)

    agent_id = run_to_end(runner)

    assert events(runner, agent_id, "commit_failed")
    assert HOOK_MARKER in result_of(runner, agent_id)["text"]
