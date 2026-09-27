"""CI-R5 adversary — the fix loop's lifecycle and its newest, untested edges.

Contract: context/specs/commit-identity.md, CI-R5 and the "Decisions,
2026-09-27 (orchestrator, after implementer ag-b7c2ff on CI-R5)" section:

    Any quota or auth cut propagates: a fix turn that ends `quota` (no reset
    time known) or `unauthenticated` gives the run that same status ... It
    does not keep the original `done`. ... The commit failure is still
    appended.

The spec's own "Verified by" list for CI-R5 does not cover the no-reset
`quota`/`unauthenticated` cut of a *fix* turn, and the decision above flags it
"untested so far". These tests supply that.

Black box, exactly as `tests/test_commit_identity_r5.py`: a real Runner runs a
scripted fake CLI, the project carries a real `pre-commit` hook that refuses
while `BLOCK` exists, and what is observed is `result.json`, the tree node, the
event log, and how many times the CLI was invoked. The CLI here adds one step
the shared harness lacks — writing to its own stderr — because a no-reset
quota / an auth failure is recognised from the run's stderr channel, not from
what the agent said.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h                                # noqa: E402

SID = "sess-ci-r5-adv"
ORIGINAL = "CI_R5_ADV_ORIGINAL the work is complete"
HOOK_MARKER = "CI_R5_ADV_HOOK_REFUSED"
GIT = shutil.which("git") or "git"


# --------------------------------------------------------------------------
# A fake CLI with a stderr step (the shared r5 CLI has none).
# --------------------------------------------------------------------------

_CLI = r'''#!{python}
import json, os, sys, time
from pathlib import Path
PROBE = Path({probe!r})
PLANS = json.loads({plans!r})
count = PROBE / "invocations"
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
(PROBE / f"argv-{{n}}.json").write_text(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}))
for step in PLANS[min(n, len(PLANS) - 1)]:
    op = step[0]
    if op == "emit":
        print(json.dumps(step[1])); sys.stdout.flush()
    elif op == "stderr":
        sys.stderr.write(step[1]); sys.stderr.flush()
    elif op == "touch":
        Path(step[1]).write_text(step[2])
    elif op == "rm":
        Path(step[1]).unlink()
    elif op == "sleep":
        time.sleep(step[1])
    elif op == "exit":
        sys.exit(step[1])
sys.exit(0)
'''


def text(words: str, session: str | None = SID) -> list:
    payload = {"type": "text", "text": words}
    if session:
        payload["session_id"] = session
    return ["emit", payload]


FIRST = [["touch", "work.txt", "agent output\n"], ["touch", "BLOCK", "x"],
         text(ORIGINAL), ["exit", 0]]
FIX = [["rm", "BLOCK"], text("removed the offending file"), ["exit", 0]]
NO_FIX = [text("I could not satisfy the hook"), ["exit", 0]]


def fake_provider(base: Path, plans, *, resume: bool = True, **extra):
    probe = base / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    script = base / "cli.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe),
                                  plans=json.dumps(plans)))
    script.chmod(0o755)
    spawn = {"args": ["--fake", "--timeout", "{timeout}", "--prompt", "{prompt}"]}
    if resume:
        spawn["resume"] = ["--resume", "{session_id}"]
    provider = {
        "bin": str(script),
        "spawn": spawn,
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                   "rules": [{"match": {"type": "text"}, "as": "text",
                              "fields": {"text": "text"}}]},
        **extra,
    }
    return provider, probe


def git(repo: Path, *args: str, check: bool = True):
    import subprocess
    p = subprocess.run([GIT, "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {args}: {p.stderr}")
    return p


def install_hook(project: Path) -> None:
    d = project / ".git" / "hooks"
    d.mkdir(parents=True, exist_ok=True)
    git(project, "config", "core.hooksPath", str(d))
    hook = d / "pre-commit"
    hook.write_text(f'#!/bin/sh\nif [ -e BLOCK ]; then echo {HOOK_MARKER} >&2; exit 1; fi\nexit 0\n')
    hook.chmod(0o755)


def make(tmp_path, monkeypatch, provider, *, limits=None, **spec_kw):
    project = tmp_path / "proj"
    r = h.make_runner(project, monkeypatch,
                      agents={"worker": h.AgentSpec("worker", "p", "m", **spec_kw)},
                      providers={"p": provider},
                      project={"limits": dict(limits or {})})
    install_hook(project)
    return r, project


def invocations(probe: Path) -> int:
    f = probe / "invocations"
    return int(f.read_text()) if f.exists() else 0


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


def on_branch(project: Path, branch: str, rel: str):
    shown = git(project, "show", f"{branch}:{rel}", check=False)
    return shown.stdout if shown.returncode == 0 else None


from multiagents.tree import AWAITING, TERMINAL                     # noqa: E402


# `statuses` is the set a run may settle in. Every test requires a terminal
# status, except the one where the contract parks the run `awaiting_user`.
def ended(r, agent_id: str, statuses=TERMINAL) -> bool:
    node = r.tree.get(agent_id)
    return (node is not None and node.status in statuses
            and (r.paths.run_dir(agent_id) / "result.json").exists())


async def settle(r, agent_id: str, timeout: float = 60,
                 statuses=TERMINAL) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ended(r, agent_id, statuses):
            await asyncio.sleep(0.75)
            if ended(r, agent_id, statuses):
                return
        await asyncio.sleep(0.05)
    node = r.tree.get(agent_id)
    raise AssertionError(f"run did not end in {timeout}s: {node.status if node else '?'}")


async def stop_all(r) -> None:
    for agent_id in list(r.runs):
        run = r.runs[agent_id]
        if run.task and not run.task.done():
            try:
                await r.stop(agent_id)
            except Exception:
                pass


def run_to_end(r, timeout: float = 60, statuses=TERMINAL) -> str:
    async def go():
        started = await r.start("worker", "go")
        try:
            await settle(r, started["agent_id"], timeout, statuses)
        finally:
            await stop_all(r)
        return started["agent_id"]
    return asyncio.run(go())


# ==========================================================================
# Controls
# ==========================================================================

def test_control_first_turn_leaves_block_and_the_hook_refuses(tmp_path, monkeypatch):
    prov, probe = fake_provider(tmp_path, [FIRST, FIX])
    r, project = make(tmp_path, monkeypatch, prov)
    agent = run_to_end(r)
    # Sanity: the fix loop ran and fixed it — same shape the r5 suite asserts.
    assert invocations(probe) == 2
    assert result_of(r, agent)["status"] == "done"


def test_control_a_stderr_auth_marker_on_the_first_turn_is_unauthenticated(tmp_path, monkeypatch):
    """The stderr step really drives classification: a first turn that writes
    an auth marker to stderr and exits non-zero ends `unauthenticated`."""
    first = [["stderr", "Error: invalid api key\n"], ["exit", 1]]
    prov, probe = fake_provider(tmp_path, [first])
    r, project = make(tmp_path, monkeypatch, prov)
    agent = run_to_end(r)
    assert result_of(r, agent)["status"] == "unauthenticated", result_of(r, agent)


# ==========================================================================
# A fix turn that ends `unauthenticated` gives the run that status
# ==========================================================================

def test_ci_r5_unauthenticated_fix_turn_makes_the_run_unauthenticated(tmp_path, monkeypatch):
    unauth_fix = [text("looking at the hook"),
                  ["stderr", "Error: invalid api key — please log in\n"],
                  ["exit", 1]]
    prov, probe = fake_provider(tmp_path, [FIRST, unauth_fix, FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 3})

    agent = run_to_end(r)

    # The auth cut ends the loop — a session that cannot authenticate cannot be
    # resumed again.
    assert invocations(probe) == 2, "no further resume after an auth cut"
    node = r.tree.get(agent)
    result = result_of(r, agent)
    assert result["status"] == "unauthenticated", (
        f"a fix turn that ended unauthenticated must give the run that status, "
        f"not keep the original done: {result['status']}")
    assert result["status"] != "done"
    assert ORIGINAL in result["text"], "the agent's original answer is kept"
    assert HOOK_MARKER in result["text"], "the commit failure is still appended"
    assert events(r, agent, "commit_failed"), "the refused commit is reported"
    assert events(r, agent, "unauthenticated"), "an unauthenticated run says so"


# ==========================================================================
# A fix turn that ends `quota` (no reset) gives the run that status
# ==========================================================================

def test_ci_r5_quota_no_reset_fix_turn_makes_the_run_quota(tmp_path, monkeypatch):
    quota_fix = [text("looking at the hook"),
                 ["stderr", "Error: 429 quota exceeded (resource_exhausted)\n"],
                 ["exit", 1]]
    prov, probe = fake_provider(tmp_path, [FIRST, quota_fix, FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 3})

    agent = run_to_end(r)

    assert invocations(probe) == 2, "no further resume after a quota cut"
    result = result_of(r, agent)
    assert result["status"] == "quota", (
        f"a fix turn that ended quota (no reset) must give the run that "
        f"status, not keep the original done: {result['status']}")
    assert result["status"] != "done"
    assert ORIGINAL in result["text"]
    assert HOOK_MARKER in result["text"], "the commit failure is still appended"
    assert events(r, agent, "commit_failed")


# ==========================================================================
# A fix turn is an ordinary turn: a NEED_DECISION it raises is a question to
# the human, and must not be silently swallowed.
# ==========================================================================

def test_ci_r5_a_need_decision_from_a_fix_turn_is_not_silently_lost(tmp_path, monkeypatch):
    """CI-R5 decision: "The fix turns are ordinary turns." An ordinary turn
    that emits `NEED_DECISION(...)` parks the run and surfaces the question so
    a human can answer it — that mechanism is the whole reason the marker
    exists. A fix agent that cannot work out what the hook wants and asks is in
    exactly that situation.

    Here the fix turn asks instead of fixing. The question must reach the tree
    (an open question, or at least a `question` event) rather than being
    dropped while the loop quietly spends the remaining attempts retrying a
    turn that was waiting on an answer that was never requested.
    """
    ask = [text("NEED_DECISION(hook): the hook wants a Signed-off-by line — which identity?"),
           text("DEFAULT: the agent fallback identity"), ["exit", 0]]
    prov, probe = fake_provider(tmp_path, [FIRST, ask, ask, ask])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": 2})

    # The decision parks the run `awaiting_user`, which is not terminal, so
    # this test alone settles on it (result.json present, as ever).
    agent = run_to_end(r, statuses=TERMINAL | {AWAITING})

    assert r.tree.get(agent).status == AWAITING, (
        f"a NEED_DECISION from a fix turn parks the run {AWAITING!r}, as an "
        f"ordinary turn does; got {r.tree.get(agent).status!r}")
    assert r.tree.open_questions(agent), (
        "a NEED_DECISION raised during a commit-fix turn was silently dropped: "
        "no open question, so the human never sees it")
    # The loop ends at the asking turn: the first turn plus one fix turn,
    # though two attempts were allowed.
    assert invocations(probe) == 2, (
        f"the fix loop must stop at the turn that asked; saw "
        f"{invocations(probe) - 1} fix turns")
    assert events(r, agent, "commit_failed"), (
        "the commit is still refused, so the failure is appended")


# ==========================================================================
# Limits: a negative / non-numeric bound must fall back to the default (2),
# never disable the loop or, worse, loop forever.
# ==========================================================================

@pytest.mark.parametrize("bad", [-1, "lots", True, "3x"],
                         ids=["negative", "word", "bool", "unit"])
def test_ci_r5_a_malformed_attempts_limit_falls_back_to_the_default(tmp_path, monkeypatch, bad):
    prov, probe = fake_provider(tmp_path, [FIRST, NO_FIX])
    r, project = make(tmp_path, monkeypatch, prov, limits={"commit_fix_attempts": bad})

    agent = run_to_end(r)

    # Default is 2: one first turn plus two resumes.
    assert invocations(probe) == 3, (
        f"commit_fix_attempts={bad!r} must fall back to the shipped default 2, "
        f"not disable the loop nor run unbounded; saw {invocations(probe) - 1} resumes")
    assert len(events(r, agent, "commit_fix_attempt")) == 2
    assert events(r, agent, "commit_failed")
