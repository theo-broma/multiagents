"""NT-R4 (durability and retries, at least once) and the pause clearing of
NT-R8, ntfy notifications part 2, context/specs/ntfy-notifications.md.

Real host scheduler on a fake clock (tests/support/nt_part2_harness.py). The
"server down" modes are the fake server's: `drop` (network error), a status code
(HTTP failure) or `hang` (it read the request and never answers). The outbox is
observed only through what the fake server receives, `scheduler_status`, and the
CLI. Retry timing is read off the scheduler's injected clock: the retry delays
are 30 s growing to 10 min (NT-R4), so "29 s after a failure: nothing; 31 s: a retry".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_part2_harness import (HOUR, INTERVAL, TOPIC, FakeNtfy, NtWorld, field,  # noqa: E402
                              wait_until)
from test_nt_part2_events import message_after  # noqa: E402,F401  (also re-exports the fixtures)
from test_nt_part2_events import fake, make  # noqa: E402,F401


def settled(w, count, seconds=1.0):
    """No request beyond `count` shows up in `seconds`."""
    w.quiet(seconds)
    return len(w.fake.requests) == count


def pending_of(w):
    return field(w.notify_status(), "pending")


# ------------------------------------------------------------------ restarts
def test_nt_r4_a_restart_after_an_accepted_send_sends_nothing_again(make):
    w = make()
    n = w.first_message()
    w.stop_scheduler()
    w.start_scheduler()
    w.advance(seconds=3 * INTERVAL)
    assert settled(w, n), "a restart re-sent an accepted message, or re-announced an unchanged destination"


def test_nt_r4_the_outbox_survives_a_restart(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    first_body = w.bodies()[0]
    assert pending_of(w) >= 1
    w.stop_scheduler()
    w.fake.mode = "accept"
    w.start_scheduler()
    w.advance(seconds=HOUR)
    assert w.got(n + 1), "the pending message was lost by the restart"
    assert w.bodies()[-1] == first_body
    assert wait_until(lambda: pending_of(w) == 0, 3.0)


def test_nt_r4_a_crash_after_the_server_accepted_resends_instead_of_losing(make):
    w = make()
    w.fake.mode = "hang"                     # the server has the request; the scheduler never hears back
    w.first_message()
    first_body = w.bodies()[0]
    w.kill9()
    w.fake.mode = "accept"
    w.fake.release.set()
    w.start_scheduler()
    w.advance(seconds=HOUR)
    assert wait_until(lambda: len(w.fake.requests) >= 2, 4.0), "the message in flight at the crash was lost"
    assert w.bodies()[1] == first_body


# --------------------------------------------------------------- retry, backoff
@pytest.mark.parametrize("mode,status", [("drop", 0), ("status", 500), ("status", 503), ("status", 429)])
def test_nt_r4_a_failure_that_may_pass_is_retried_after_thirty_seconds_and_then_delivered(make, mode, status):
    w = make()
    w.fake.mode, w.fake.status = mode, status
    n = w.first_message()
    w.advance(seconds=29)
    assert settled(w, n), "retried before the 30 s backoff"
    w.fake.mode = "accept"
    w.advance(seconds=2)
    assert w.got(n + 1), "not retried after the 30 s backoff"
    assert wait_until(lambda: pending_of(w) == 0, 3.0)


def test_nt_r4_the_backoff_grows_and_is_capped_at_ten_minutes(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    w.advance(seconds=31)
    assert w.got(n + 1), "no retry after 30 s"
    w.advance(seconds=30)
    assert settled(w, n + 1), "the second delay is not longer than the first"
    for k in range(2, 9):                    # delays 60, 120, ... capped: a 10-minute step always retries
        w.advance(seconds=600)
        assert w.got(n + k, 3.0), f"no retry within 10 minutes of failure {k}"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_nt_r4_a_client_error_is_not_retried_and_the_message_stays_queued(make, status):
    w = make()
    w.fake.mode, w.fake.status = "status", status
    n = w.first_message()
    for _ in range(3):
        w.advance(seconds=HOUR)
    assert settled(w, n), f"HTTP {status} was retried"
    assert pending_of(w) >= 1, "the unsent message was dropped"


def test_nt_r4_a_401_pauses_sending_until_notify_test_succeeds(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    n = w.first_message()
    first_body = w.bodies()[0]
    w.advance(seconds=HOUR)
    w.fake.mode = "accept"                  # the server is fine again, but the pause stands
    w.advance(seconds=HOUR)
    assert settled(w, n), "sending resumed without anything clearing the pause"
    done = w.cli("notify", "test")
    assert done.returncode == 0, done.stdout + done.stderr
    sent = len(w.fake.requests)             # the test message itself is among them
    assert sent == n + 1
    w.advance(seconds=INTERVAL + 1)
    assert wait_until(lambda: first_body in w.bodies(sent), 4.0), \
        "the queued message was not sent after notify test cleared the pause"
    assert wait_until(lambda: pending_of(w) == 0, 3.0)


def test_nt_r8_a_failing_notify_test_does_not_clear_the_pause(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    n = w.first_message()
    assert w.cli("notify", "test").returncode != 0
    n = len(w.fake.requests)
    w.fake.mode = "accept"
    w.advance(seconds=HOUR)
    assert settled(w, n), "a failed notify test cleared the pause"


def test_nt_r4_the_pause_survives_a_scheduler_restart(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    n = w.first_message()
    w.stop_scheduler()
    w.fake.mode = "accept"
    w.start_scheduler()
    w.advance(seconds=HOUR)
    assert settled(w, n), "a restart cleared the pause"


# --------------------------------------------------------------------- expiry
EXPIRED = re.compile(r"\b1 notifications? expired", re.I)


def test_nt_r4_a_message_older_than_24_hours_is_dropped_and_counted_in_the_next_one(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    stale_body = w.bodies()[0]
    w.advance(hours=25)
    w.fake.mode = "accept"
    q = w.question()
    w.release()
    req = message_after(w, n, q)
    assert req is not None, "nothing was sent after the outage"
    assert EXPIRED.search(req.text()), f"no '1 notifications expired' in: {req.text()!r}"
    assert stale_body not in w.bodies(n), "the expired message was sent"


def test_nt_r4_a_message_just_under_24_hours_old_is_still_sent_without_an_expiry_note(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    first_body = w.bodies()[0]
    w.advance(hours=23)
    w.fake.mode = "accept"
    w.advance(seconds=700)
    assert wait_until(lambda: first_body in w.bodies(n), 4.0), "a message under 24 h old was not delivered"
    delivered = w.bodies(n)
    assert not any("expired" in b.lower() for b in delivered)


# ----------------------------------------------------------- config changes
def outage_with_pending_event(w):
    """An outage during which one question event is pending; returns its id."""
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    q = w.question()
    w.release()
    assert message_after(w, n, q) is not None, "the event was never attempted"
    return q


def test_nt_r4_removing_the_notify_section_discards_the_outbox(make):
    w = make()
    q = outage_with_pending_event(w)
    w.stop_scheduler()
    w.set_notify(None)
    n = len(w.fake.requests)
    w.start_scheduler()
    w.advance(seconds=HOUR)
    assert settled(w, n), "a scheduler without notify: sent something"
    w.stop_scheduler()
    w.fake.mode = "accept"
    w.set_notify({"ntfy_url": w.fake.url, "topic": TOPIC, "min_interval_seconds": INTERVAL})
    w.start_scheduler()
    w.advance(seconds=HOUR)
    w.advance(seconds=HOUR)
    w.quiet(1.5)
    assert not any(q in b for b in w.bodies(n)), "an outbox discarded with the section came back"


@pytest.mark.parametrize("change", ["topic", "url"])
def test_nt_r4_changing_the_destination_sends_the_pending_messages_to_the_new_one(make, change):
    w = make()
    q = outage_with_pending_event(w)
    old_count = len(w.fake.requests)
    w.stop_scheduler()
    w.fake.mode = "accept"
    other = FakeNtfy() if change == "url" else None
    try:
        target = other or w.fake
        new_topic = TOPIC if change == "url" else "multiagents-other"
        w.set_notify({"ntfy_url": target.url, "topic": new_topic, "min_interval_seconds": INTERVAL})
        w.start_scheduler()
        w.advance(seconds=HOUR)
        w.advance(seconds=HOUR)
        assert wait_until(lambda: any(q in r.text() and r.path == "/" + new_topic
                                      for r in target.requests[(0 if other else old_count):]), 4.0), \
            "the pending event never reached the new destination"
        if other:
            assert len(w.fake.requests) == old_count, "the old server was sent to after the change"
        else:
            assert all(r.path == "/" + new_topic for r in w.fake.requests[old_count:])
    finally:
        if other:
            other.close()


def test_nt_r4_a_changed_destination_clears_a_401_pause(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    n = w.first_message()
    w.stop_scheduler()
    w.fake.mode = "accept"
    w.set_notify({"ntfy_url": w.fake.url, "topic": "multiagents-other", "min_interval_seconds": INTERVAL})
    w.start_scheduler()
    w.advance(seconds=HOUR)
    assert w.got(n + 1), "a config change did not end the pause"
    assert w.fake.requests[n].path == "/multiagents-other"


def test_nt_r3_a_destination_changed_since_the_last_start_sends_a_new_activation_summary(make):
    w = make()
    n = w.first_message()
    w.stop_scheduler()
    w.set_notify({"ntfy_url": w.fake.url, "topic": "multiagents-other", "min_interval_seconds": INTERVAL})
    w.start_scheduler()
    w.advance(seconds=INTERVAL + 1)
    assert w.got(n + 1), "no summary at the new destination"
    assert w.fake.requests[n].path == "/multiagents-other"


# ------------------------------------------------------- a separate cursor
def test_nt_r4_the_orchestrators_ack_does_not_swallow_a_notification(make):
    w = make()
    n = w.first_message()
    node = w.held_node()
    nxt = w.rpc("wait_for_nodes", {"timeout": 0, "cursor": 0})["result"]["next_cursor"]
    assert w.rpc("ack_nodes", {"cursor": nxt}).get("ok")
    w.release()
    assert message_after(w, n, node) is not None


def test_nt_r4_sending_a_notification_does_not_move_the_orchestrators_cursor(make):
    w = make()
    n = w.first_message()
    node = w.held_node()
    w.release()
    assert message_after(w, n, node) is not None
    kinds = [t["kind"] for t in w.rpc("wait_for_nodes", {"timeout": 0})["result"]["transitions"]
             if t["node_id"] == node]
    assert "held" in kinds, "the orchestrator no longer sees a transition the notifier sent"
