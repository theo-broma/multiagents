"""LR — a loop held by a failed run can be resumed (context/specs/loop-run-failed-resume.md).

LR-R1 relaunching the held loop, LR-R2 relaunching the failed child, LR-R3 other
holds keep their preconditions, LR-R4 `wait_for_agents` reports a hold once.

Scripting as in tests/test_nc_m4_loops.py: per-provider queues, one entry per
activation. A child that "fails" exits 1 on its first run and on the scheduler's
one retry (two `{"exit": 1}` entries), which holds the loop `run_failed`.

Fixture loop `three`: `wa` (provider fxa, completes), `wb` (provider fxw, session
alias A, the one that fails), `rv` (provider fxr, gives the verdict).

Assumptions where the contract is silent (kept loose):
- a refused relaunch is `ok: false`; no error code is asserted.
- "resumes its session": the rerun's `resume` equals the session of an earlier
  run of the same child (which one is not asserted: the retry may have rebound).
- "a hold names the child": not asserted; the tests fail exactly one child.
- NOT TESTED (spec silent): `max_rounds` not above the counter on a run_failed
  loop, root-only / stale-revision checks on this path.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.m4_world import (M4World, commit_entry, finding, verdict_entry)  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT = 8
WAIT_ROUNDS = 20
CLEARED = 4         # the hold must be gone this soon after an accepted relaunch
QUIET = 2
SHORT = 2           # timeout of a wait that must block
ASK = "NEED_INFO(q): LR-QUESTION-1?"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxa = world.provider("fxa")
    world.fxw = world.provider("fxw", session_model_change=True)
    world.fxr = world.provider("fxr")
    world.agent("wa", "fxa", writes=True)
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


FAIL = {"exit": 1}


TRIO = """\
template: lrtrio
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: pre, kind: simple, agent: wa, task: "prepare"}
    - {key: wrk, kind: simple, agent: wk, task: "do the work", session: A}
    - {key: rev, kind: simple, agent: rv, task: "review the work", session: B}
"""


def three(w, rounds=3):
    """wa -> wb (alias A) -> rv (alias B), in one loop."""
    w.register_ok(TRIO)
    loop = w.instantiate_ok("lrtrio", {"rounds": rounds})
    wa, wb, rv = w.children(loop)
    return loop, wa, wb, rv


def held_by_worker(w, rounds=3):
    """`wb` fails twice in round 1: the loop is held run_failed."""
    w.fxa.queue(commit_entry("a.txt", "a\n", "prepare"))
    w.fxw.queue(FAIL, FAIL)
    w.start_scheduler()
    loop, wa, wb, rv = three(w, rounds)
    held = w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 0
    return loop, wa, wb, rv, held


def held_by_reviewer(w):
    w.fxa.queue(commit_entry("a.txt", "a\n", "prepare"))
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(FAIL, FAIL)
    w.start_scheduler()
    loop, wa, wb, rv = three(w)
    held = w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert w.fxr.spawns() == 2
    return loop, wa, wb, rv, held


def not_held_run_failed(w, loop):
    n = w.get(loop)
    return (n.get("hold") or {}).get("reason") != "run_failed" and n["state"] != "held"


def hold_cleared(w, loop):
    w.until(lambda: not_held_run_failed(w, loop), CLEARED, what=f"{loop} released from run_failed")


def relaunch_loop(w, loop, **args):
    return w.root_op("relaunch_node", loop, **args)


# =========================================================================== R1

def test_lr_r1_relaunching_a_loop_held_by_a_failed_worker_is_accepted_and_the_round_completes(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    reply = relaunch_loop(w, loop)
    assert reply.get("ok") is True, reply
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 0
    assert len(done["generations"]) == 1, "the relaunch counted a new round"
    assert w.fxw.spawns() == 3 and w.fxr.spawns() == 1


def test_lr_r1_only_the_failed_child_is_rerun_and_the_sibling_keeps_its_state(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    before = w.get(wa)
    assert before["state"] == "done" and before["outcome"] == "completed"
    w.fxw.queue({"gate": "gb", **commit_entry("b.txt", "b\n", "work")})
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    w.wait_running(wb, timeout=WAIT)
    sibling = w.get(wa)
    assert sibling["state"] == "done" and sibling["outcome"] == "completed"
    assert len(sibling["runs"]) == len(before["runs"]), "the sibling was rerun"
    w.gate("gb", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert w.fxa.spawns() == 1, "the completed sibling ran again"
    assert len(w.get(wa)["runs"]) == 1


def test_lr_r1_the_hold_is_cleared_and_the_round_count_is_unchanged(w):
    loop, wa, wb, rv, held = held_by_worker(w)
    rounds = held["loop"]["rounds_rejected"]
    w.fxw.queue({"gate": "gb", **commit_entry("b.txt", "b\n", "work")})
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    hold_cleared(w, loop)
    w.wait_running(wb, timeout=WAIT)
    now = w.get(loop)
    assert now["state"] == "running" and not now.get("hold")
    assert now["loop"]["rounds_rejected"] == rounds
    assert len(now["generations"]) == len(held["generations"])
    w.gate("gb", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)


def test_lr_r1_the_failed_child_resumes_its_session(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    calls = w.fxw.calls()
    assert len(calls) == 3
    assert calls[2]["resume"] is not None, "the rerun started a new provider session"
    assert calls[2]["resume"] in {calls[0]["session"], calls[1]["session"]}


def test_lr_r1_a_failed_verdict_child_is_rerun_on_the_same_generation(w):
    loop, wa, wb, rv, held = held_by_reviewer(w)
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert w.fxa.spawns() == 1 and w.fxw.spawns() == 1, "the siblings were rerun"
    calls = w.fxr.calls()
    assert len(calls) == 3
    assert calls[2]["head"] == calls[0]["head"], "the rerun reviewed another generation"
    assert calls[2]["resume"] in {calls[0]["session"], calls[1]["session"]}


def test_lr_r1_the_reviewers_rejection_after_the_resume_is_honoured(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "round 1"), commit_entry("c.txt", "c\n", "round 2"))
    w.fxr.queue(verdict_entry("rejected", [finding("LR-FINDING-9")]), verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    assert w.fxa.spawns() == 2 and w.fxr.spawns() == 2
    assert "LR-FINDING-9" in w.fxw.calls()[-1]["prompt"], "the findings did not reach the next round"


def test_lr_r1_max_rounds_is_optional_and_may_be_passed(w):
    loop, wa, wb, rv, held = held_by_worker(w, rounds=2)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop, max_rounds=5).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["loop"]["max_rounds"] == 5 and done["loop"]["rounds_rejected"] == 0


def test_lr_r1_pins_apply_to_the_failed_child(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop, pins={wb: {"model": "fxw/m2"}}).get("ok") is True
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    calls = w.fxw.calls()
    assert [c["model"] for c in calls[:2]] == ["fxw/m1", "fxw/m1"]
    assert calls[2]["model"] == "fxw/m2"
    assert calls[2]["resume"] is not None, "the pin dropped the session"


def test_lr_r1_a_second_relaunch_while_the_resumed_round_runs_is_refused(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue({"gate": "gb", **commit_entry("b.txt", "b\n", "work")})
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    w.wait_running(wb, timeout=WAIT)
    again = relaunch_loop(w, loop)
    assert again.get("ok") is False, again
    w.gate("gb", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert w.fxw.spawns() == 3, "the repeated relaunch started another run"


def test_lr_r1_a_child_that_fails_again_holds_the_loop_again_and_can_be_resumed_again(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(FAIL, FAIL)
    assert relaunch_loop(w, loop).get("ok") is True
    w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert w.fxw.spawns() == 4 and w.fxa.spawns() == 1
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0


# =========================================================================== R2

def test_lr_r2_relaunching_the_failed_worker_clears_the_loops_hold_and_continues(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    reply = w.root_op("relaunch_node", wb)
    assert reply.get("ok") is True, reply
    hold_cleared(w, loop)
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert len(done["generations"]) == 1
    assert w.fxa.spawns() == 1 and w.fxw.spawns() == 3 and w.fxr.spawns() == 1
    calls = w.fxw.calls()
    assert calls[2]["resume"] in {calls[0]["session"], calls[1]["session"]}


def test_lr_r2_relaunching_the_failed_reviewer_continues_on_the_same_generation(w):
    loop, wa, wb, rv, _ = held_by_reviewer(w)
    w.fxr.queue(verdict_entry("approved"))
    reply = w.root_op("relaunch_node", rv)
    assert reply.get("ok") is True, reply
    hold_cleared(w, loop)
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0
    assert w.fxa.spawns() == 1 and w.fxw.spawns() == 1
    calls = w.fxr.calls()
    assert calls[2]["head"] == calls[0]["head"]


def test_lr_r2_the_verdict_after_a_direct_child_relaunch_is_honoured(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "round 1"), commit_entry("c.txt", "c\n", "round 2"))
    w.fxr.queue(verdict_entry("rejected", [finding("LR-FINDING-2")]), verdict_entry("approved"))
    assert w.root_op("relaunch_node", wb).get("ok") is True
    hold_cleared(w, loop)
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["loop"]["rounds_rejected"] == 1 and done["outcome"] == "approved"


def test_lr_r2_pins_on_a_direct_relaunch_apply_to_the_child(w):
    loop, wa, wb, rv, _ = held_by_worker(w)
    w.fxw.queue(commit_entry("b.txt", "b\n", "work"))
    w.fxr.queue(verdict_entry("approved"))
    assert w.root_op("relaunch_node", wb, pins={wb: {"model": "fxw/m2"}}).get("ok") is True
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert w.fxw.calls()[2]["model"] == "fxw/m2"


# =========================================================================== R3

def test_lr_r3_a_running_loop_is_still_refused(w):
    w.fxa.queue({"gate": "ga", **commit_entry("a.txt", "a\n", "prepare")})
    w.start_scheduler()
    loop, wa, wb, rv = three(w)
    w.wait_state(loop, "running", timeout=WAIT)
    reply = relaunch_loop(w, loop)
    assert reply.get("ok") is False, reply
    reply = relaunch_loop(w, loop, max_rounds=5)
    assert reply.get("ok") is False, reply
    assert w.get(loop)["state"] == "running"
    w.gate("ga", w.fxa)


def test_lr_r3_a_loop_held_at_its_maximum_is_still_relaunched_with_a_higher_maximum(w):
    w.fxa.queue(commit_entry("a.txt", "a\n", "p1"), commit_entry("b.txt", "b\n", "p2"))
    w.fxw.queue(commit_entry("c.txt", "c\n", "w1"), commit_entry("d.txt", "d\n", "w2"))
    w.fxr.queue(verdict_entry("rejected", [finding("one")]), verdict_entry("approved"))
    w.start_scheduler()
    loop, *_ = three(w, rounds=1)
    w.wait_held(loop, "loop_max", timeout=WAIT_ROUNDS)
    assert relaunch_loop(w, loop, max_rounds=3).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    assert w.fxa.spawns() == 2, "a loop_max relaunch must start a new round"


# =========================================================================== R4

def held_unrelated(w, n=2):
    """`n` held simple nodes (they ended asking for information)."""
    for _ in range(n):
        w.fxw.queue({"text": ASK})
    w.start_scheduler()
    ids = [w.simple(f"H{i}", "wk", prose="ask") for i in range(n)]
    for i in ids:
        w.wait_held(i, "needs_info", timeout=WAIT)
    return ids


def start_gated(w, gate, **entry):
    w.fxr.queue({"gate": gate, **entry})
    r = call_tool(w, "start_agent", "rv", task("RUN", "run"))
    assert not r.get("error"), r
    w.wait_running(r["node_id"], timeout=WAIT)
    return r


def mentions(changed, r) -> bool:
    text = json.dumps(changed)
    return r["node_id"] in text or r["agent_id"] in text


def wait_agents(w, ids, timeout):
    t0 = time.monotonic()
    out = call_tool(w, "wait_for_agents", ids, timeout=timeout)
    return out, time.monotonic() - t0


def test_lr_r4_standing_holds_of_unrelated_nodes_do_not_end_a_wait_on_a_running_node(w):
    held = held_unrelated(w)
    r = start_gated(w, "gR")
    out, took = wait_agents(w, [r["node_id"]], SHORT)
    assert took >= SHORT - 0.5, f"returned after {took:.1f}s: {json.dumps(out)[:300]}"
    for h in held:
        assert h not in json.dumps(out.get("changed")), "an unrelated hold was reported as changed"
    w.gate("gR", w.fxr)


def test_lr_r4_repeated_waits_keep_blocking_and_never_repeat_the_old_holds(w):
    held = held_unrelated(w)
    r = start_gated(w, "gR")
    for _ in range(2):
        out, took = wait_agents(w, [r["node_id"]], 1)
        assert took >= 0.5, f"returned after {took:.1f}s: {json.dumps(out)[:300]}"
        assert not any(h in json.dumps(out.get("changed")) for h in held)
    w.gate("gR", w.fxr)


def test_lr_r4_the_wait_returns_when_the_requested_node_finishes(w):
    held = held_unrelated(w)
    r = start_gated(w, "gR")
    opener = threading.Timer(1.0, lambda: w.gate("gR", w.fxr))
    opener.start()
    try:
        out, took = wait_agents(w, [r["node_id"]], WAIT)
    finally:
        opener.cancel()
        w.gate("gR", w.fxr)
    assert 0.8 <= took < WAIT - 1, f"took {took:.1f}s: {json.dumps(out)[:300]}"
    assert mentions(out.get("changed"), r), out
    assert not any(h in json.dumps(out.get("changed")) for h in held)


def test_lr_r4_a_hold_of_the_requested_node_is_reported_once(w):
    held = held_unrelated(w)
    w.fxr.queue({"gate": "gR", "text": ASK})
    r = call_tool(w, "start_agent", "rv", task("RUN", "run"))
    w.wait_running(r["node_id"], timeout=WAIT)
    opener = threading.Timer(1.0, lambda: w.gate("gR", w.fxr))
    opener.start()
    try:
        out, took = wait_agents(w, [r["node_id"]], WAIT)
    finally:
        opener.cancel()
        w.gate("gR", w.fxr)
    assert took < WAIT - 1 and mentions(out.get("changed"), r), out
    w.wait_held(r["node_id"], "needs_info", timeout=WAIT)
    again, _ = wait_agents(w, [r["node_id"]], 1)
    assert not mentions(again.get("changed"), r), again
    assert not any(h in json.dumps(again.get("changed")) for h in held)
