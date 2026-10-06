"""NT-R6 (sending never disturbs scheduling, and the status fields) and the
token half of NT-R7, ntfy notifications part 2, context/specs/ntfy-notifications.md.

The fake server hangs or fails; the scheduler's ticks, nodes, runs and
transitions must read exactly as they do without any notify section. Where the
contract names a quantity but not its key, the status helpers look a key up by
a word it must contain (`pending`, `last`/`accept`, `fail`/`error`).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nt_part2_harness import (HOUR, INTERVAL, TOKEN, TOPIC, NtWorld, field, leaks_of,  # noqa: E402
                              process_output, token_file, wait_until)
from test_nt_part2_events import fake, make, message_after  # noqa: E402,F401

TICK_BOUND = 2.0          # the host tick is 0.2 s; a hung server may not stretch a tick to this
RPC_BOUND = 2.0
from multiagents.paths import state_root  # noqa: E402


def tick_sees(w, instant, bound=TICK_BOUND):
    w.write_clock(instant)
    return wait_until(lambda: w._names(json.dumps(w.status().get("last_tick"), default=str), instant),
                      bound, 0.05)


def signature(w):
    """What the scheduler itself recorded, normalised across runs: per node (in
    creation order) its state, outcome and the kinds of its transitions."""
    rows = []
    transitions = w.rpc("wait_for_nodes", {"timeout": 0, "cursor": 0})["result"]["transitions"]
    for node in sorted(w.list(), key=lambda n: n["created_at"]):
        kinds = [t["kind"] for t in transitions if t["node_id"] == node["id"]]
        rows.append((node["kind"], node["state"], node.get("outcome"), kinds))
    return rows


def scenario(w):
    w.start_scheduler()
    w.question()
    w.done_node("S1")
    crashed = w.simple("S2", "worker", fx={"crash": True})
    w.until(lambda: w.get(crashed)["state"] == "done", timeout=15, what="S2 finished")
    w.held_node()
    w.release()
    w.release()
    w.quiet(0.5)
    return signature(w)


# ------------------------------------------------------------------ hanging
def test_nt_r6_a_hanging_server_does_not_delay_a_tick(make):
    w = make()
    w.fake.mode = "hang"
    w.seeded = [w.question()]
    w.start_scheduler()
    assert w.got(1), "the scheduler never tried to send"      # it is now stuck inside the hang
    t0 = time.monotonic()
    for k in range(1, 4):
        assert tick_sees(w, w.now + timedelta(seconds=7 * k)), f"tick {k} did not see the clock within {TICK_BOUND}s"
    started = time.monotonic()
    w.status()
    assert time.monotonic() - started < RPC_BOUND, "scheduler_status stalled behind a hanging send"
    w.quiet(0.1)
    assert time.monotonic() - t0 < 30


def test_nt_r6_a_hanging_server_does_not_slow_a_node_down_or_block_a_write(make):
    w = make()
    w.fake.mode = "hang"
    w.seeded = [w.question()]
    w.start_scheduler()
    assert w.got(1)
    started = time.monotonic()
    node = w.done_node("T1")                 # create, launch, run, finish, all while the send hangs
    held = w.held_node()
    assert w.get(node)["outcome"] == "completed" and w.get(held)["state"] == "held"
    assert time.monotonic() - started < 25


@pytest.mark.parametrize("mode,status", [("hang", 0), ("status", 500), ("drop", 0), ("status", 401)])
def test_nt_r6_a_slow_or_failing_server_changes_no_node_run_or_transition(tmp_path, monkeypatch, mode, status):
    from nt_part2_harness import FakeNtfy
    (tmp_path / "plain").mkdir()
    (tmp_path / "sick").mkdir()
    plain = NtWorld(tmp_path / "plain", monkeypatch, FakeNtfy(), notify=None)
    try:
        baseline = scenario(plain)
    finally:
        plain.close()
        plain.fake.close()
    sick_fake = FakeNtfy()
    sick_fake.mode, sick_fake.status = mode, status
    sick = NtWorld(tmp_path / "sick", monkeypatch, sick_fake)
    try:
        observed = scenario(sick)
        assert len(sick_fake.requests) >= 1, "the scheduler never tried to send: the comparison proves nothing"
    finally:
        sick_fake.release.set()
        sick.close()
        sick_fake.close()
    assert observed == baseline


# ------------------------------------------------------------- status fields
def test_nt_r6_scheduler_status_shows_pending_last_accepted_and_the_current_failure(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    status = w.notify_status()
    assert field(status, "pending") >= 1
    assert not field(status, "accept", "last") or str(field(status, "accept", "last")).lower() in ("never", "none")
    assert "503" in str(field(status, "fail", "error")), status
    w.fake.mode = "accept"
    w.advance(seconds=HOUR)
    assert w.got(n + 1)
    assert wait_until(lambda: field(w.notify_status(), "pending") == 0, 3.0)
    status = w.notify_status()
    assert field(status, "accept", "last"), "no time of the last accepted message"
    assert not field(status, "fail", "error"), f"a failure still shown after recovery: {status}"


def test_nt_r6_notify_status_prints_the_same_fields(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    w.first_message()
    out = w.cli("notify", "status")
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert re.search(r"pending\D*[1-9]", text), text
    assert re.search(r"last", text, re.I), text
    assert "503" in text, text
    w.fake.mode = "accept"
    w.advance(seconds=HOUR)
    assert wait_until(lambda: re.search(r"pending\D*0\b", w.cli("notify", "status").stdout), 4.0)
    text = w.cli("notify", "status").stdout
    assert "503" not in text, "the failure is still shown after the server accepted"


def log_lines_about_the_failure(w):
    text = process_output(w)
    logs = list((state_root()).rglob("*.log")) + list(w.tmp_path.rglob("*.log"))
    for path in logs:
        try:
            text += "\n" + path.read_text(errors="replace")
        except OSError:
            pass
    # The `scheduler start` wrapper prints the scheduler_status JSON; that is not a log line.
    return [ln for ln in text.splitlines()
            if not ln.lstrip().startswith("{")
            and re.search(r"ntfy|notif", ln, re.I) and re.search(r"fail|error|50\d|unreach|refus", ln, re.I)
            and not re.search(r"recover|resum|restor", ln, re.I)]


def test_nt_r6_a_failure_is_logged_once_per_outage_per_process(make):
    w = make()
    w.fake.mode, w.fake.status = "status", 503
    n = w.first_message()
    for k in range(1, 6):                    # five retries, all failing
        w.advance(seconds=600)
        assert w.got(n + k, 3.0)
    w.fake.mode = "accept"
    w.advance(seconds=600)
    assert wait_until(lambda: field(w.notify_status(), "pending") == 0, 3.0)
    w.fake.mode, w.fake.status = "status", 502   # a second, separate outage
    w.question()
    w.release()
    m = len(w.fake.requests)
    for k in range(1, 4):
        w.advance(seconds=600)
        assert w.got(m + k, 3.0)
    w.stop_scheduler()
    lines = log_lines_about_the_failure(w)
    assert len(lines) == 2, f"expected one log line per outage (2), got {len(lines)}: {lines}"


# --------------------------------------------------------------------- token
def test_nt_r7_the_token_appears_in_no_log_status_or_output_whatever_goes_wrong(make, tmp_path):
    tf = token_file(tmp_path)
    w = make(notify={"ntfy_url": "http://placeholder.invalid", "topic": TOPIC})
    w.set_notify({"ntfy_url": w.fake.url, "topic": TOPIC, "token_file": str(tf), "min_interval_seconds": INTERVAL})
    outputs = []
    n = w.first_message()
    assert w.fake.requests[0].headers["authorization"] == f"Bearer {TOKEN}"     # it is really in use
    for mode, status in [("status", 503), ("hang", 0), ("status", 401), ("drop", 0)]:
        w.fake.mode, w.fake.status = mode, status
        w.question()
        w.release()
        outputs.append(json.dumps(w.status(), default=str))
        for words in (("notify", "status"), ("notify", "test")):
            done = w.cli(*words)
            outputs.append(done.stdout + done.stderr)
    w.fake.release.set()
    w.fake.mode = "accept"
    w.stop_scheduler()
    outputs.append(process_output(w))
    for req in w.fake.requests:
        assert TOKEN not in req.text() and TOKEN not in (req.headers.get("title") or "")
        assert TOKEN not in req.path
    assert not any(TOKEN in o for o in outputs), "the token reached a status or CLI output"
    assert leaks_of(TOKEN, tmp_path, state_root(), skip=(tf,)) == [], "the token reached a file (log, outbox, store)"
