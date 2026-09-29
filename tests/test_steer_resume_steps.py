"""Regression tests for bug-1b2612.

Two independent defects, both surfacing on the SAME evidence (ag-207058): a
steer respawns a run, the watchdog flags the new process `stuck` almost
immediately, and the orchestrator is told the steer failed even though the
process is alive and goes on to finish the instructed work.

1. `Supervisor.observe` counted steps as `event.step + 1`. Providers such as
   agy number steps monotonically across a resumed session, so a respawned
   run's first event can carry a step index left over from the whole
   conversation (~100), not from this run — and `max_steps` (sized for one
   run) trips on the very first event. Fixed by offsetting from the first
   step index this Supervisor instance ever sees; each run gets its own
   Supervisor (see `Runner._supervisor`), so a genuinely fresh run (first
   step 0) is unaffected.

2. `Runner.steer` treated any node status other than `running`/`pending` as
   proof the respawned run "ended immediately" — but `stuck` is not
   terminal. A trip only fires on a process that is actually running, so a
   `stuck` node is still live and may carry on. Only a node in
   `multiagents.tree.TERMINAL` is a run that actually ended without acting.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents.config import AgentSpec
from multiagents.providers import Event
from multiagents.supervisor import Supervisor
from multiagents.tree import Node

# ---------------------------------------------------------------------------
# 1. Supervisor: a resumed stream starting well above max_steps
# ---------------------------------------------------------------------------


def test_bug_1b2612_resumed_stream_does_not_trip_on_its_first_high_step():
    """agy's first event after a steer can carry a step index from the whole
    prior conversation (here, 100), against a max_steps sized for one run
    (40). It must not trip on that first event."""
    sup = Supervisor(max_steps=40)
    trip = sup.observe(Event(kind="step", step=100))
    assert trip is None, trip
    assert sup.steps == 1, "the run's own first step must count as step 1"


def test_bug_1b2612_resumed_stream_still_trips_after_max_steps_within_the_run():
    """The fix must not disable runaway_steps altogether — it must count
    from this run's own start, not from zero forever."""
    sup = Supervisor(max_steps=40)
    events = [Event(kind="step", step=100 + i) for i in range(41)]
    trips = [sup.observe(e) for e in events]
    assert all(t is None for t in trips[:40]), \
        "the first 40 steps of THIS run must stay under max_steps=40"
    trip = trips[40]
    assert trip is not None and trip.reason == "runaway_steps", trips
    assert trip.detail == "41 steps exceeds max_steps=40", trip.detail


def test_bug_1b2612_a_fresh_run_starting_at_step_zero_is_unaffected():
    """A provider whose steps really do start at 0 for a fresh run must see
    exactly the same counting as before the fix."""
    sup = Supervisor(max_steps=3)
    trips = [sup.observe(Event(kind="step", step=i)) for i in range(4)]
    assert trips[:3] == [None, None, None]
    assert trips[3] is not None and trips[3].reason == "runaway_steps"
    assert trips[3].detail == "4 steps exceeds max_steps=3"


# ---------------------------------------------------------------------------
# 2. Runner.steer: a respawned run flagged `stuck` while still alive
# ---------------------------------------------------------------------------


def _runner(tmp_path, agents=None, providers_yaml=None, git=True, project=None):
    """A `Runner` over a throwaway project, with config injected directly —
    mirrors `test_core.py`'s helper of the same name."""
    import os
    import subprocess

    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner

    paths = ProjectPaths(tmp_path)
    paths.ensure()
    if git:
        env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(tmp_path), *args],
                           capture_output=True, env=env)
    config = Config(
        project=project or {}, providers=providers_yaml or {},
        agents=agents or {}, models={}, instruction_dirs=[],
    )
    return Runner(paths, config)


def _recording_provider(name, probe, linger="sleep 2"):
    """A fake CLI that records its argv, then idles `linger` (alive, doing
    nothing else) — standing in for a real subprocess the watchdog can flag
    `stuck` without it having actually exited."""
    target = str(probe)
    script = (f'printf "%s\\n" "$@" > "{target}.part"; '
              f'mv "{target}.part" "{target}"; {linger}')
    return {
        "bin": "sh",
        "spawn": {
            "args": ["-c", script, name, "--provider", name,
                     "--model", "{model}", "--cwd", "{workdir}"],
            "resume": ["--resume", "{session_id}"],
        },
    }


async def _steer_flagging_stuck(r, agent_id, message, probe):
    """Steer, and the moment the respawned process is up, do exactly what
    the real supervision loop does to a run that trips while still alive:
    mark the node `stuck` and give the confirm loop a sign of life — without
    waiting out STEER_CONFIRM_SECONDS, the way every steer test here does."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(r.steer(agent_id, message))
    deadline = loop.time() + 15
    while not probe.exists() and not task.done() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    run = r.runs.get(agent_id)
    if run is not None:
        r.tree.set_status(agent_id, "stuck",
                          "doom_loop: x called 5x with identical arguments")
        run.events.append({"kind": "tool", "name": "x"})
    return await task


def test_bug_1b2612_steer_reports_success_for_a_live_stuck_run(tmp_path):
    """ag-207058: steer returned `steered: false` while check_agent showed
    the node running and the agent went on to finish the instructed commit.
    A `stuck` node whose process is still alive is not a run that "ended
    immediately" — only a TERMINAL status is."""
    probe = tmp_path / "argv.txt"
    spec = AgentSpec("worker", "p", "m")
    r = _runner(tmp_path, {"worker": spec},
               {"p": _recording_provider("p", probe)})
    worktree = r.paths.worktree("ag-1")
    worktree.mkdir(parents=True)
    r.tree.add(Node(id="ag-1", agent="worker", provider="p", model="m",
                    parent=None, depth=1, status="running", session_id="s-1",
                    worktree=str(worktree)))

    result = asyncio.run(_steer_flagging_stuck(r, "ag-1", "carry on", probe))

    assert probe.exists(), f"nothing was respawned: {result}"
    assert result["steered"] is True, result
    assert result["status"] == "stuck", result
    assert "error" not in result, result
    assert "stuck" in result.get("note", ""), result
