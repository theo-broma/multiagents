"""tests/test_doom_loop_result_changes.py — bug-2e68e4.

Polling a long-running log with the same command (`tail -3 x.log`) repeats the
call signature while the output moves on (13% -> 18% -> 25%). That is progress,
not a doom loop. A repeat counts only while the result is unchanged; a provider
that reports no result keeps the signature-only behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.providers import Event                            # noqa: E402
from multiagents.supervisor import Supervisor                      # noqa: E402

BIG = 10 ** 9
CMD = {"command": "tail -3 /var/tmp/x.log"}


def _sup(**kw) -> Supervisor:
    kw.setdefault("silence_timeout", BIG)
    kw.setdefault("wall_timeout", BIG)
    kw.setdefault("max_steps", BIG)
    kw.setdefault("loop_repeats", 3)
    return Supervisor(**kw)


def _call(i: int, result: str = "") -> Event:
    # opencode shape: the call and its output arrive on one event.
    return Event(kind="tool", name="command_execution", args=dict(CMD),
                 tool_id=f"c{i}", result=result)


def _done(i: int, result: str) -> Event:
    # codex shape: the output arrives on the completed step.
    return Event(kind="step", tool_id=f"c{i}", result=result)


def test_changing_output_never_trips_same_event():
    sup = _sup()
    for i in range(30):
        assert sup.observe(_call(i, f"{i}%")) is None


def test_changing_output_never_trips_separate_result_event():
    sup = _sup()
    for i in range(30):
        assert sup.observe(_call(i)) is None
        assert sup.observe(_done(i, f"{i}%")) is None


def test_unchanged_output_trips_at_the_limit_same_event():
    sup = _sup()
    assert sup.observe(_call(0, "13%")) is None
    assert sup.observe(_call(1, "13%")) is None
    trip = sup.observe(_call(2, "13%"))
    assert trip is not None and trip.reason == "doom_loop"


def test_unchanged_output_trips_at_the_limit_separate_result_event():
    sup = _sup()
    for i in range(2):
        assert sup.observe(_call(i)) is None
        assert sup.observe(_done(i, "13%")) is None
    trip = sup.observe(_call(2))
    assert trip is not None and trip.reason == "doom_loop"


def test_change_then_stall_counts_from_the_change():
    sup = _sup()
    for i, out in enumerate(["1%", "2%", "3%"]):
        sup.observe(_call(i)); sup.observe(_done(i, out))
    # 3% is new at call 2 (progress); calls 3, 4, 5 then repeat it unchanged.
    for i in (3, 4):
        assert sup.observe(_call(i)) is None
        sup.observe(_done(i, "3%"))
    trip = sup.observe(_call(5))
    assert trip is not None and trip.reason == "doom_loop"


def test_no_result_reported_keeps_signature_only_behaviour():
    sup = _sup()
    assert sup.observe(_call(0)) is None
    assert sup.observe(_call(1)) is None
    trip = sup.observe(_call(2))
    assert trip is not None and trip.reason == "doom_loop"


def test_oscillating_output_still_trips():
    sup = _sup(loop_repeats=4)
    trip = None
    for i in range(12):
        trip = sup.observe(_call(i, "A" if i % 2 == 0 else "B"))
        if trip:
            break
    assert trip is not None and trip.reason == "doom_loop"


def test_oscillating_output_separate_result_event_still_trips():
    sup = _sup(loop_repeats=4)
    trip = None
    for i in range(12):
        trip = sup.observe(_call(i))
        if trip:
            break
        sup.observe(_done(i, "A" if i % 2 == 0 else "B"))
    assert trip is not None and trip.reason == "doom_loop"


def test_seen_set_is_bounded_by_the_window():
    sup = _sup(loop_window=5, loop_repeats=3)
    for i in range(100):
        sup.observe(_call(i, f"r{i}"))
    assert all(len(seen) <= 5 for seen in sup._results.values())


def _test(i: int, result: str = "") -> Event:
    return Event(kind="tool", name="bash", args={"command": "pytest"},
                 tool_id=f"t{i}", result=result)


def _edit(i: int) -> Event:
    return Event(kind="tool", name="bash", args={"command": "sed -i s/a/b/ f"},
                 tool_id=f"e{i}", result=f"ok{i}")


def test_interleaved_cycle_with_new_results_is_not_flagged():
    sup = _sup(loop_repeats=3)
    for i in range(30):
        assert sup.observe(_test(i, f"{i} failed")) is None
        assert sup.observe(_edit(i)) is None


def test_interleaved_cycle_with_identical_results_still_trips():
    sup = _sup(loop_repeats=3)
    trip = None
    for i in range(10):
        trip = sup.observe(_test(i, "3 failed")) or sup.observe(_edit(0))
        if trip:
            break
    assert trip is not None and trip.reason == "doom_loop"


def test_interleaved_progress_reset_finds_the_signature_anywhere():
    # edit -> test (result arrives on a later event) -> edit -> test ...
    sup = _sup(loop_repeats=3)
    for i in range(30):
        assert sup.observe(_edit(0)) is None
        assert sup.observe(_test(i)) is None
        assert sup.observe(Event(kind="step", tool_id=f"t{i}", result=f"{i} failed")) is None


def test_opencode_result_is_a_digest_not_the_output():
    from multiagents.providers import load_providers
    import yaml
    raw = yaml.safe_load((Path(__file__).resolve().parent.parent / "src" / "multiagents"
                          / "defaults" / "providers.yaml").read_text())["providers"]
    prov = load_providers(raw)["opencode"]
    secret = "SECRET-OUTPUT-12345"
    line = json.dumps({"type": "tool_use", "sessionID": "s", "part": {
        "tool": "bash", "state": {"status": "completed", "input": {"command": "x"},
                                  "output": secret}}})
    ev = prov.parse_line(line)
    assert ev.kind == "tool" and ev.result and secret not in ev.result
    assert len(ev.result) == 16
