"""NC-R38: Paris 2026 DST tables through the scheduler's window evaluator.

UTC inputs distinguish the two occurrences of a repeated wall time. Expected
boundaries are instants, not elapsed-duration arithmetic: spring skips 02:xx;
fall visits 02:xx twice. No real clock, scheduler process or private API needed.
"""
import json
from datetime import datetime

import pytest

from multiagents.scheduler.windows import evaluate, prepare


def stamp(value):
    return datetime.fromisoformat(value + "+00:00")


def result(days, interval, instant):
    spec = {"timezone": "Europe/Paris", "days": days, "ranges": [interval]}
    prepare("Europe/Paris", [spec])
    return evaluate(json.dumps([spec]), "Europe/Paris", stamp(instant).timestamp())


# Spring: 00:59:59Z is 01:59:59 CET; 01:00Z is 03:00 CEST.
@pytest.mark.parametrize("interval,expected", [
    ("02:15-02:45", [False, False, False, False, False, False]),
    ("02:30-04:00", [False, False, True, True, True, False]),
    ("01:30-02:30", [False, True, False, False, False, False]),
    ("01:30-04:00", [False, True, True, True, True, False]),
])
def test_nc_r38_spring_gap_membership_table(interval, expected):
    instants = ["2026-03-29T00:29:59", "2026-03-29T00:59:59",
                "2026-03-29T01:00:00", "2026-03-29T01:30:00",
                "2026-03-29T01:59:59", "2026-03-29T02:00:00"]
    for instant, opened in zip(instants, expected):
        assert result(["sun"], interval, instant)["open"] is opened, (interval, instant)


# Fall: 00:xxZ is the first 02:xx (CEST), 01:xxZ the second (CET).
@pytest.mark.parametrize("interval,expected", [
    ("02:30-02:45", [False, True, True, False, False, False, True, True, False, False]),
    ("02:30-04:00", [False, True, True, True, True, False, True, True, True, False]),
    ("01:30-02:30", [True, False, False, False, False, True, False, False, False, False]),
    ("01:30-04:00", [True, True, True, True, True, True, True, True, True, False]),
])
def test_nc_r38_fall_repeated_hour_membership_table(interval, expected):
    instants = ["2026-10-25T00:29:59", "2026-10-25T00:30:00",
                "2026-10-25T00:44:59", "2026-10-25T00:45:00",
                "2026-10-25T00:59:59", "2026-10-25T01:00:00",
                "2026-10-25T01:30:00", "2026-10-25T01:44:59",
                "2026-10-25T01:45:00", "2026-10-25T03:00:00"]
    for instant, opened in zip(instants, expected):
        assert result(["sun"], interval, instant)["open"] is opened, (interval, instant)


@pytest.mark.parametrize("interval,instant,opened,next_open,next_close", [
    # An entirely imaginary range waits until the following Sunday.
    ("02:15-02:45", "2026-03-29T00:59:59", False,
     "2026-04-05T00:15:00", "2026-04-05T00:45:00"),
    # Missing start/end points take effect at the jump itself.
    ("02:30-04:00", "2026-03-29T00:59:59", False,
     "2026-03-29T01:00:00", "2026-03-29T02:00:00"),
    ("01:30-02:30", "2026-03-29T00:45:00", True,
     "2026-04-04T23:30:00", "2026-03-29T01:00:00"),
    ("01:30-04:00", "2026-03-29T00:29:59", False,
     "2026-03-29T00:30:00", "2026-03-29T02:00:00"),
    ("02:30-04:00", "2026-03-29T01:00:00", True,
     "2026-04-05T00:30:00", "2026-03-29T02:00:00"),
    # A narrow repeated range opens and closes independently in both folds.
    ("02:30-02:45", "2026-10-25T00:29:59", False,
     "2026-10-25T00:30:00", "2026-10-25T00:45:00"),
    ("02:30-02:45", "2026-10-25T00:30:00", True,
     "2026-10-25T01:30:00", "2026-10-25T00:45:00"),
    ("02:30-02:45", "2026-10-25T00:45:00", False,
     "2026-10-25T01:30:00", "2026-10-25T01:45:00"),
    ("02:30-02:45", "2026-10-25T01:30:00", True,
     "2026-11-01T01:30:00", "2026-10-25T01:45:00"),
    # Rollback itself closes a range starting in 02:xx and reopens one ending there.
    ("02:30-04:00", "2026-10-25T00:45:00", True,
     "2026-10-25T01:30:00", "2026-10-25T01:00:00"),
    ("01:30-02:30", "2026-10-25T00:30:00", False,
     "2026-10-25T01:00:00", "2026-10-25T01:30:00"),
    ("01:30-04:00", "2026-10-25T00:45:00", True,
     "2026-11-01T00:30:00", "2026-10-25T03:00:00"),
])
def test_nc_r38_dst_next_boundary_table(interval, instant, opened, next_open, next_close):
    got = result(["sun"], interval, instant)
    assert got["open"] is opened
    assert datetime.fromisoformat(got["next_open"]) == stamp(next_open)
    assert datetime.fromisoformat(got["next_close"]) == stamp(next_close)
    assert got["empty"] is False


@pytest.mark.parametrize("instants,next_open,next_close", [
    (["2026-03-28T20:59:59", "2026-03-28T21:00:00", "2026-03-28T23:00:00",
      "2026-03-29T00:59:59", "2026-03-29T01:00:00", "2026-03-29T03:59:59",
      "2026-03-29T04:00:00"], "2026-03-28T21:00:00", "2026-03-29T04:00:00"),
    (["2026-10-24T19:59:59", "2026-10-24T20:00:00", "2026-10-24T22:00:00",
      "2026-10-25T00:59:59", "2026-10-25T01:00:00", "2026-10-25T04:59:59",
      "2026-10-25T05:00:00"], "2026-10-24T20:00:00", "2026-10-25T05:00:00"),
])
def test_nc_r38_midnight_crossing_transition_night_table(instants, next_open, next_close):
    for instant, opened in zip(instants, [False, True, True, True, True, True, False]):
        got = result(["sat"], "22:00-06:00", instant)
        assert got["open"] is opened, instant
        if instant == instants[0]:
            assert datetime.fromisoformat(got["next_open"]) == stamp(next_open)
        if instant != instants[-1]:
            assert datetime.fromisoformat(got["next_close"]) == stamp(next_close)
