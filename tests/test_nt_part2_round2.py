"""NT part 2, round 2: the fixes decided after review ag-5532e6
(context/specs/ntfy-notifications.md, "Decisions after review ag-5532e6").

Same harness as the rest of NT part 2: a real host scheduler on a fake clock
publishing to a fake ntfy server. Numbering follows the decisions: 1 one
notification per hold, 2 `done` on disposal, 3 one log line per outage,
4 non-finite Retry-After, 5 re-adding `notify:`, 6 a failed pause clear,
7 a quiet tick writes nothing, 8 bounded marks.

Items 7 and 8 read the scheduler's own database (read-only): they are about
what the scheduler stores, and the database is the only place that shows it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_part2_harness import (HOUR, INTERVAL, TOPIC, NtWorld, field,  # noqa: E402
                              wait_until)
from test_nt_part2_events import fake, make, message_after  # noqa: E402,F401
from test_nt_part2_isolation import log_lines_about_the_failure  # noqa: E402


def settled(w, count, seconds=1.0):
    w.quiet(seconds)
    return len(w.fake.requests) == count


def plan_db(w) -> Path:
    files = [p for p in w.scheduler_files() if p.name == "plan.sqlite3"]
    assert files, "the scheduler has no database"
    return files[0]


def meta_rows(w) -> list[tuple[str, str]]:
    db = sqlite3.connect(f"{plan_db(w).as_uri()}?mode=ro", uri=True, timeout=2)
    try:
        return [(str(k), str(v)) for k, v in db.execute("SELECT key, value FROM meta")]
    finally:
        db.close()


def mentions(w, ident: str) -> list[tuple[str, str]]:
    return [(k, v) for k, v in meta_rows(w) if ident in k or ident in v]


# ---------------------------------------------------- 1. one notification per hold
@pytest.mark.parametrize("edit", [{"urgent": True}, {"task": "an edited task"}])
def test_nt_r2fix_1_editing_a_node_that_stays_held_announces_nothing_new(make, edit):
    w = make()
    n = w.first_message()
    node = w.held_node()
    w.release()
    assert message_after(w, n, node) is not None, "the hold was never announced"
    seen = len(w.fake.requests)
    rev = w.get(node)["revision"]
    reply = w.rpc("update_node", {"id": node, "revision": rev, **edit})
    assert reply.get("ok"), reply
    assert w.get(node)["revision"] > rev and w.get(node)["state"] == "held"
    for _ in range(3):
        w.release()
    assert settled(w, seen), "an edit of a still-held node sent a second 'held' notification"


# --------------------------------------------------------- 2. done on disposal
def test_nt_r2fix_2_a_top_level_node_cancelled_by_disposal_sends_done(make):
    w = make(events=["done"])
    n = w.first_message()
    node = w.held_node()
    rev = w.get(node)["revision"]
    reply = w.rpc("dispose_node", {"id": node, "revision": rev})
    assert reply.get("ok"), reply
    w.until(lambda: w.get(node)["state"] == "cancelled", timeout=15, what="the disposal finished")
    w.release()
    req = message_after(w, n, node)
    assert req is not None, "disposing a top-level node sent no `done`"
    assert "done" in (req.headers["title"] + req.text()).lower()
    assert "cancelled" in req.text()


def test_nt_r2fix_2_disposing_an_already_finished_node_announces_nothing_more(make):
    w = make(events=["done"])
    n = w.first_message()
    node = w.done_node()
    w.release()
    assert message_after(w, n, node) is not None
    seen = len(w.fake.requests)
    rev = w.get(node)["revision"]
    assert w.rpc("dispose_node", {"id": node, "revision": rev}).get("ok")
    w.release()
    assert settled(w, seen), "a node announced as done was announced again by its disposal"


# ----------------------------------------------------- 3. one log line per outage
def test_nt_r2fix_3_an_outage_with_changing_reasons_logs_one_line(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    for mode, status in [("status", 502), ("drop", 0), ("status", 503), ("status", 502)]:
        w.fake.mode, w.fake.status = mode, status
        w.advance(seconds=600)
        n += 1
        assert w.got(n, 3.0), f"no retry under {mode} {status}"
    w.fake.close()                           # now nothing listens: connection refused
    for _ in range(3):
        w.advance(seconds=600)
        w.quiet(0.4)
    w.stop_scheduler()
    lines = log_lines_about_the_failure(w)
    assert len(lines) == 1, f"expected one log line for the whole outage, got {len(lines)}: {lines}"


def test_nt_r2fix_3_a_new_outage_after_an_accepted_send_logs_again(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    w.fake.mode = "accept"
    w.advance(seconds=600)
    assert wait_until(lambda: field(w.notify_status(), "pending") == 0, 3.0)
    w.fake.mode, w.fake.status = "drop", 0
    w.question()
    w.release()
    m = len(w.fake.requests)
    w.fake.mode, w.fake.status = "status", 502
    w.advance(seconds=600)
    assert w.got(m + 1, 3.0)
    w.stop_scheduler()
    assert len(log_lines_about_the_failure(w)) == 2


# ------------------------------------------------------------ 4. Retry-After
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity", "1e999", "soon"])
def test_nt_r2fix_4_a_non_finite_or_unparsable_retry_after_is_treated_as_absent(make, value):
    w = make()
    w.fake.mode, w.fake.status = "status", 429
    w.fake.extra_headers = {"Retry-After": value}
    n = w.first_message()
    w.advance(seconds=29)
    assert settled(w, n, 0.6), "retried before the 30 s backoff"
    w.advance(seconds=2)
    assert w.got(n + 1, 3.0), f"Retry-After: {value} stopped or delayed the normal backoff"
    assert settled(w, n + 1, 0.8), "a tight retry loop: another attempt with no time passing"
    w.fake.mode = "accept"
    w.advance(seconds=61)
    assert w.got(n + 2, 3.0), "the second normal backoff step (60 s) did not retry"


def test_nt_r2fix_4_a_finite_retry_after_is_still_honoured(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 429
    w.fake.extra_headers = {"Retry-After": "120"}
    n = w.first_message()
    w.advance(seconds=60)
    assert settled(w, n, 0.6), "Retry-After: 120 was not honoured"
    w.advance(seconds=61)
    assert w.got(n + 1, 3.0)


# -------------------------------------------------------- 5. re-adding notify:
def test_nt_r2fix_5_removing_and_re_adding_notify_sends_a_summary_not_the_history(make):
    w = make()
    n = w.first_message()
    w.stop_scheduler()
    w.set_notify(None)
    w.start_scheduler()
    q = w.question()
    held = w.held_node()
    done = w.done_node()
    w.advance(seconds=HOUR)
    assert settled(w, n, 0.5), "a scheduler without notify: sent something"
    w.stop_scheduler()
    w.set_notify({"ntfy_url": w.fake.url, "topic": TOPIC, "min_interval_seconds": INTERVAL})
    w.start_scheduler()
    assert w.got(n + 1), "re-adding notify: sent no activation summary"
    for _ in range(3):
        w.release()
    assert settled(w, n + 1), "more than the one summary was sent"
    summary = w.bodies(n)[0]
    assert "2" in summary and "1" in summary, f"the summary lacks the counts (2 questions, 1 held): {summary!r}"
    for ident in (q, held, done):
        assert not any(ident in b for b in w.bodies(n)), f"{ident} was announced one by one"


# ------------------------------------------------- 6. a failed pause clear
def test_nt_r2fix_6_notify_test_that_cannot_clear_the_pause_fails_and_says_so(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 401
    w.first_message()
    w.advance(seconds=HOUR)
    w.fake.mode = "accept"
    db = plan_db(w)
    mode = db.stat().st_mode
    os.chmod(db, 0o444)                      # the store refuses writes from the CLI
    try:
        done = w.cli("notify", "test")
    finally:
        os.chmod(db, mode & 0o7777)
    text = done.stdout + done.stderr
    assert done.returncode != 0, f"notify test reported success though the pause stayed: {text!r}"
    assert "pause" in text.lower(), f"the failure does not mention the pause: {text!r}"


def test_nt_r2fix_6_notify_test_still_succeeds_when_there_is_no_pause_to_clear(make):
    w = make()
    w.first_message()
    done = w.cli("notify", "test")
    assert done.returncode == 0, done.stdout + done.stderr


# ---------------------------------------------------- 7. a quiet tick writes nothing
def db_signature(w):
    base = plan_db(w)
    sig = []
    for suffix in ("", "-wal"):
        p = Path(str(base) + suffix)
        sig.append((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)
    return sig


@pytest.mark.parametrize("notify", ["absent", "present"])
def test_nt_r2fix_7_a_quiet_tick_writes_nothing(make, notify):
    if notify == "absent":
        w = make(notify=None)
        w.start_scheduler()
    else:
        w = make()
        w.first_message()
    w.until(lambda: len(w.scheduler_files()) > 0, timeout=5, what="the scheduler's files")
    w.advance(seconds=20)                    # let any start-up write settle
    w.quiet(0.8)
    before = db_signature(w)
    for _ in range(4):
        w.advance(seconds=10)
        w.quiet(0.4)
    assert db_signature(w) == before, "ticks with nothing to report wrote to the scheduler database"


# --------------------------------------------------------- 8. bounded marks
def test_nt_r2fix_8_the_marks_of_a_closed_question_are_gone(make):
    w = make()
    n = w.first_message()
    q = w.question()
    w.release()
    assert message_after(w, n, q) is not None
    w.answer(q)
    seed = w.seeded[0]
    w.answer(seed)
    w.advance(seconds=INTERVAL)
    assert wait_until(lambda: not mentions(w, q) and not mentions(w, seed), 4.0), \
        f"marks of closed questions remain: {mentions(w, q) + mentions(w, seed)}"


def test_nt_r2fix_8_the_marks_of_a_terminal_node_are_gone(make):
    w = make()
    n = w.first_message()
    node = w.held_node()
    w.release()
    assert message_after(w, n, node) is not None
    assert w.cancel(node).get("ok")
    w.until(lambda: w.get(node)["state"] == "cancelled", timeout=15, what="the node cancelled")
    w.advance(seconds=INTERVAL)
    assert wait_until(lambda: not mentions(w, node), 4.0), f"marks of a terminal node remain: {mentions(w, node)}"
