"""TB2-R2: the doom-loop watchdog does not flag commands whose output changes.

Contract: `context/specs/tooling-batch-2-2026-10.md`. A repeated identical
command counts toward `limits.doom_loop_repeats` only when its output is also
identical to the previous repetition; when the provider's event carries no
output, only if nothing changed on disk AND no file the worktree holds grew.

Two layers, both black box:
- PIPELINE: codex-shaped raw events from a fake native codex, through the
  shipped codex provider under a real `Runner`; observed in the `stuck` events.
- RUN: a scripted fake CLI under a real `Runner` whose tool events carry no
  output, while the script appends to a log in its worktree. Observed through
  the node's status and the project's `stuck` events.

Assumptions where the spec is silent (kept loose):
- "grew" is read as: a file in the worktree is bigger than at the previous
  repetition. Growth of an UNTRACKED, never-committed file counts.
- A run-level trip is `doom_loop` in the event log; nothing about its detail.
- Alternating outputs (A, B, A, B) never equal the PREVIOUS repetition, so they
  do not trip.
- RUN repetitions are spaced PACE seconds apart so any reasonable sampling of
  the worktree sees the growth between them; no internal seam is patched.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as ch                            # noqa: E402
from support import c3_harness as h                                # noqa: E402

BIG = 10 ** 9
REPEATS = 3
CMD = "tail -n 2 x.log"
UNTIL_BOUND = 15.0
PACE = 3.3   # seconds between repetitions in the RUN layer


# ================================================================ PIPELINE ==

def _codex_run(tmp_path, monkeypatch, calls, *, command: str = CMD) -> list[str]:
    """The `stuck` reasons of a real `Runner` run on the shipped codex provider
    whose native CLI (a fake) makes one `command_execution` per entry of
    `calls`. An entry is an output string, a `(command, output)` pair, or None
    for a completion that carries no output."""
    fake = ch.FakeCodex(tmp_path)
    raw = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
    for i, call in enumerate(calls):
        if isinstance(call, dict):      # a raw event line, verbatim
            raw.append(call)
            continue
        cmd, body = call if isinstance(call, tuple) else (command, call)
        item = {"id": f"c{i}", "type": "command_execution", "command": cmd,
                "status": "in_progress"}
        done = {**item, "status": "completed", "exit_code": 0}
        if body is not None:
            done["aggregated_output"] = body
        raw += [{"type": "item.started", "item": item},
                {"type": "item.completed", "item": done}]
    raw += [{"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}}]
    fake.set(events=raw)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "auth.json").write_text("{}")
    monkeypatch.setenv("MULTIAGENTS_CODEX_PROFILE", str(profile))
    block = dict(ch.block())
    block["bin"] = str(fake.path)
    r = h.make_runner(tmp_path / "proj", monkeypatch,
                      agents={"worker": h.AgentSpec("worker", "codex", "gpt-x", writes=True)},
                      providers={"codex": block},
                      project={"limits": {"doom_loop_repeats": REPEATS}})

    async def go():
        res = await r.start("worker", "go")
        run = r.runs.get(res["agent_id"])
        if run is not None:
            await asyncio.wait_for(run.done.wait(), UNTIL_BOUND)
    asyncio.run(go())
    return _stuck_events(r)


def test_tb2_r2_codex_tail_of_a_growing_log_does_not_trip(tmp_path, monkeypatch):
    assert _codex_run(tmp_path, monkeypatch,
                      [f"line {i}\nline {i + 1}\n" for i in range(12)]) == []


def test_tb2_r2_codex_growing_output_never_trips_at_any_threshold_multiple(tmp_path, monkeypatch):
    """Re-arming must not bring a trip back later: 40 repeats, all different."""
    assert _codex_run(tmp_path, monkeypatch, [f"progress {i}%\n" for i in range(40)]) == []


def test_tb2_r2_codex_identical_command_and_output_still_trips(tmp_path, monkeypatch):
    assert "doom_loop" in _codex_run(tmp_path, monkeypatch, ["same\nsame\n"] * REPEATS)


def test_tb2_r2_codex_one_repeat_short_of_the_limit_does_not_trip(tmp_path, monkeypatch):
    assert _codex_run(tmp_path, monkeypatch, ["same\n"] * (REPEATS - 1)) == []


def test_tb2_r2_codex_output_that_changes_once_then_stalls_trips_after_the_stall(tmp_path, monkeypatch):
    assert "doom_loop" in _codex_run(tmp_path, monkeypatch, ["a\n", "b\n"] + ["c\n"] * 6)


def test_tb2_r2_codex_stall_shorter_than_the_limit_after_a_change_does_not_trip(tmp_path, monkeypatch):
    assert _codex_run(tmp_path, monkeypatch, ["a\n", "b\n", "c\n", "c\n"]) == []


def test_tb2_r2_codex_calls_that_report_no_output_and_change_nothing_still_trip(tmp_path, monkeypatch):
    assert "doom_loop" in _codex_run(tmp_path, monkeypatch, [None] * REPEATS)


def test_tb2_r2_codex_output_alternating_between_two_values_does_not_trip(tmp_path, monkeypatch):
    """Each repetition differs from the PREVIOUS one, so none counts."""
    assert _codex_run(tmp_path, monkeypatch, ["A\n", "B\n"] * 8) == []


def test_tb2_r2_codex_real_sequence_three_tails_with_interleaved_events_does_not_trip(tmp_path, monkeypatch):
    """The shape of the real trip (ag-89e3a7): three `tail -n 2` calls with three
    different outputs, a message between them, and an out-of-order completion of
    an earlier, failed call."""
    def started(i, cmd):
        return {"type": "item.started", "item": {"id": f"item_{i}", "type": "command_execution",
                "command": cmd, "status": "in_progress"}}

    def completed(i, cmd, out, status="completed", code=0):
        return {"type": "item.completed", "item": {"id": f"item_{i}", "type": "command_execution",
                "command": cmd, "status": status, "exit_code": code, "aggregated_output": out}}
    t = "/bin/bash -lc 'tail -n 2 nt-baseline.log'"
    calls = [started(30, t), completed(30, t, "ok 10\nok 11\n"),
             started(31, t), completed(31, t, "ok 20\nok 21\n"),
             {"type": "item.completed", "item": {"id": "item_32", "type": "agent_message",
                                                 "text": "The baseline is nearing completion."}},
             completed(17, "pytest", "", status="failed", code=1),
             started(33, t), completed(33, t, "ok 30\nok 31\n")]
    assert _codex_run(tmp_path, monkeypatch, calls) == []


def test_tb2_r2_codex_different_commands_with_identical_output_do_not_trip(tmp_path, monkeypatch):
    calls = [(f"tail -n {i + 1} x.log", "same\n") for i in range(6)]
    assert _codex_run(tmp_path, monkeypatch, calls) == []


# ===================================================================== RUN ==

_CLI = r'''#!{python}
import json, os, sys, time
from pathlib import Path

PROBE = Path({probe!r})
GROW = {grow!r}
N = {n!r}
LOG = Path("x.log")
LOG.write_text("line 0\n")
for i in range(N):
    print(json.dumps({{"type": "tool", "session_id": "s", "name": "command_execution",
                      "input": {{"command": {cmd!r}}}}}))
    sys.stdout.flush()
    # Leave time for the engine to take its look at the tree.
    time.sleep({pace!r})
    if GROW:
        with LOG.open("a") as f:
            f.write("line %d\\n" % (i + 1))
(PROBE / "emitted").write_text("x")
deadline = time.time() + 20
while not (PROBE / "release").exists() and time.time() < deadline:
    time.sleep(0.05)
print(json.dumps({{"type": "text", "session_id": "s", "text": "done"}}))
sys.exit(0)
'''


def _runner(tmp_path, monkeypatch, *, grow: bool, n: int):
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "cli.py"
    script.write_text(_CLI.format(python=sys.executable, probe=str(probe),
                                  grow=grow, n=n, cmd=CMD, pace=PACE))
    script.chmod(0o755)
    prov = {"bin": str(script), "spawn": {"args": ["--fake"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session_id"],
                       "rules": [
                           {"match": {"type": "tool"}, "as": "tool",
                            "fields": {"name": "name", "args": "input"}},
                           {"match": {"type": "text"}, "as": "text",
                            "fields": {"text": "text"}}]}}
    r = h.make_runner(tmp_path / "proj", monkeypatch,
                      agents={"worker": h.AgentSpec("worker", "p", "m", writes=True)},
                      providers={"p": prov},
                      project={"limits": {"doom_loop_repeats": REPEATS}})
    return r, probe


def _stuck_events(r) -> list[str]:
    path = r.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "stuck":
            out.append(e.get("reason"))
    return out


def _drive(r, probe) -> list[str]:
    """Run to the point the script has emitted every repetition, then release
    it and return the `stuck` reasons seen over the whole run."""
    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        deadline = time.monotonic() + UNTIL_BOUND + PACE * 4
        while not (probe / "emitted").exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert (probe / "emitted").exists(), "fixture: the script never finished its calls"
        await asyncio.sleep(1.0)
        seen = _stuck_events(r)
        (probe / "release").write_text("x")
        run = r.runs.get(agent)
        if run is not None:
            await asyncio.wait_for(run.done.wait(), 20)
        return seen
    return asyncio.run(go())


def test_tb2_r2_a_repeated_command_with_no_output_on_a_growing_log_is_not_stuck(tmp_path, monkeypatch):
    r, probe = _runner(tmp_path, monkeypatch, grow=True, n=REPEATS)
    assert _drive(r, probe) == []


def test_tb2_r2_a_repeated_command_with_no_output_and_nothing_changing_is_stuck(tmp_path, monkeypatch):
    """Control: the same script without the growth is the loop the watchdog is for."""
    r, probe = _runner(tmp_path, monkeypatch, grow=False, n=REPEATS)
    assert "doom_loop" in _drive(r, probe)
