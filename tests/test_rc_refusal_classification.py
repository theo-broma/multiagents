"""RC-R1..RC-R5: a provider refusal is not a startup outage.

Contract: context/specs/refusal-classification.md (ticket bug-1213a0).
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import rc_harness as rc  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.providers import Provider  # noqa: E402

PATTERN = "zq-blocked-[a-z]+"
STDERR_LINE = "zq-blocked-content: request declined\n"


def _shipped(name: str) -> dict:
    path = Path(__file__).parents[1] / "src/multiagents/defaults/providers.yaml"
    return yaml.safe_load(path.read_text())["providers"][name]


def _one(tmp_path, monkeypatch, *, markers=(PATTERN,), limits=None, **ctl):
    cli = rc.Cli(tmp_path, "p", markers=list(markers))
    cli.set(**ctl)
    r = rc.runner(tmp_path, monkeypatch, {"p": cli.config}, limits=limits)
    return r, cli


# ---------------------------------------------------------------- RC-R1 ----

def test_rc_r1_shipped_codex_declares_markers_matching_the_filter_message():
    markers = Provider.from_dict("codex", _shipped("codex")).refusal_markers
    assert markers, "codex declares no refusal_markers"
    line = "This content was flagged for possible cybersecurity risk…"
    assert any(re.search(m, line, re.IGNORECASE) for m in markers)


@pytest.mark.parametrize("line", [
    "THIS CONTENT WAS FLAGGED FOR POSSIBLE CYBERSECURITY RISK",
    "this content was flagged for possible cybersecurity risk",
    "error: This content was flagged for possible cybersecurity risk. Try again.\n",
    "This content was flagged for possible policy risk",
])
def test_rc_r1_codex_markers_are_case_insensitive_and_cover_other_risks(line):
    markers = Provider.from_dict("codex", _shipped("codex")).refusal_markers
    assert any(re.search(m, line, re.IGNORECASE) for m in markers), line


@pytest.mark.parametrize("line", [
    "startup exploded",
    "error: could not connect to the provider",
    "This content was saved for later review",
])
def test_rc_r1_codex_markers_do_not_match_ordinary_failures(line):
    markers = Provider.from_dict("codex", _shipped("codex")).refusal_markers
    assert not any(re.search(m, line, re.IGNORECASE) for m in markers), line


def test_rc_r1_codex_shaped_run_with_filter_on_stderr_ends_refused(tmp_path, monkeypatch):
    markers = Provider.from_dict("codex", _shipped("codex")).refusal_markers
    r, _ = _one(tmp_path, monkeypatch, markers=markers,
                events=[], stderr=rc.CODEX_MESSAGE + "\n", exit=1)
    result = rc.start(r)
    assert rc.status(r, result) == "refused"


# ---------------------------------------------------------------- RC-R2 ----

def test_rc_r2_nonzero_exit_with_marker_only_on_stderr_is_refused(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr=STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_stderr_match_is_a_search_not_a_full_match(tmp_path, monkeypatch):
    noise = "warn: something\n" + STDERR_LINE + "trailing: more text after\n"
    r, _ = _one(tmp_path, monkeypatch, stderr=noise, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_stderr_match_ignores_case(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr=STDERR_LINE.upper(), exit=1)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_nonzero_exit_after_progress_with_stderr_marker_is_refused(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, events=rc.OK_EVENTS, stderr=STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_zero_exit_with_marker_on_stderr_keeps_its_status(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, events=rc.OK_EVENTS, stderr=STDERR_LINE, exit=0)
    assert rc.status(r, rc.start(r)) == "done"


def test_rc_r2_nonzero_exit_without_marker_stays_failed(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr="startup exploded\n", exit=1)
    assert rc.status(r, rc.start(r)) == "failed"


def test_rc_r2_nonzero_exit_with_empty_stderr_stays_failed(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, exit=1)
    assert rc.status(r, rc.start(r)) == "failed"


def test_rc_r2_provider_without_markers_never_reclassifies(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, markers=(), stderr=STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "failed"


def test_rc_r2_final_message_still_fullmatches_and_refuses(tmp_path, monkeypatch):
    events = [{"type": "text", "text": "ZQ-BLOCKED-FILTER", "session_id": "s1"}]
    r, _ = _one(tmp_path, monkeypatch, events=events, exit=0)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_final_message_containing_a_marker_is_not_a_full_match(tmp_path, monkeypatch):
    # Unchanged: only stderr is searched. An answer that merely mentions the
    # phrase is an answer.
    events = [{"type": "text", "text": "the log said zq-blocked-content today",
               "session_id": "s1"}]
    r, _ = _one(tmp_path, monkeypatch, events=events, exit=0)
    assert rc.status(r, rc.start(r)) == "done"


def test_rc_r2_nonzero_exit_with_marker_only_inside_final_message_stays_failed(tmp_path, monkeypatch):
    events = [{"type": "text", "text": "the log said zq-blocked-content today",
               "session_id": "s1"}]
    r, _ = _one(tmp_path, monkeypatch, events=events, exit=1)
    assert rc.status(r, rc.start(r)) == "failed"


def test_rc_r2_truncation_outranks_a_stderr_refusal_marker(tmp_path, monkeypatch):
    cli = rc.Cli(tmp_path, "p", markers=[PATTERN], truncation_markers=["print timeout"])
    cli.set(stderr="print timeout\n" + STDERR_LINE, exit=1)
    r = rc.runner(tmp_path, monkeypatch, {"p": cli.config})
    assert rc.status(r, rc.start(r)) == "truncated"


def test_rc_r2_stop_requested_outranks_a_stderr_refusal_marker(tmp_path, monkeypatch):
    import asyncio
    r, _ = _one(tmp_path, monkeypatch, events=rc.OK_EVENTS, stderr=STDERR_LINE,
                exit=1, late=30)

    async def go():
        result = await r.start("worker", "work")
        deadline = time.time() + 15
        while time.time() < deadline and not r.tree.get(result["agent_id"]).session_id:
            await asyncio.sleep(0.05)
        await r.stop(result["agent_id"])
        run = r.runs.get(result["agent_id"])
        if run:
            await asyncio.wait_for(run.done.wait(), 20)
        return result
    result = asyncio.run(go())
    assert rc.status(r, result) != "refused"


@pytest.mark.parametrize("attempt", range(5))
def test_rc_r2_marker_written_last_after_a_flood_of_stderr_is_still_seen(tmp_path, monkeypatch, attempt):
    # The verdict must not depend on how much of stderr the drain got to
    # before the process was reaped.
    flood = ("noise line of stderr output, padding padding padding\n" * 4000)
    r, _ = _one(tmp_path, monkeypatch, stderr=flood + STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"


def test_rc_r2_records_source_and_pattern_for_stderr(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr=STDERR_LINE, exit=1)
    result = rc.start(r)
    assert rc.status(r, result) == "refused"
    blob = rc.recorded(r, result)
    assert "stderr" in blob.lower(), blob
    assert PATTERN in blob, blob


def test_rc_r2_records_source_and_pattern_for_assistant(tmp_path, monkeypatch):
    events = [{"type": "text", "text": "ZQ-BLOCKED-FILTER", "session_id": "s1"}]
    r, _ = _one(tmp_path, monkeypatch, events=events, exit=0)
    result = rc.start(r)
    assert rc.status(r, result) == "refused"
    blob = rc.recorded(r, result)
    assert "assistant" in blob.lower(), blob
    assert PATTERN in blob, blob


def test_rc_r2_recorded_excerpt_is_present_and_at_most_200_chars(tmp_path, monkeypatch):
    long_line = "zq-blocked-" + "x" * 1000
    r, _ = _one(tmp_path, monkeypatch, markers=["zq-blocked-x+"],
                stderr=long_line + "\n", exit=1)
    result = rc.start(r)
    assert rc.status(r, result) == "refused"
    blob = rc.recorded(r, result)
    assert "zq-blocked-xxx" in blob, "no excerpt of the matched text recorded"
    assert "x" * 201 not in blob, "excerpt exceeds 200 chars"


def test_rc_r2_recorded_excerpt_is_redacted(tmp_path, monkeypatch):
    secret = "sk-" + "A1b2C3d4" * 4
    r, _ = _one(tmp_path, monkeypatch, markers=["zq-blocked-.*"],
                stderr=f"zq-blocked-token {secret}\n", exit=1)
    result = rc.start(r)
    assert rc.status(r, result) == "refused"
    blob = rc.recorded(r, result)
    assert "zq-blocked-token" in blob
    assert secret not in blob


# ---------------------------------------------------------------- RC-R3 ----

def test_rc_r3_refused_runs_never_set_startup_down(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr=STDERR_LINE, exit=1,
                limits={"startup_failure_threshold": 2})
    for _ in range(4):
        assert rc.status(r, rc.start(r)) == "refused"
    assert r.startup.availability("p") is None
    assert not rc.events(r, "startup_down")


def test_rc_r3_threshold_one_single_refusal_does_not_trip(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr=STDERR_LINE, exit=1,
                limits={"startup_failure_threshold": 1})
    assert rc.status(r, rc.start(r)) == "refused"
    assert r.startup.availability("p") is None
    # and the provider still admits the next start
    again = rc.start(r)
    assert isinstance(again, dict) and again.get("agent_id"), again


def test_rc_r3_assistant_sourced_refusal_does_not_trip_either(tmp_path, monkeypatch):
    events = [{"type": "text", "text": "ZQ-BLOCKED-FILTER", "session_id": "s1"}]
    r, _ = _one(tmp_path, monkeypatch, events=events, exit=1,
                limits={"startup_failure_threshold": 1})
    assert rc.status(r, rc.start(r)) == "refused"
    assert r.startup.availability("p") is None


def test_rc_r3_a_refusal_does_not_add_to_the_failure_count(tmp_path, monkeypatch):
    # threshold 2: refusal, then two genuine failures. The refusal adds
    # nothing, so one real failure leaves the provider up and only the
    # second trips it.
    r, cli = _one(tmp_path, monkeypatch, limits={"startup_failure_threshold": 2})
    cli.set(stderr=STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"
    cli.set(stderr="startup exploded\n", exit=1)
    assert rc.status(r, rc.start(r)) == "failed"
    assert r.startup.availability("p") is None, "a refusal was counted as a failure"
    assert rc.status(r, rc.start(r)) == "failed"
    assert r.startup.availability("p") is not None


def test_rc_r3_refused_half_open_probe_leaves_provider_available(tmp_path, monkeypatch):
    r, cli = _one(tmp_path, monkeypatch, limits={"startup_failure_threshold": 1,
                                                 "provider_down_cooldown_seconds": 0.15})
    cli.set(stderr="startup exploded\n", exit=1)
    assert rc.status(r, rc.start(r)) == "failed"
    assert r.startup.availability("p") is not None
    time.sleep(0.25)
    cli.set(stderr=STDERR_LINE, exit=1)
    probe = rc.start(r)
    assert rc.status(r, probe) == "refused"
    assert r.startup.availability("p") is None
    nxt = rc.start(r)
    assert isinstance(nxt, dict) and nxt.get("agent_id"), nxt


def test_rc_r3_refusal_is_not_retried_automatically(tmp_path, monkeypatch):
    r, cli = _one(tmp_path, monkeypatch, stderr=STDERR_LINE, exit=1)
    assert rc.status(r, rc.start(r)) == "refused"
    time.sleep(0.5)
    assert cli.launches() == 1


# ---------------------------------------------------------------- RC-R4 ----

def _pinned_world(tmp_path, monkeypatch):
    """A provider with one finished, resumable, model-pinned run."""
    r, cli = _one(tmp_path, monkeypatch, markers=(),
                  limits={"startup_failure_threshold": 1,
                          "provider_down_cooldown_seconds": 0.3})
    cli.ok()
    first = rc.start(r, model="m1")
    assert rc.status(r, first) == "done", first
    assert r.tree.get(first["agent_id"]).session_id
    return r, cli, first["agent_id"]


def _trip(r, cooldown):
    token = r.startup.claim("p", "rc-trip")
    r.startup.finish("p", "rc-trip", token, True, "boom", 1, cooldown)
    assert r.startup.availability("p") is not None


def _verdicts(r, agent_id):
    start = rc.start(r, model="m1")
    steer = rc.steer(r, agent_id)
    return start, steer


def test_rc_r4_cooldown_active_start_and_steer_agree(tmp_path, monkeypatch):
    r, _, agent_id = _pinned_world(tmp_path, monkeypatch)
    _trip(r, cooldown=60)
    start, steer = _verdicts(r, agent_id)
    assert isinstance(start, dict) and isinstance(steer, dict)
    assert start["reason"] == steer["reason"] == "startup_down"
    assert start.get("retry_after") and start["retry_after"] == steer["retry_after"]
    assert not start.get("agent_id") and steer.get("steered") is False


def test_rc_r4_half_open_probe_held_start_and_steer_both_refused_alike(tmp_path, monkeypatch):
    r, _, agent_id = _pinned_world(tmp_path, monkeypatch)
    _trip(r, cooldown=0.05)
    time.sleep(0.15)
    probe = r.startup.claim("p", "rc-probe")      # someone else holds the probe
    assert probe
    start, steer = _verdicts(r, agent_id)
    assert start["reason"] == steer["reason"] == "startup_down"
    assert start.get("retry_after") == steer.get("retry_after")
    assert not start.get("agent_id") and steer.get("steered") is False


def test_rc_r4_half_open_probe_free_start_may_take_it_and_resolves_it(tmp_path, monkeypatch):
    r, cli, agent_id = _pinned_world(tmp_path, monkeypatch)
    _trip(r, cooldown=0.05)
    time.sleep(0.15)
    start = rc.start(r, model="m1")
    assert isinstance(start, dict) and start.get("agent_id"), start
    assert rc.status(r, start) == "done"
    assert r.startup.availability("p") is None


def test_rc_r4_half_open_probe_free_steer_may_take_it_and_resolves_it(tmp_path, monkeypatch):
    r, cli, agent_id = _pinned_world(tmp_path, monkeypatch)
    _trip(r, cooldown=0.05)
    time.sleep(0.15)
    steer = rc.steer(r, agent_id)
    assert isinstance(steer, dict) and steer.get("steered") is True, steer
    assert r.startup.availability("p") is None


def test_rc_r4_steer_refused_for_startup_down_leaves_the_run_resumable(tmp_path, monkeypatch):
    r, cli, agent_id = _pinned_world(tmp_path, monkeypatch)
    before = cli.launches()
    _trip(r, cooldown=60)
    assert rc.steer(r, agent_id)["steered"] is False
    assert cli.launches() == before
    assert r.tree.get(agent_id).session_id


# ---------------------------------------------------------------- RC-R5 ----

def test_rc_r5_marker_free_early_exit_still_counts_as_startup_failure(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, stderr="startup exploded\n", exit=1,
                limits={"startup_failure_threshold": 1})
    assert rc.status(r, rc.start(r)) == "failed"
    down = rc.events(r, "startup_down")
    assert len(down) == 1 and down[0].get("provider") == "p", down
    assert r.startup.availability("p") is not None


def test_rc_r5_marker_free_early_exit_trips_at_threshold_even_with_markers_declared(tmp_path, monkeypatch):
    markers = Provider.from_dict("codex", _shipped("codex")).refusal_markers
    r, _ = _one(tmp_path, monkeypatch, markers=markers, stderr="connection reset\n",
                exit=1, limits={"startup_failure_threshold": 2})
    rc.start(r)
    assert r.startup.availability("p") is None
    rc.start(r)
    blocked = r.startup.availability("p")
    assert blocked and blocked["reason"] == "startup_down"


def test_rc_r5_success_with_progress_does_not_trip(tmp_path, monkeypatch):
    r, _ = _one(tmp_path, monkeypatch, events=rc.OK_EVENTS,
                limits={"startup_failure_threshold": 1})
    assert rc.status(r, rc.start(r)) == "done"
    assert r.startup.availability("p") is None
