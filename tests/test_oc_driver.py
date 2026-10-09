"""OC — the driver honours a requested compaction: OC-R2, R3, R4, R5, R6, R8
and Decisions 3, 5, 7, 9, 10.

Contract: `context/specs/orchestrator-compaction-request.md`. Written red,
before any implementation.

Harness: `Oc` (tests/support/oc_harness.py) over the R8f `Session` — a real child
"CLI" that is a fake provider script, a real transcript, the real
`scripts.exec_action` / `run_action`, the project's tree and events file. The
orchestrator's request is made from a helper thread through the real MCP tool,
once the CLI is up, the way the CLI's own server would make it.

Timing is driven by the fake CLI: it appends a closed-turn record
(`system/turn_duration`) to the transcript at a set offset, and the driver is
asserted to stop it only after that. `compact_idle_seconds` is 600 unless a test
needs the automatic path, so nothing here is the idle rule's doing; the reading
is below `compact_at_tokens` unless a test needs the crossing.

What the contract leaves open and the tests therefore do not fix: the
plugin's entry-point names for `in_session` (everything but `launch`,
`check`, `prepare` and `compact` is recorded as "other" and judged by its
payload), the audit event kinds (matched on the phase word), the status
payload beyond `request_id` / `status`, and when a failed delivery is
retried (the tests allow the CLI's whole life).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import oc_harness as oc  # noqa: E402
from oc_harness import CLOSE, GRACE, OVER, UNDER, Oc, declaration, sent, text  # noqa: E402

from multiagents import server  # noqa: E402
import test_phase0_interactive_compact as ic  # noqa: E402
from test_phase0_interactive_compact import assert_stop_compact_resume  # noqa: E402

LIFE = 4.0       # a CLI a correct driver stops long before this
SHORT = 2.2      # a CLI that must NOT be stopped lives this long
REASON = "the plan changed; start clean"


@pytest.fixture
def make(tmp_path, monkeypatch, capsys):
    def build(mode="restart", **kw) -> Oc:
        declare = declaration(mode) if mode else None
        return Oc(tmp_path, monkeypatch, capsys, declare=declare, **kw)
    yield build
    server._reset()


def ask(s: Oc, **kw):
    """The request, made by the orchestrator once its CLI is up."""
    return s.later(lambda: s.request(REASON), **kw)


def closes(*at: float) -> list:
    return [[t, [CLOSE]] for t in at]


def stopped_after_close(s: Oc, close_n: int = 0) -> float:
    """Seconds between the `close_n`-th closed turn the fake wrote and the SIGTERM."""
    appends = [e["t"] for e in s.calls("append")]
    return s.stops()[0]["t"] - appends[close_n]


def not_stopped(s: Oc) -> None:
    assert s.stops() == [], "the CLI was stopped"
    assert s.calls("compact") == [], "a compaction ran"
    assert len(s.calls("launch")) == 1


# ================================================================== OC-R2 ==

def test_oc_r2_a_request_below_the_threshold_with_an_agent_running_stops_compacts_and_resumes(make):
    s = make("restart")
    s.reading(UNDER)
    agent = s.node("running")
    box = ask(s)
    code, out = s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert "error" not in box, box
    request_id = box["value"]["request_id"]
    assert_stop_compact_resume(s)
    assert code == 0
    assert s.elapsed < LIFE - 1, "the driver waited for something the request skips"
    assert s.wait_status(request_id, "completed").get("status") == "completed"
    # the agent is neither stopped nor lost: still running, and named on the way back
    assert s.tree.read()["nodes"][agent]["status"] == "running"
    assert agent in s.prompt(), "the resumed session is not told what is in flight"


def test_oc_r2_the_resume_prompt_names_the_reason(make):
    s = make("restart")
    s.reading(UNDER)
    s.node("running")
    ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert_stop_compact_resume(s)
    assert REASON in s.prompt(), s.prompt()
    assert "compacted by the driver" in s.prompt().lower()    # the existing return prompt


def test_oc_r2_nothing_in_flight_still_resumes_with_the_reason(make):
    s = make("restart")
    s.reading(UNDER)
    ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert_stop_compact_resume(s)
    assert REASON in s.prompt(), "an explicit request resumes silently"


def test_oc_r2_nothing_happens_before_the_turn_closes(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": SHORT}])
    assert "error" not in box, box
    not_stopped(s)
    assert s.status(box["value"]["request_id"]).get("status") == "pending"


def test_oc_d3_an_idle_record_older_than_the_request_does_not_count(make):
    s = make("restart")
    s.reading(UNDER)
    ch_append = oc.ch.append_transcript
    ch_append(s.transcript, [CLOSE])          # the transcript ends closed, before the request
    box = ask(s)
    s.go([{"life": SHORT}])
    not_stopped(s)
    assert s.status(box["value"]["request_id"]).get("status") == "pending"


def test_oc_d3_a_turn_state_nothing_declares_never_authorises(make):
    s = make("restart")
    oc.ch.write_transcript(s.transcript, [{"type": "summary", "summary": "x"},
                                          oc.ch.request(UNDER)])
    s.provider.transcript["turn_state"] = []
    box = ask(s)
    s.go([{"life": SHORT, "appends": closes(0.5)}])
    not_stopped(s)
    assert s.status(box["value"]["request_id"]).get("status") == "pending"


def test_oc_d3_a_transcript_that_cannot_be_read_never_authorises(make):
    s = make("restart", transcript_block=False)
    box = ask(s)
    s.go([{"life": SHORT}])
    not_stopped(s)
    assert s.status(box["value"]["request_id"]).get("status") == "pending"


def test_oc_r2_the_announcement_and_the_grace_period_are_kept(make):
    s = make("restart", limits={"compact_grace_seconds": 2})
    s.reading(UNDER)
    ask(s)
    _, out = s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert_stop_compact_resume(s)
    lines = [l for l in out.split("\n") if "send" in l.lower() and "cancel" in l.lower()]
    assert lines, "no announcement saying that a sent message cancels"
    assert stopped_after_close(s) >= 1.8, "the stop came before the grace period ran out"


def test_oc_r2_the_threshold_crossing_that_already_compacted_does_not_block_a_request(make):
    s = make("restart", limits={"compact_idle_seconds": 1})
    s.reading(OVER)
    box = s.later(lambda: s.request(REASON), after_launches=2)
    s.go([{"life": LIFE},                                    # the automatic compaction
          {"life": 4.0, "appends": closes(0.5)},            # then the request
          {"life": 0}])
    assert len(s.calls("compact")) == 2, s.seq()
    assert len(s.stops()) == 2
    assert s.wait_status(box["value"]["request_id"], "completed").get("status") == "completed"


def test_oc_d9_a_failure_that_disabled_automatic_compaction_does_not_block_a_request(make):
    s = make("restart", limits={"compact_idle_seconds": 1})
    s.reading(OVER)
    box = s.later(lambda: s.request(REASON), after_launches=2)
    s.go([{"life": LIFE},
          {"life": 4.0, "appends": closes(0.5)},
          {"life": 0}],
         compacts=[{"exit": 1, "stderr": "boom"}, {"exit": 0, "stdout": oc.FIGURES + "\n"}])
    assert len(s.calls("compact")) == 2, s.seq()
    assert s.wait_status(box["value"]["request_id"], "completed").get("status") == "completed"


# ================================================================== OC-R3 ==

def test_oc_r3_the_request_is_consumed_exactly_once(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)},
          {"life": 3.0, "appends": closes(0.3, 1.2)},     # later turns close too
          ])
    request_id = box["value"]["request_id"]
    assert len(s.calls("compact")) == 1, s.seq()
    assert len(s.stops()) == 1
    assert s.status(request_id).get("status") == "completed"
    # and a new call afterwards is a new request
    fresh = s.request("again")
    assert fresh.get("status") == "pending" and fresh["request_id"] != request_id, fresh


def test_oc_r3_a_request_recorded_before_the_driver_started_is_honoured_by_it(make):
    """A driver restart: the request outlives the driver that saw it."""
    s = make("restart")
    s.reading(UNDER)
    request_id = s.request(REASON)["request_id"]
    server._reset()
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert_stop_compact_resume(s)
    assert s.status(request_id).get("status") == "completed"


def test_oc_d9_a_session_that_ends_first_leaves_the_request_pending_or_failed_session_ended(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": 1.5}])                      # the user quits before the turn closes
    got = s.status(box["value"]["request_id"])
    assert s.calls("compact") == []
    assert got.get("status") in ("pending", "failed"), got
    if got["status"] == "failed":
        assert "session_ended" in text(got), got


# ================================================================== OC-R4 ==

def test_oc_r4_a_sent_message_delays_the_compaction_to_the_next_closed_turn(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": 5.5, "appends": [[0.5, [CLOSE]], [1.0, [sent()]], [2.2, [CLOSE]]]},
          {"life": 0}])
    assert_stop_compact_resume(s)
    appended = [e["t"] for e in s.calls("append")]
    assert len(appended) == 3, appended
    assert s.stops()[0]["t"] >= appended[2], "stopped before the reply to the sent message closed"
    assert len(s.calls("compact")) == 1
    assert s.wait_status(box["value"]["request_id"], "completed").get("status") == "completed"


def test_oc_r4_a_sent_message_does_not_cancel_the_request_for_good(make):
    """The contrast with the automatic compaction: after the message and its
    reply the request is still honoured, not dropped."""
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": SHORT, "appends": [[0.5, [CLOSE]], [1.0, [sent()]]]}])
    # The message is still unanswered when the CLI ends: no compaction, and the
    # request is neither completed nor failed.
    assert s.calls("compact") == []
    assert s.status(box["value"]["request_id"]).get("status") in ("pending",), s.status()


# ================================================================== OC-R5 ==

def test_oc_r5_a_failed_compaction_fails_the_request_and_the_session_continues(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    code, _ = s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}],
                   compacts=[{"exit": 1, "stderr": "disk-on-fire"}])
    request_id = box["value"]["request_id"]
    got = s.wait_status(request_id, "failed")
    assert got.get("status") == "failed", got
    assert REASON in text(got) or "disk-on-fire" in text(got)
    assert len(s.calls("launch")) == 2, "the session was not carried on after the failure"
    assert code == 0
    fresh = s.request("try again")
    assert fresh.get("status") == "pending" and fresh["request_id"] != request_id, fresh


def test_oc_r5_a_plugin_that_says_it_cannot_at_run_time_fails_the_request(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}],
         compacts=[{"exit": 64}])
    got = s.wait_status(box["value"]["request_id"], "failed")
    assert got.get("status") == "failed", got
    assert len(s.calls("launch")) == 2


@pytest.mark.parametrize("mode", ["restart", "in_session"])
def test_oc_r5_a_safe_point_that_is_not_reached_fails_the_request_and_leaves_the_cli(make, mode):
    s = make(mode, limits={"compact_safe_point_seconds": 1})
    s.reading(UNDER)
    box = ask(s)
    code, _ = s.go([{"life": 4.5, "appends": closes(0.5), "rogue_server": True}])
    request_id = box["value"]["request_id"]
    assert s.stops() == [], "the CLI was stopped without a safe point"
    assert s.calls("compact") == [], "a compaction ran without a safe point"
    # OA-R3 requires startup identity; the harness logs it separately.
    assert s.other() == [], "a control or delivery action ran without a safe point"
    assert len(s.calls("launch")) == 1 and code == 0
    got = s.status(request_id)
    assert got.get("status") == "failed", got
    assert str(got.get("reason", got)).strip()
    fresh = s.request("again")
    assert fresh.get("status") == "pending" and fresh["request_id"] != request_id, fresh


# ================================================================== OC-R6 ==

def test_oc_r6_the_request_is_honoured_through_the_plugins_compact_entry_point(make):
    s = make("restart")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    real = [c for c in s.calls("compact")]
    assert len(real) == 1
    assert real[0]["env"].get("MULTIAGENTS_SESSION_ID") == oc.SID
    assert box["value"]["request_id"] in json.dumps({"argv": real[0]["argv"], "env": real[0]["env"]}), (
        "D5: the request id is not passed to the plugin")


def test_oc_d1_the_entry_point_is_asked_for_nothing_when_the_provider_is_unsupported(make):
    s = make(None)
    s.reading(UNDER)
    got = {}
    box = s.later(lambda: got.setdefault("r", s.request(REASON)))
    s.go([{"life": SHORT, "appends": closes(0.5)}])
    assert box["value"]["status"] == "unsupported"
    not_stopped(s)


# ================================================================== OC-R8 ==

def test_oc_r8_restart_replaces_the_cli_process(make):
    s = make("restart")
    s.reading(UNDER)
    ask(s)
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    assert_stop_compact_resume(s)
    launches = s.calls("launch")
    assert len(launches) == 2 and launches[0]["pid"] != launches[1]["pid"]
    assert len(s.stops()) == 1


def test_oc_r8_in_session_keeps_the_same_cli_process_and_never_stops_it(make):
    s = make("in_session")
    s.reading(UNDER)
    box = ask(s)
    code, _ = s.go([{"life": 4.0, "appends": closes(0.5)}])
    request_id = box["value"]["request_id"]
    assert len(s.calls("launch")) == 1, "the CLI was relaunched"
    assert s.stops() == [], "the CLI was terminated"
    assert [e["action"] for e in s.calls() if e["action"] in ("exit",)] == ["exit"]
    # the compaction itself ran through the plugin, for this request
    real = s.calls("compact")
    assert len(real) == 1, s.seq()
    assert request_id in json.dumps({"argv": real[0]["argv"], "env": real[0]["env"]})
    assert s.wait_status(request_id, "completed").get("status") == "completed"
    assert code == 0


def test_oc_r8_in_session_delivers_the_return_prompt_as_the_next_input(make):
    s = make("in_session")
    s.reading(UNDER)
    s.node("running")
    ask(s)
    s.go([{"life": 4.0, "appends": closes(0.5)}])
    delivered = s.delivered()
    assert delivered, f"no plugin invocation carried the return prompt: {s.other()}"
    assert REASON in delivered[0]["payload"]
    # delivered after the compaction, not before it
    assert delivered[0]["t"] >= s.calls("compact")[0]["t"]
    assert s.stops() == []


def test_oc_d7_a_failed_delivery_is_retried_without_compacting_again(make):
    s = make("in_session")
    s.reading(UNDER)
    box = ask(s)
    s.go([{"life": 5.0, "appends": closes(0.5)}], deliver_fail_first=True)
    assert len(s.calls("compact")) == 1, "the compaction was repeated for a failed delivery"
    assert len(s.delivered()) >= 2, "the delivery was not retried"
    assert s.status(box["value"]["request_id"]).get("status") == "completed"


@pytest.mark.parametrize("mode", ["restart", "in_session"])
def test_oc_r8_r2_to_r5_apply_to_both_modes(make, mode):
    """A sent message delays the compaction in either mode (OC-R4)."""
    s = make(mode)
    s.reading(UNDER)
    ask(s)
    s.go([{"life": 5.5, "appends": [[0.5, [CLOSE]], [1.0, [sent()]], [2.2, [CLOSE]]]},
          {"life": 0}] if mode == "restart" else
         [{"life": 5.0, "appends": [[0.5, [CLOSE]], [1.0, [sent()]], [2.2, [CLOSE]]]}])
    done = s.calls("compact")
    assert len(done) == 1
    appended = [e["t"] for e in s.calls("append")]
    assert done[0]["t"] >= appended[2]


# ================================================================== D10 ====

def test_oc_d10_the_driver_records_an_event_per_phase(make):
    s = make("restart")
    s.reading(UNDER)
    long_reason = "x" * 2000
    box = s.later(lambda: s.request(long_reason))
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}])
    request_id = box["value"]["request_id"]
    phases = s.phases()
    for word in ("requested", "started", "compacted", "completed"):
        assert phases.get(word), f"no {word!r} event: {sorted(phases)}"
        event = phases[word][0]
        assert event.get("request_id") == request_id, event
        assert event.get("session") == oc.SID, event
        assert event.get("mode") == "restart", event
        assert len(str(event.get("reason", ""))) <= 500, event
    assert not phases.get("failed")


def test_oc_d10_a_failure_is_recorded_with_the_request_id_and_a_bounded_reason(make):
    s = make("restart")
    s.reading(UNDER)
    box = s.later(lambda: s.request("y" * 2000))
    s.go([{"life": LIFE, "appends": closes(0.5)}, {"life": 0}], compacts=[{"exit": 1}])
    failed = s.phases().get("failed")
    assert failed, sorted(s.phases())
    assert failed[0].get("request_id") == box["value"]["request_id"]
    assert len(str(failed[0].get("reason", ""))) <= 500
    assert not s.phases().get("completed")
