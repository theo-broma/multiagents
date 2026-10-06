"""NT-R3 (scheduler events), ntfy notifications part 2, context/specs/ntfy-notifications.md.

A real host scheduler process on a fake clock publishes to a fake ntfy server
(tests/support/nt_part2_harness.py). The rate limit (NT-R5) gates every message
after the first one, so each test lets the first message (the activation
summary, over one seeded open question) go, raises its event, and then moves the
clock one interval so that the event's turn comes. Nothing sleeps on wall time
except short, bounded polls for the fake server's request list.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_part2_harness import (INTERVAL, QID, QUIET, TOPIC, FakeNtfy, NtWorld,  # noqa: E402
                              wait_until)

MARKER_FILE = "TRANSCRIPT-LIKE_assistant_said_rm-rf.txt"
SECRET_TEXT = "TRANSCRIPT-LIKE: assistant> I will now rm -rf the build; user> yes"


@pytest.fixture
def fake():
    f = FakeNtfy()
    yield f
    f.close()


@pytest.fixture
def make(tmp_path, monkeypatch, fake):
    made = []

    def build(**kw):
        w = NtWorld(tmp_path, monkeypatch, fake, **kw)
        made.append(w)
        return w

    yield build
    fake.release.set()
    for w in made:
        w.close()


def message_after(world, since, containing, timeout=4.0):
    """The first request after index `since` whose body holds `containing`."""
    found = []

    def probe():
        found[:] = [r for r in world.fake.requests[since:] if containing in r.text()]
        return found

    wait_until(probe, timeout)
    return found[0] if found else None


# ------------------------------------------------------------------ one per kind
def test_nt_r3_question_event_names_the_question_id_and_is_high_priority(make):
    w = make()
    n = w.first_message()
    qid = w.question(topic="which db", text=SECRET_TEXT)
    w.release()
    req = message_after(w, n, qid)
    assert req is not None, "no notification carried the question id"
    assert req.headers["priority"] in ("high", "4")
    assert "question" in (req.headers["title"] + req.text()).lower()
    assert SECRET_TEXT not in req.text() and "which db" not in req.text()
    assert req.path == "/" + TOPIC


def test_nt_r3_held_event_names_the_node_and_the_hold_reason_and_is_high_priority(make):
    w = make()
    n = w.first_message()
    node = w.held_node()
    w.release()
    req = message_after(w, n, node)
    assert req is not None, "no notification carried the held node's id"
    assert req.headers["priority"] in ("high", "4")
    assert "held" in (req.headers["title"] + req.text()).lower()
    assert "input_conflict" in req.text()


def test_nt_r3_anomaly_event_names_the_node_and_the_anomaly_kind_and_is_default_priority(make):
    w = make()
    n = w.first_message()
    blocked = w.blocked_pair()
    w.advance(seconds=60)                    # past anomaly_admission_seconds
    w.release()
    req = message_after(w, n, blocked)
    assert req is not None, "no notification carried the blocked node's id"
    assert req.headers.get("priority", "default") in ("default", "3")
    assert "anomaly" in (req.headers["title"] + req.text()).lower()
    assert "admission_blocked" in req.text()


def test_nt_r3_done_event_names_the_node_and_its_outcome_and_is_default_priority(make):
    w = make()
    n = w.first_message()
    node = w.done_node()
    outcome = w.get(node)["outcome"]
    w.release()
    req = message_after(w, n, node)
    assert req is not None, "no notification carried the finished node's id"
    assert req.headers.get("priority", "default") in ("default", "3")
    assert "done" in (req.headers["title"] + req.text()).lower()
    assert outcome in req.text()


def test_nt_r3_done_is_sent_whatever_the_outcome(make):
    w = make()
    n = w.first_message()
    node = w.simple("F", "worker", fx={"crash": True})      # the run dies: the node finishes `failed`
    w.until(lambda: w.get(node)["state"] == "done", timeout=15, what="the node finished")
    outcome = w.get(node)["outcome"]
    assert outcome != "completed"
    w.release()
    req = message_after(w, n, node)
    assert req is not None, "no notification for a node that finished with another outcome"
    assert outcome in req.text()


def test_nt_r3_a_second_hold_of_the_same_node_is_a_new_event_and_one_hold_is_one_event(make):
    w = make()
    n = w.first_message()
    node = w.held_node()
    w.release()
    assert message_after(w, n, node) is not None
    seen = len(w.fake.requests)
    for _ in range(3):                       # still held, many intervals later: nothing new
        w.release()
    w.quiet()
    assert [r for r in w.fake.requests[seen:] if node in r.text()] == []
    rev = w.get(node)["revision"]
    assert w.relaunch(node).get("ok")
    w.until(lambda: w.get(node)["state"] == "held" and w.get(node)["revision"] >= rev + 2,
            timeout=15, what="the node held a second time")
    w.release()
    assert message_after(w, seen, node) is not None, "a later hold of the same node sent nothing"


# --------------------------------------------------------------- events: filter
@pytest.mark.parametrize("listed", ["question", "held", "anomaly", "done"])
def test_nt_r3_only_the_listed_kinds_send(make, listed):
    w = make(events=[listed])
    ids = {}
    w.start_scheduler()
    assert w.got(1), "no activation summary"      # events before activation belong to the summary
    ids["question"] = w.question()
    ids["held"] = w.held_node()
    ids["done"] = w.done_node()
    ids["anomaly"] = w.blocked_pair()
    w.advance(seconds=60)
    for _ in range(2):
        w.release()
    assert wait_until(lambda: any(ids[listed] in b for b in w.bodies()), 4.0), \
        f"the listed kind {listed} sent nothing"
    w.quiet()
    for kind, ident in ids.items():
        if kind != listed:
            assert not any(ident in b for b in w.bodies()), f"unlisted kind {kind} was sent"


# ------------------------------------------------------------------- staleness
def test_nt_r3_a_question_answered_before_its_turn_is_dropped(make):
    w = make()
    n = w.first_message()
    gone = w.question()
    w.answer(gone)
    kept = w.question()
    w.release()
    assert message_after(w, n, kept) is not None
    assert not any(gone in b for b in w.bodies(n)), "an answered question was still announced"


def test_nt_r3_a_node_that_left_held_before_its_turn_is_dropped(make):
    w = make(events=["held", "question"])
    n = w.first_message()
    node = w.held_node()
    assert w.cancel(node).get("ok")
    assert w.get(node)["state"] != "held"
    kept = w.question()
    w.release()
    assert message_after(w, n, kept) is not None
    assert not any(node in b for b in w.bodies(n)), "a node no longer held was still announced"


# ------------------------------------------------------------ activation summary
def test_nt_r3_activation_sends_one_summary_of_counts_and_no_history(make):
    w = make(notify=None)
    old = w.question(text="old " + SECRET_TEXT)
    w.answer(old)                                    # history: answered, must not be reported
    w.start_scheduler()
    finished = w.done_node()                         # a finished node is history too
    held = w.held_node()
    still_open = [w.question(text="open " + SECRET_TEXT) for _ in range(3)]
    w.stop_scheduler()
    w.set_notify({"ntfy_url": w.fake.url, "topic": TOPIC, "min_interval_seconds": INTERVAL})
    w.start_scheduler()
    assert w.got(1), "no activation summary"
    w.advance(seconds=3 * INTERVAL)
    w.quiet()
    assert len(w.fake.requests) == 1, "the activation sent more than one message"
    body = w.bodies()[0]
    assert "3" in body, "the 3 open questions are not counted"
    assert "1" in body, "the held node is not counted"
    for ident in [old, finished, held, *still_open]:
        assert ident not in body
    assert SECRET_TEXT not in body


def test_nt_r3_a_scheduler_without_a_notify_section_sends_nothing(make):
    w = make(notify=None)
    w.start_scheduler()
    w.question()
    w.held_node()
    w.release()
    w.quiet()
    assert w.fake.requests == []


# ------------------------------------------------------- templates, no free text
def test_nt_r3_a_transition_detail_with_transcript_like_text_never_reaches_the_message(make):
    w = make()
    n = w.first_message()
    node = w.held_node(filename=MARKER_FILE)
    # control: the free text really is in the scheduler's own record of the hold
    assert MARKER_FILE in str(w.get(node)["hold"]) or MARKER_FILE in str(w.rpc("wait_for_nodes", {"timeout": 0, "cursor": 0})), \
        "the harness did not get free text into the hold's detail"
    w.release()
    assert message_after(w, n, node) is not None
    for req in w.fake.requests:
        blob = req.text() + " ".join(req.headers.values())
        assert "TRANSCRIPT-LIKE" not in blob and "assistant_said" not in blob
