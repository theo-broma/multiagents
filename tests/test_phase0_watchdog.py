"""Phase 0, group B — the watchdog reports per condition and re-arms only what
can recur. Contract: `context/specs/phase0-runtime-repairs.md`, P0-R2.1–R2.8.

Black box: a `Supervisor` is driven only through its public surface
(construction, `observe`, `note_progress`, `check_timers`), and the runner only
through `start` / `steer` / `stop` with a fake CLI, reading what it leaves in
the tree and the events log.

Time is real and short (hundredths of a second) at the supervisor level, as the
existing watchdog tests in `test_core.py` do. The runner's timer loop polls on
its own fixed interval (5 s today), so the runner-level tests take seconds each.

P0-R2.6 ("the first trip is reported exactly as today") is verified by the
existing tests in `test_core.py`, which this file leaves untouched.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.providers import Event                            # noqa: E402
from multiagents.supervisor import Supervisor                      # noqa: E402
from support import c3_harness as h                                # noqa: E402

BIG = 10 ** 9


def _sup(**kw) -> Supervisor:
    """A supervisor with every condition disabled except those a test names."""
    kw.setdefault("silence_timeout", BIG)
    kw.setdefault("wall_timeout", BIG)
    kw.setdefault("max_steps", BIG)
    return Supervisor(**kw)


def _tool(name: str = "view_file", **args) -> Event:
    return Event(kind="tool", name=name, args=args or {"path": "/w/a.py"})


def _step() -> Event:
    return Event(kind="step")


def _feed(sup: Supervisor, events) -> list:
    """Observe every event; return the trips reported, in order."""
    return [t for t in (sup.observe(e) for e in events) if t is not None]


def _poll(sup: Supervisor, times: int, gap: float) -> list:
    """`times` timer checks, `gap` seconds apart; return the trips reported."""
    trips = []
    for _ in range(times):
        time.sleep(gap)
        trip = sup.check_timers()
        if trip is not None:
            trips.append(trip)
    return trips


def _reasons(trips) -> list[str]:
    return [t.reason for t in trips]


# ===========================================================================
# P0-R2.1 — a trip whose reason differs from every one already reported is
# reported
# ===========================================================================

def test_p0_r2_1_timeout_after_doom_loop_is_reported():
    sup = _sup(loop_repeats=3, wall_timeout=0.05)
    assert _reasons(_feed(sup, [_tool()] * 3)) == ["doom_loop"]
    time.sleep(0.1)
    trip = sup.check_timers()
    assert trip is not None and trip.reason == "timeout", trip


def test_p0_r2_1_runaway_steps_after_doom_loop_is_reported():
    sup = _sup(loop_repeats=3, max_steps=5)
    trips = _feed(sup, [_tool()] * 3 + [_step() for _ in range(6)])
    assert _reasons(trips) == ["doom_loop", "runaway_steps"]


def test_p0_r2_1_doom_loop_after_runaway_steps_is_reported():
    sup = _sup(loop_repeats=3, max_steps=2)
    trips = _feed(sup, [_step() for _ in range(3)] + [_tool()] * 3)
    assert _reasons(trips) == ["runaway_steps", "doom_loop"]


def test_p0_r2_1_silence_after_timeout_is_reported():
    """A terminal timeout must not stop the silence check behind it."""
    sup = _sup(wall_timeout=0.02, silence_timeout=0.05)
    trips = _poll(sup, times=12, gap=0.03)
    assert sorted(_reasons(trips)) == ["silence", "timeout"], _reasons(trips)
    assert trips[0].reason == "timeout"


def test_p0_r2_1_timeout_after_silence_is_reported():
    sup = _sup(silence_timeout=0.02, wall_timeout=0.25)
    trips = _poll(sup, times=15, gap=0.03)
    assert _reasons(trips) == ["silence", "timeout"], _reasons(trips)


# ===========================================================================
# P0-R2.2 — doom_loop re-arms after a further `doom_loop_rearm` repeats
# (default: equal to doom_loop_repeats)
# ===========================================================================

def test_p0_r2_2_doom_loop_rearms_after_default_repeats():
    """The contract's own example: 5 → trip; 4 more → nothing; 5th → trip."""
    sup = _sup(loop_repeats=5)
    assert _reasons(_feed(sup, [_tool()] * 5)) == ["doom_loop"]
    for n in range(4):
        assert sup.observe(_tool()) is None, f"re-arm tripped early at +{n + 1}"
    trip = sup.observe(_tool())
    assert trip is not None and trip.reason == "doom_loop"


def test_p0_r2_2_doom_loop_keeps_rearming_at_small_threshold():
    """Boundary at loop_repeats=3: trips on calls 3, 6 and 9 exactly."""
    sup = _sup(loop_repeats=3)
    tripped_at = [i + 1 for i in range(10) if sup.observe(_tool()) is not None]
    assert tripped_at == [3, 6, 9]


def test_p0_r2_2_unrelated_call_resets_the_rearm_count():
    sup = _sup(loop_repeats=5)
    assert _reasons(_feed(sup, [_tool()] * 5)) == ["doom_loop"]
    assert _feed(sup, [_tool()] * 3) == []
    assert sup.observe(_tool("grep_search", Query="other")) is None
    # Without the reset this would be 3 + 4 = 7 repeats since the trip.
    assert _feed(sup, [_tool()] * 4) == [], "the unrelated call must reset the count"
    trip = sup.observe(_tool())
    assert trip is not None and trip.reason == "doom_loop"


def test_p0_r2_2_working_tree_change_resets_the_rearm_count():
    sup = _sup(loop_repeats=5)
    sup.note_progress("tree-A")
    assert _reasons(_feed(sup, [_tool()] * 5)) == ["doom_loop"]
    assert _feed(sup, [_tool()] * 3) == []
    sup.note_progress("tree-B")                  # something landed on disk
    assert _feed(sup, [_tool()] * 4) == [], "a tree change must reset the count"
    trip = sup.observe(_tool())
    assert trip is not None and trip.reason == "doom_loop"


def test_p0_r2_2_lifecycle_duplicates_do_not_count_twice_toward_rearm():
    """agy reports one call as ACTIVE then DONE with the same step index; that
    is one repeat for re-arm, exactly as it is one for the first trip."""
    sup = _sup(loop_repeats=5)

    def call(step):
        return [Event(kind="tool", name="view_file", args={"p": "a.py"},
                      state=state, step=step) for state in ("ACTIVE", "DONE")]

    events = [e for step in range(1, 6) for e in call(step)]
    assert _reasons(_feed(sup, events)) == ["doom_loop"]
    events = [e for step in range(6, 10) for e in call(step)]  # 4 more calls
    assert _feed(sup, events) == [], "8 lifecycle events are 4 calls, not 8"
    assert _reasons(_feed(sup, call(10))) == ["doom_loop"]


def _trip_calls(sup, events) -> list[int]:
    """1-based positions of the calls on which a doom_loop trip fired."""
    return [i for i, e in enumerate(events, 1)
            if "doom_loop" in _reasons(_feed(sup, [e]))]


def test_p0_r2_2_a_new_loop_on_another_signature_is_a_fresh_detection():
    """Settled: a different signature resets the re-arm count; a loop on it is
    detected afresh at doom_loop_repeats — on its 5th call, not its 4th."""
    sup = _sup(loop_repeats=5)
    assert _reasons(_feed(sup, [_tool()] * 5)) == ["doom_loop"]
    c = _tool("run_command", CommandLine="pytest -q")
    assert _trip_calls(sup, [c] * 10) == [5, 10]


def test_p0_r2_2_two_step_cycle_rearms_once_per_repeats_pairs():
    """Settled: for an A,B cycle the re-arm unit is one pair. loop_repeats=3:
    trips after the 3rd, 6th and 9th pair, i.e. on calls 6, 12 and 18."""
    sup = _sup(loop_repeats=3)
    a, b = _tool("a", k=1), _tool("b", k=1)
    assert _trip_calls(sup, [a, b] * 9) == [6, 12, 18]


def test_p0_r2_2_supervisor_takes_loop_rearm():
    """Settled keyword: `loop_rearm`. First trip at loop_repeats, then every
    loop_rearm further calls."""
    sup = _sup(loop_repeats=3, loop_rearm=5)
    assert _trip_calls(sup, [_tool()] * 13) == [3, 8, 13]


def test_p0_r2_2_loop_rearm_of_one_reports_every_further_call():
    sup = _sup(loop_repeats=3, loop_rearm=1)
    assert _trip_calls(sup, [_tool()] * 6) == [3, 4, 5, 6]


def test_p0_r2_2_loop_rearm_defaults_to_loop_repeats():
    sup = _sup(loop_repeats=4)
    assert _trip_calls(sup, [_tool()] * 12) == [4, 8, 12]


def test_p0_r2_2_shipped_config_declares_doom_loop_rearm():
    path = Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults" / "project.yaml"
    text = path.read_text()
    limits = yaml.safe_load(text)["limits"]
    assert "doom_loop_rearm" in limits, "new key under limits:"
    assert limits["doom_loop_rearm"] == limits["doom_loop_repeats"], \
        "default equal to doom_loop_repeats"
    lines = text.splitlines()
    at = next(i for i, l in enumerate(lines) if re.match(r"\s+doom_loop_rearm\s*:", l))
    assert "#" in lines[at] or lines[at - 1].strip().startswith("#"), \
        "documented with a one-line comment"


# ===========================================================================
# P0-R2.3 — runaway_steps and timeout are terminal: at most once per run
# ===========================================================================

def test_p0_r2_3_runaway_steps_boundary_and_reported_once():
    sup = _sup(max_steps=3)
    assert _feed(sup, [_step() for _ in range(3)]) == [], "== max_steps is allowed"
    trip = sup.observe(_step())
    assert trip is not None and trip.reason == "runaway_steps"
    assert trip.detail == "4 steps exceeds max_steps=3", "first trip exactly as today"
    assert _feed(sup, [_step() for _ in range(100)]) == [], "terminal: once per run"


def test_p0_r2_3_runaway_steps_once_with_step_indices():
    sup = _sup(max_steps=5)
    trips = _feed(sup, [Event(kind="step", step=i) for i in range(120)])
    assert _reasons(trips) == ["runaway_steps"]


def test_p0_r2_3_timeout_reported_once_across_polls():
    sup = _sup(wall_timeout=0.01)
    trips = _poll(sup, times=20, gap=0.005)
    assert _reasons(trips) == ["timeout"]


def test_p0_r2_3_timeout_reported_once_while_events_keep_flowing():
    """Events between polls keep silence away, and must not re-arm timeout."""
    sup = _sup(wall_timeout=0.01, silence_timeout=BIG)
    trips = []
    for _ in range(20):
        time.sleep(0.005)
        trips += _feed(sup, [_step()])
        trip = sup.check_timers()
        if trip:
            trips.append(trip)
    assert _reasons(trips) == ["timeout"]


# ===========================================================================
# P0-R2.4 — silence is per episode
# ===========================================================================

def test_p0_r2_4_one_long_quiet_period_is_one_report():
    sup = _sup(silence_timeout=0.02)
    trips = _poll(sup, times=12, gap=0.03)
    assert _reasons(trips) == ["silence"]


def test_p0_r2_4_an_event_rearms_silence():
    sup = _sup(silence_timeout=0.02)
    assert _reasons(_poll(sup, times=6, gap=0.03)) == ["silence"]
    sup.observe(_step())                          # the agent spoke again
    assert _reasons(_poll(sup, times=8, gap=0.03)) == ["silence"], \
        "a second quiet period is a second report"
    assert _poll(sup, times=4, gap=0.03) == [], "and still one per period"


def test_p0_r2_4_a_moving_tree_alone_does_not_rearm_silence():
    """Re-arms 'only after a stream event has arrived'."""
    sup = _sup(silence_timeout=0.02)
    assert _reasons(_poll(sup, times=6, gap=0.03)) == ["silence"]
    trips = []
    for n in range(8):
        sup.note_progress(f"tree-{n}")
        trips += _poll(sup, times=1, gap=0.03)
    assert trips == [], _reasons(trips)


# ===========================================================================
# Runner level — P0-R2.1, R2.2, R2.5, R2.7, R2.8
# ===========================================================================

SID = "sess-1"


def _cli(tmp_path, events, *, then_sleep: float, name: str = "p") -> dict:
    """A fake CLI: prints `events` as NDJSON, then stays alive `then_sleep`
    seconds. Its provider block classifies tool, step and text lines."""
    prov = h.fake_cli(tmp_path, name, events=events)
    script = Path(prov["bin"])
    script.write_text(script.read_text().replace(
        "sys.exit(0)", f"time.sleep({then_sleep!r})\nsys.exit(0)"))
    prov["spawn"]["resume"] = ["--resume", "{session_id}"]
    prov["stream"]["session_id_paths"] = ["session_id"]
    prov["stream"]["rules"] = [
        {"match": {"type": "tool"}, "as": "tool",
         "fields": {"name": "name", "args": "input"}},
        {"match": {"type": "step"}, "as": "step", "fields": {}},
        *prov["stream"]["rules"],
    ]
    return prov


def _tool_line(**input):
    return {"type": "tool", "session_id": SID, "name": "view_file",
            "input": input or {"path": "a.py"}}


def _step_line():
    return {"type": "step", "session_id": SID}


def _events(r) -> list[dict]:
    path = r.paths.events_file
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _stuck(r, agent_id) -> list[str]:
    return [e.get("reason") for e in _events(r)
            if e.get("kind") == "stuck" and e.get("agent") == agent_id]


async def _until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return predicate()


def _runner(tmp_path, monkeypatch, prov, *, project=None, **spec_kw):
    spec = h.AgentSpec("worker", "p", "m", **spec_kw)
    return h.make_runner(tmp_path, monkeypatch, agents={"worker": spec},
                         providers={"p": prov}, project=project)


def test_p0_r2_2_runner_reads_doom_loop_rearm_from_limits(tmp_path, monkeypatch):
    """limits.doom_loop_rearm is the knob: repeats=2, rearm=4, and 2 + 3
    identical calls give one stuck event, 2 + 4 give two."""
    def run(n_after, sub):
        base = tmp_path / sub
        base.mkdir()
        prov = _cli(base, [_tool_line() for _ in range(2 + n_after)], then_sleep=0)
        r = _runner(base, monkeypatch, prov, project={"limits": {
            "doom_loop_repeats": 2, "doom_loop_rearm": 4}})

        async def go():
            res = await r.start("worker", "go")
            await asyncio.wait_for(r.runs[res["agent_id"]].done.wait(), 20)
            return res["agent_id"]
        agent = asyncio.run(go())
        return _stuck(r, agent)

    assert run(3, "a") == ["doom_loop"]
    assert run(4, "b") == ["doom_loop", "doom_loop"]


def test_p0_r2_2_runner_rearm_defaults_to_doom_loop_repeats(tmp_path, monkeypatch):
    prov = _cli(tmp_path, [_tool_line() for _ in range(4)], then_sleep=0)
    r = _runner(tmp_path, monkeypatch, prov,
                project={"limits": {"doom_loop_repeats": 2}})

    async def go():
        res = await r.start("worker", "go")
        await asyncio.wait_for(r.runs[res["agent_id"]].done.wait(), 20)
        return res["agent_id"]
    agent = asyncio.run(go())
    assert _stuck(r, agent) == ["doom_loop", "doom_loop"]


def test_p0_r2_1_and_r2_5_timeout_after_loop_is_a_second_stuck_event(tmp_path, monkeypatch):
    """Two stuck events, and the node's status and reason follow the latest."""
    prov = _cli(tmp_path, [_tool_line() for _ in range(3)], then_sleep=12)
    r = _runner(tmp_path, monkeypatch, prov, timeout=1,
                project={"limits": {"doom_loop_repeats": 3}})

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        seen = await _until(lambda: len(_stuck(r, agent)) >= 2, 11)
        node = r.tree.get(agent)
        snapshot = (seen, list(_stuck(r, agent)), node.status, node.reason)
        await r.stop(agent)
        return snapshot
    seen, reasons, status, reason = asyncio.run(go())
    assert reasons == ["doom_loop", "timeout"], reasons
    assert status == "stuck"
    assert reason.startswith("timeout"), reason


# ===========================================================================
# P0-R2.9 — the timer loop survives: timeouts reach the tree, and one failing
# poll is recorded and does not end the loop
# ===========================================================================

# some text, so the run's early exit is never mistaken for a silent crash
SAID = {"type": "text", "text": "working"}


def test_p0_r2_9_wall_timeout_reaches_the_tree(tmp_path, monkeypatch):
    """timeout=1 and an agent alive ~7 s: the first poll (at ~5 s) trips."""
    prov = _cli(tmp_path, [_step_line(), SAID], then_sleep=7)
    r = _runner(tmp_path, monkeypatch, prov, timeout=1)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        await asyncio.wait_for(r.runs[agent].done.wait(), 20)
        return _stuck(r, agent)
    assert asyncio.run(go()) == ["timeout"]


def test_p0_r2_9_a_failing_poll_is_recorded_and_the_next_poll_runs(tmp_path, monkeypatch):
    """check_timers raises once (poll 1, ~5 s); the failure lands in the event
    log and poll 2 (~10 s) still reports the timeout."""
    real = Supervisor.check_timers
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom-injected")
        return real(self, *a, **kw)
    monkeypatch.setattr(Supervisor, "check_timers", flaky)
    prov = _cli(tmp_path, [_step_line(), SAID], then_sleep=13)
    r = _runner(tmp_path, monkeypatch, prov, timeout=1)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        await asyncio.wait_for(r.runs[agent].done.wait(), 25)
        return agent
    agent = asyncio.run(go())
    mine = [e for e in _events(r) if e.get("agent") == agent]
    assert any("boom-injected" in json.dumps(e) for e in mine), \
        "the failed poll is recorded as an event"
    assert _stuck(r, agent) == ["timeout"], "the next poll still ran"


def test_p0_r2_8_idle_polls_after_a_terminal_trip_write_nothing(tmp_path, monkeypatch):
    prov = _cli(tmp_path, [_step_line()], then_sleep=20)
    r = _runner(tmp_path, monkeypatch, prov, timeout=1)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await _until(lambda: _stuck(r, agent) == ["timeout"], 8), _stuck(r, agent)
        await asyncio.sleep(0.5)                 # let the trip's own writes land
        tree_before = r.paths.tree_file.read_bytes()
        events_before = r.paths.events_file.read_bytes()
        await asyncio.sleep(11)                  # at least two further timer polls
        after = (r.paths.tree_file.read_bytes(), r.paths.events_file.read_bytes())
        await r.stop(agent)
        return tree_before, events_before, after
    tree_before, events_before, (tree_after, events_after) = asyncio.run(go())
    assert events_after == events_before, "idle polls appended to the events log"
    assert tree_after == tree_before, "idle polls rewrote the tree"


def test_p0_r2_7_steer_starts_a_fresh_watchdog_timeout(tmp_path, monkeypatch):
    prov = _cli(tmp_path, [_step_line()], then_sleep=12)
    r = _runner(tmp_path, monkeypatch, prov, timeout=1)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await _until(lambda: _stuck(r, agent) == ["timeout"], 8), _stuck(r, agent)
        steered = await r.steer(agent, "carry on")
        assert steered.get("steered"), steered
        await _until(lambda: len(_stuck(r, agent)) >= 2, 8)
        reasons = list(_stuck(r, agent))
        await r.stop(agent)
        return reasons
    assert asyncio.run(go()) == ["timeout", "timeout"]


def test_p0_r2_7_steer_restarts_the_step_count(tmp_path, monkeypatch):
    """Turn 1 trips runaway_steps at max_steps=2. Turn 2 takes exactly 2 steps,
    which is within the limit only if the count restarted."""
    turn = tmp_path / "turn"
    prov = _cli(tmp_path, [], then_sleep=0)
    script = Path(prov["bin"])
    # First invocation: 3 steps; any later one: 2 steps. Both stay alive.
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time, pathlib\n"
        f"marker = pathlib.Path({str(turn)!r})\n"
        "n = 2 if marker.exists() else 3\n"
        "marker.write_text('x')\n"
        "for _ in range(n):\n"
        f"    print(json.dumps({{'type': 'step', 'session_id': {SID!r}}}), flush=True)\n"
        "time.sleep(8)\n")
    r = _runner(tmp_path, monkeypatch, prov, max_steps=2)

    async def go():
        res = await r.start("worker", "go")
        agent = res["agent_id"]
        assert await _until(lambda: _stuck(r, agent) == ["runaway_steps"], 5), _stuck(r, agent)
        steered = await r.steer(agent, "carry on")
        assert steered.get("steered"), steered
        await asyncio.sleep(1.5)
        reasons = list(_stuck(r, agent))
        await r.stop(agent)
        return reasons
    assert asyncio.run(go()) == ["runaway_steps"], "turn 2's 2 steps must not trip"
