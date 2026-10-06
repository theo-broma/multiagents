"""AN round 2: held_idle ages are measured on ONE clock (the scheduler's).

Regression tests for the two blocking defects reviewer ag-98e524 found in
src/multiagents/scheduler/engine.py:

* a transition laid down between two checks was attributed to the PREVIOUS
  check's time, so `held_idle` (and the other age-based kinds) could fire up
  to `anomaly_interval_seconds` early;
* `_anomaly_map_time` added a real-time difference to the scheduler clock,
  so when the scheduler clock did not advance at real-time rate (the
  injected test clock, or after a restart) ages came out huge and anomalies
  fired at once.

Both are black box like tests/test_an_scheduler_anomalies.py: the harness
below (Env, INTERVAL, THRESHOLD) is imported from that module, and the only
surface under test is the scheduler's transitions as `wait_for_nodes`
delivers them. `time.time` is faked only around the engine restart in the
second test, and restored through the same monkeypatch fixture.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import test_an_scheduler_anomalies as an  # noqa: E402
from multiagents.scheduler.engine import Engine  # noqa: E402

INTERVAL = an.INTERVAL
THRESHOLD = an.THRESHOLD


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = an.Env(tmp_path, monkeypatch)
    yield e
    e.close()


def test_round2_a_hold_just_before_a_check_is_not_reported_early(env):
    """A hold laid down 1 s before a check is aged from the hold (AN-R2).

    Old code attributed the new transition to the previous check and fired
    up to one interval early (here at scheduler time 70 for a hold laid
    down at 19 with a threshold of 60). The fixed check reports nothing
    until the threshold has truly passed on the scheduler clock.
    """
    node = env.h.record()
    env.h.save(node)
    env.hold(node)                       # an early episode, superseded below
    env.at_offset(0)
    for step in range(1, 20):
        env.at_offset(step)              # ticks without checks move the clock on
    env.hold(env.h.nodes()[node["id"]])  # the episode under test, at time 19
    env.at_offset(20)                    # the check 1 s later: age 1
    assert env.anomalies("held_idle") == []
    env.at_offset(70)                    # true age 51: still nothing
    assert env.anomalies("held_idle") == []
    env.at_offset(80)                    # true age 61
    assert [t["node_id"] for t in env.anomalies("held_idle")] == [node["id"]]


def test_round2_a_restart_after_real_time_jumped_fires_nothing(env, tmp_path, monkeypatch):
    """A restart must not turn a real-time jump into scheduler age (AN-R2).

    The hold predates every check, real time jumps far ahead, and the
    scheduler clock barely moves. Old code mapped the hold's real-time
    stamp onto the new engine's timeline and fired at once; the fixed
    check falls back to first observation and stays quiet until the
    threshold has truly passed on the scheduler clock.
    """
    node = env.h.record()
    env.h.save(node)
    env.hold(env.h.nodes()[node["id"]])  # laid down before any check ran
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 100000)
    env.engine.loop.close()
    env.engine = Engine(env.service, clock_file=env.clock_file)    # scheduler restart
    env.service.engine = env.engine
    env.h.engine = env.engine
    monkeypatch.setattr(time, "time", real_time)
    env.at_offset(5)                     # scheduler clock barely moved
    assert env.anomalies("held_idle") == []
    env.at_offset(15)
    assert env.anomalies("held_idle") == []
    env.at_offset(65)                    # 60 s since first observation: due
    assert [t["node_id"] for t in env.anomalies("held_idle")] == [node["id"]]
