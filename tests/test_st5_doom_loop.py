"""ST5-R1/R3 (stabilization.md at 6d9f23de), through public stream APIs.

Codex frames are its adapter's public NDJSON output; opencode and claude
frames are native NDJSON. Shipped provider rules parse every frame. No
supervisor internals or digest algorithm is part of these assertions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from multiagents import supervisor as supervisor_module
from multiagents.providers import Provider, resolve_inheritance
from multiagents.supervisor import Supervisor

DEFAULTS = Path(__file__).resolve().parents[1] / "src/multiagents/defaults/providers.yaml"
REPEATS = 3
COMMAND = "tail -n 2 suite.log"
BIG = 10**9


@pytest.fixture
def clock(monkeypatch):
    readings = SimpleNamespace(now=100.0)
    monkeypatch.setattr(supervisor_module, "time",
                        SimpleNamespace(monotonic=lambda: readings.now))
    return readings


@pytest.fixture(scope="module")
def providers():
    data = resolve_inheritance(yaml.safe_load(DEFAULTS.read_text())["providers"])
    return {name: Provider.from_dict(name, data[name])
            for name in ("codex", "opencode", "claude")}


def supervisor(clock):
    sup = Supervisor(loop_repeats=REPEATS, silence_timeout=BIG, wall_timeout=BIG,
                     started=clock.now, last_event=clock.now)
    sup.note_progress("sampled-tree-unchanged")
    return sup


def event(providers, provider, frame):
    parsed = providers[provider].parse_line(json.dumps(frame))
    assert parsed is not None, "fixture: a complete JSON frame must parse"
    return parsed


def start(ident, command=COMMAND):
    return {"kind": "tool", "name": "command_execution", "args": {"command": command},
            "state": "in_progress", "tool_id": ident}


def result(ident, output):
    # The adapter's public stream carries a digest, including for empty native
    # output. Generate realistic synthetic digests without asserting their
    # algorithm in the production parser/supervisor.
    digest = hashlib.sha1(output.encode()).hexdigest()[:16]
    return {"kind": "step", "state": "completed", "tool_id": ident, "result": digest}


def completed(provider, ident, output, command=COMMAND):
    if provider == "codex":
        return {**start(ident, command), "state": "completed", "result": result(ident, output)["result"]}
    return {"type": "tool_use", "sessionID": "st5", "part": {
        "id": ident, "tool": "bash", "state": {
            "status": "completed", "input": {"command": command}, "output": output}}}


def feed(sup, providers, provider, frames):
    trips = []
    for frame in frames:
        trip = sup.observe(event(providers, provider, frame))
        if trip is not None:
            trips.append(trip)
    return trips


@pytest.mark.parametrize("provider", ["codex", "opencode"])
@pytest.mark.parametrize("outputs", [["10 passed", "20 passed", "30 passed"],
                                      ["a", "b", "a", "b", "a", "b"],
                                      ["same", "same", "changed"]])
def test_st5_r1_differing_results_do_not_trip(clock, providers, provider, outputs):
    sup = supervisor(clock)
    assert feed(sup, providers, provider,
                [completed(provider, str(i), out) for i, out in enumerate(outputs)]) == []


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r1_identical_results_trip_only_at_the_limit(clock, providers, provider):
    sup = supervisor(clock)
    for i in range(REPEATS - 1):
        assert feed(sup, providers, provider, [completed(provider, str(i), "unchanged")]) == []
    trips = feed(sup, providers, provider, [completed(provider, "last", "unchanged")])
    assert [trip.reason for trip in trips] == ["doom_loop"]


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r1_identical_results_with_different_arguments_do_not_trip(clock, providers, provider):
    sup = supervisor(clock)
    frames = [completed(provider, str(i), "passed", f"pytest test_case_{i}.py")
              for i in range(REPEATS * 2)]
    assert feed(sup, providers, provider, frames) == []


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r1_new_result_starts_a_full_repeat_streak(clock, providers, provider):
    sup = supervisor(clock)
    frames = [completed(provider, str(i), output)
              for i, output in enumerate(["old", "new", "new", "new"])]
    assert feed(sup, providers, provider, frames[:-1]) == []
    trips = feed(sup, providers, provider, frames[-1:])
    assert [trip.reason for trip in trips] == ["doom_loop"]


def test_st5_r1_codex_waits_for_the_threshold_calls_result(clock, providers):
    sup = supervisor(clock)
    for i in range(REPEATS - 1):
        assert feed(sup, providers, "codex", [start(str(i)), result(str(i), "same")]) == []
    assert feed(sup, providers, "codex", [start("pending")]) == []
    clock.now += 100
    assert sup.check_timers() is None
    trips = feed(sup, providers, "codex", [result("pending", "same")])
    assert [trip.reason for trip in trips] == ["doom_loop"]


def test_st5_r1_codex_late_changed_result_does_not_trip(clock, providers):
    sup = supervisor(clock)
    for i in range(REPEATS - 1):
        assert feed(sup, providers, "codex", [start(str(i)), result(str(i), "same")]) == []
    assert feed(sup, providers, "codex", [start("pending")]) == []
    assert feed(sup, providers, "codex", [result("pending", "new progress")]) == []


@pytest.mark.parametrize("changing", [False, True])
def test_st5_r1_parallel_crossed_results_follow_call_ids(clock, providers, changing):
    sup = supervisor(clock)
    trips = []
    for i in range(REPEATS):
        # Completion frames carry only the id and result: the latest started
        # call's arguments cannot identify an earlier call's completion.
        frames = [start(f"a{i}", "tail a.log"), start(f"b{i}", "tail b.log"),
                  result(f"b{i}", f"b-{i}" if changing else "fixed-b"),
                  result(f"a{i}", f"a-{i}" if changing else "fixed-a")]
        trips += feed(sup, providers, "codex", frames)
    assert [trip.reason for trip in trips] == ([] if changing else ["doom_loop"])


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r1_empty_result_is_evidence_and_breaks_a_nonempty_streak(clock, providers, provider):
    sup = supervisor(clock)
    assert feed(sup, providers, provider,
                [completed(provider, str(i), out) for i, out in enumerate(["nonempty", "", ""])]) == []
    trips = feed(sup, providers, provider, [completed(provider, "third-empty", "")])
    assert [trip.reason for trip in trips] == ["doom_loop"]


def test_st5_r1_claude_without_results_keeps_arguments_and_disk_fallback(clock, providers):
    sup = supervisor(clock)
    frames = [{"type": "assistant", "message": {"id": str(i), "content": [{
        "type": "tool_use", "id": str(i), "name": "Bash", "input": {"command": COMMAND}}]}}
              for i in range(REPEATS)]
    trips = feed(sup, providers, "claude", frames)
    assert [trip.reason for trip in trips] == ["doom_loop"]


@pytest.mark.parametrize("provider", ["codex", "opencode", "claude"])
def test_st5_r1_disk_progress_prevents_identical_repeats(clock, providers, provider):
    sup = supervisor(clock)
    for i in range(REPEATS * 2):
        sup.note_progress(f"tree-sample-{i}")
        frame = completed(provider, str(i), "same") if provider != "claude" else {
            "type": "assistant", "message": {"id": str(i), "content": [{
                "type": "tool_use", "name": "Bash", "input": {"command": COMMAND}}]}}
        assert feed(sup, providers, provider, [frame]) == []


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r1_existing_two_step_cycle_is_kept(clock, providers, provider):
    sup = supervisor(clock)
    frames = [completed(provider, f"{i}-{name}", f"fixed-{name}", f"cat {name}.log")
              for i in range(REPEATS) for name in ("a", "b")]
    assert feed(sup, providers, provider, frames[:-1]) == []
    trips = feed(sup, providers, provider, frames[-1:])
    assert [trip.reason for trip in trips] == ["doom_loop"]


@pytest.mark.parametrize("provider", ["codex", "opencode"])
def test_st5_r3_loop_reason_names_arguments_and_result_digest(clock, providers, provider):
    sup = supervisor(clock)
    frames = [completed(provider, str(i), "fixed output") for i in range(REPEATS)]
    digest = event(providers, provider, frames[-1]).result
    trips = feed(sup, providers, provider, frames)
    assert len(trips) == 1 and trips[0].reason == "doom_loop"
    assert COMMAND in trips[0].detail, "ST5-R3: name the repeated arguments"
    assert digest and digest in trips[0].detail, "ST5-R3: name the repeated result digest"


def test_st5_r3_fallback_reason_names_unchanged_disk(clock, providers):
    sup = supervisor(clock)
    frames = [{"type": "assistant", "message": {"content": [{
        "type": "tool_use", "name": "Bash", "input": {"command": COMMAND}}]}}
              for _ in range(REPEATS)]
    trips = feed(sup, providers, "claude", frames)
    assert len(trips) == 1 and trips[0].reason == "doom_loop"
    detail = trips[0].detail.lower()
    assert "disk" in detail and any(word in detail for word in ("unchanged", "nothing", "no change"))
