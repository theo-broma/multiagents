"""NT-R5 (rate limit and grouping), ntfy notifications part 2,
context/specs/ntfy-notifications.md. Driven by the scheduler's fake clock.

Reading used here (see the report's silences): the activation summary is itself
a message the scheduler publishes, so it opens the first interval.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_part2_harness import INTERVAL, wait_until  # noqa: E402
from test_nt_part2_events import fake, make, message_after  # noqa: E402,F401


def test_nt_r5_at_most_one_message_per_interval_and_the_rest_wait_for_the_next(make):
    w = make()
    n = w.first_message()
    q1 = w.question()
    w.advance(seconds=INTERVAL - 2)
    w.quiet()
    assert len(w.fake.requests) == n, "a second message went out inside the interval"
    w.advance(seconds=3)
    assert message_after(w, n, q1) is not None, "the event never went out once the interval elapsed"
    sent = len(w.fake.requests)
    q2 = w.question()
    w.advance(seconds=100)
    w.quiet()
    assert len(w.fake.requests) == sent, "a message went out inside the second interval"
    w.advance(seconds=INTERVAL)
    assert message_after(w, sent, q2) is not None
    assert not any(q1 in r.text() for r in w.fake.requests[sent:]), "an event was announced twice"


@pytest.mark.parametrize("count", [1, 2, 10, 11, 12])
def test_nt_r5_a_burst_is_one_message_of_at_most_ten_lines_and_plus_n_more(make, count):
    w = make()
    n = w.first_message()
    ids = [w.question() for _ in range(count)]
    w.release()
    assert w.got(n + 1), "the burst was never sent"
    w.quiet()
    assert len(w.fake.requests) == n + 1, "the burst was not grouped into one message"
    body = w.bodies(n)[0]
    listed = [i for i in ids if i in body]
    assert listed == ids[:min(count, 10)], "not the first ten, in the order they happened"
    positions = [body.index(i) for i in listed]
    assert positions == sorted(positions)
    more = re.search(r"\+\s*(\d+)\s+more", body)
    if count > 10:
        assert more and int(more.group(1)) == count - 10, body
    else:
        assert more is None, "a '+N more' with nothing left over"


def test_nt_r5_the_group_takes_the_highest_priority_and_lists_events_in_order(make):
    w = make()
    n = w.first_message()
    finished = w.done_node()                 # default priority, happens first
    q = w.question()                         # high priority, happens second
    w.release()
    req = message_after(w, n, q)
    assert req is not None and finished in req.text()
    assert req.text().index(finished) < req.text().index(q)
    assert req.headers["priority"] in ("high", "4")
    assert len([r for r in w.fake.requests[n:] if q in r.text()]) == 1


def test_nt_r5_a_group_of_default_events_stays_default(make):
    w = make()
    n = w.first_message()
    first = w.done_node("D1")
    second = w.done_node("D2")
    w.release()
    req = message_after(w, n, second)
    assert req is not None and first in req.text()
    assert req.headers.get("priority", "default") in ("default", "3")


def test_nt_r5_the_notify_test_command_is_not_held_back_by_the_scheduler_interval(make):
    w = make()
    n = w.first_message()                    # the interval is open, no time has passed
    done = w.cli("notify", "test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(w.fake.requests) == n + 1
