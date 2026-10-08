"""LR-R1..LR-R3 — a loop held by a failed run can be resumed.

Contract: `context/specs/loop-run-failed-resume.md`.

A child run that fails twice in one round (the scheduler retries a failed
child once per round) holds its nearest loop with `run_failed`. The fixture
provides that with two `{"exit": 1}` queue entries on the child's provider.

Where the contract is silent (kept loose):
- worker (`W`) and reviewer (`R`) are bound to session aliases `A` and `B`;
  "its session binding is kept" is observed as the rerun being launched with
  `-s <session of the failed run>` (the fixture records `resume`).
- a refusal is `ok: false`; only `forbidden` / `conflict` are asserted by code.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import (M4World, commit_entry, err_code,  # noqa: E402
                                 finding, verdict_entry)

WAIT = 8
WAIT_ROUNDS = 20
CLEAR = 3
FAIL = {"exit": 1}


def make_world(tmp_path, monkeypatch, *, threshold=None, **provider_extra):
    world = M4World(tmp_path, monkeypatch)
    if threshold is not None:
        world.project["limits"]["provider_failure_threshold"] = threshold
    world.fxw = world.provider("fxw", **provider_extra)
    world.fxr = world.provider("fxr", **provider_extra)
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    return world


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def wpin(tmp_path, monkeypatch):
    """Providers that keep a session across a model change (NC-R32), so a pin is legitimate (LR-R1/R2)."""
    world = make_world(tmp_path, monkeypatch, session_model_change=True)
    yield world
    world.close()


@pytest.fixture
def wbreaker(tmp_path, monkeypatch):
    """Failure threshold 4: the original failure, its retry and a failed rerun do not trip the breaker."""
    world = make_world(tmp_path, monkeypatch, threshold=4)
    yield world
    world.close()


def work(n: int):
    return commit_entry(f"f{n}.txt", f"v{n}\n", f"round {n}")


PAIR = """\
template: lrpair
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: wrk, kind: simple, agent: wk, task: "implement the thing", session: A}
    - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
"""


def mkloop(w, max_rounds: int = 3):
    w.register_ok(PAIR)
    loop = w.instantiate_ok("lrpair", {"rounds": max_rounds})
    wk, rv = w.children(loop)
    return loop, wk, rv


def worker_failed(w, *, prior_rejection: bool = False, max_rounds: int = 3):
    """A loop held `run_failed` because the worker failed (twice) in its round."""
    if prior_rejection:
        w.fxw.queue(work(1), FAIL, FAIL)
        w.fxr.queue(verdict_entry("rejected", [finding("redo")]))
    else:
        w.fxw.queue(FAIL, FAIL)
    w.start_scheduler()
    loop, wk, rv = mkloop(w, max_rounds)
    held = w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert held["hold"]["detail"] == wk
    return loop, wk, rv


def reviewer_failed(w, max_rounds: int = 3):
    w.fxw.queue(work(1))
    w.fxr.queue(FAIL, FAIL)
    w.start_scheduler()
    loop, wk, rv = mkloop(w, max_rounds)
    held = w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert held["hold"]["detail"] == rv
    return loop, wk, rv


def hold_clears(w, loop):
    """Fail within seconds, not on the long completion wait, when the loop stays held."""
    return w.until(lambda: w.get(loop)["state"] != "held" or None, CLEAR,
                   what=f"the hold of {loop} to clear")


def relaunch_loop(w, loop, **kw):
    return w.root_op("relaunch_node", loop, **kw)


# ------------------------------------------------------------------ LR-R1

def test_lr_r1_relaunching_a_loop_held_by_a_failed_worker_is_accepted(w):
    loop, wk, rv = worker_failed(w)
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("approved"))
    reply = relaunch_loop(w, loop)
    assert reply.get("ok") is True, reply


def test_lr_r1_the_failed_worker_reruns_in_the_same_round_with_its_session(w):
    loop, wk, rv = worker_failed(w, prior_rejection=True)
    assert w.fxr.spawns() == 1, "the reviewer ran before the relaunch"
    failed_session = w.fxw.calls()[-1]["session"]
    spawns_before = w.fxw.spawns()
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved"
    assert w.fxw.spawns() == spawns_before + 1, "the worker did not rerun exactly once"
    rerun = w.fxw.calls()[-1]
    assert rerun["resume"] == failed_session, "the failed child lost its session binding"
    assert w.fxr.spawns() == 2, "the reviewer did not run once after the worker"


def test_lr_r1_the_round_count_is_unchanged(w):
    loop, wk, rv = worker_failed(w, prior_rejection=True)
    assert w.get(loop)["loop"]["rounds_rejected"] == 1
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["loop"]["rounds_rejected"] == 1
    assert done["loop"]["max_rounds"] == 3


def test_lr_r1_the_hold_is_cleared(w):
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    try:
        node = w.get(loop)
        assert node["state"] == "running", node
        assert node["hold"] is None
        assert w.get(wk)["state"] != "held"
    finally:
        w.gate("gw", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)


def test_lr_r1_a_failed_reviewer_reruns_alone_the_worker_and_its_session_are_kept(w):
    loop, wk, rv = reviewer_failed(w)
    assert w.get(wk)["state"] == "done" and w.get(wk)["outcome"] == "completed"
    failed_session = w.fxr.calls()[-1]["session"]
    worker_spawns = w.fxw.spawns()
    reviewer_spawns = w.fxr.spawns()
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved"
    assert w.fxw.spawns() == worker_spawns, "the completed sibling was run again"
    assert w.fxr.spawns() == reviewer_spawns + 1
    assert w.fxr.calls()[-1]["resume"] == failed_session
    assert done["loop"]["rounds_rejected"] == 0


def test_lr_r1_the_reviewer_verdict_after_the_relaunch_is_honoured_a_rejection_counts(w):
    loop, wk, rv = worker_failed(w, max_rounds=1)
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("rejected", [finding("still wrong")]))
    assert relaunch_loop(w, loop).get("ok") is True
    held = w.wait_held(loop, "loop_max", timeout=WAIT_ROUNDS)
    assert held["loop"]["rounds_rejected"] == 1
    assert w.fxw.spawns() == 3 and w.fxr.spawns() == 1


def test_lr_r1_a_rejection_after_the_relaunch_starts_the_next_round(w):
    loop, wk, rv = worker_failed(w, max_rounds=3)
    w.fxw.queue(work(2), work(3))
    w.fxr.queue(verdict_entry("rejected", [finding("AGAIN-77")]), verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 1
    assert "AGAIN-77" in w.fxw.calls()[-1]["prompt"]


def test_lr_r1_max_rounds_is_optional_and_applied_when_given(w):
    loop, wk, rv = worker_failed(w, prior_rejection=True)
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("approved"))
    reply = relaunch_loop(w, loop, max_rounds=6)
    assert reply.get("ok") is True, reply
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["loop"]["max_rounds"] == 6 and done["loop"]["rounds_rejected"] == 1


def test_lr_r1_pins_apply_to_the_failed_child(wpin):
    loop, wk, rv = worker_failed(wpin)
    wpin.fxw.queue(work(2))
    wpin.fxr.queue(verdict_entry("approved"))
    reply = relaunch_loop(wpin, loop, pins={wk: {"model": "fxw/m2"}})
    assert reply.get("ok") is True, reply
    wpin.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    models = [c["model"] for c in wpin.fxw.calls()]
    assert models[:2] == ["fxw/m1", "fxw/m1"] and models[2] == "fxw/m2", models


def test_lr_r1_it_is_root_only_and_checks_the_revision(w):
    loop, wk, rv = worker_failed(w)
    from multiagents.scheduler import issue_run_capability
    run = issue_run_capability(w.root, "some-run", loop, {"read", "delegate"})
    rev = w.get(loop)["revision"]
    assert err_code(w.rpc("relaunch_node", {"id": loop, "revision": rev}, token=run)) == "forbidden"
    assert err_code(w.rpc("relaunch_node", {"id": loop, "revision": rev - 1})) == "conflict"
    w.quiet(1)
    assert w.get(loop)["state"] == "held" and w.fxw.spawns() == 2


def test_lr_r1_a_repeated_relaunch_launches_one_rerun(w):
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    rev = w.get(loop)["revision"]
    first = w.rpc("relaunch_node", {"id": loop, "revision": rev})
    second = w.rpc("relaunch_node", {"id": loop, "revision": rev})
    try:
        assert first.get("ok") is True, first
        assert second.get("ok") is False or second.get("result") == first.get("result"), second
        w.wait_running(wk, timeout=WAIT)
        w.quiet(1)
        assert w.fxw.spawns() == 3, "one relaunch must start one rerun"
    finally:
        w.gate("gw", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)


def test_lr_r1_a_rerun_that_fails_again_holds_the_loop_again_and_can_be_relaunched(wbreaker):
    loop, wk, rv = worker_failed(wbreaker)
    wbreaker.fxw.queue(FAIL, work(2))        # the rerun fails; the scheduler's own retry is not repeated
    wbreaker.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(wbreaker, loop).get("ok") is True
    wbreaker.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert relaunch_loop(wbreaker, loop).get("ok") is True
    done = wbreaker.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved" and done["loop"]["rounds_rejected"] == 0


# ------------------------------------------------------------------ LR-R2

def test_lr_r2_relaunching_the_failed_worker_clears_the_loop_hold_and_the_round_continues(w):
    loop, wk, rv = worker_failed(w, prior_rejection=True)
    failed_session = w.fxw.calls()[-1]["session"]
    spawns_before = w.fxw.spawns()
    w.fxw.queue(work(2))
    w.fxr.queue(verdict_entry("approved"))
    assert w.root_op("relaunch_node", wk).get("ok") is True
    hold_clears(w, loop)
    done = w.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved"
    assert done["loop"]["rounds_rejected"] == 1
    assert done["hold"] is None
    assert w.fxw.spawns() == spawns_before + 1
    assert w.fxw.calls()[-1]["resume"] == failed_session
    assert w.fxr.spawns() == 2


def test_lr_r2_the_loop_is_no_longer_held_while_the_child_reruns(w):
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    assert w.root_op("relaunch_node", wk).get("ok") is True
    try:
        hold_clears(w, loop)
        w.wait_running(wk, timeout=WAIT)
        node = w.get(loop)
        assert node["state"] == "running" and node["hold"] is None, node
    finally:
        w.gate("gw", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)


def test_lr_r2_relaunching_the_failed_reviewer_keeps_the_worker_and_honours_the_verdict(w):
    loop, wk, rv = reviewer_failed(w, max_rounds=1)
    failed_session = w.fxr.calls()[-1]["session"]
    worker_spawns = w.fxw.spawns()
    w.fxr.queue(verdict_entry("rejected", [finding("nope")]))
    assert w.root_op("relaunch_node", rv).get("ok") is True
    hold_clears(w, loop)
    held = w.wait_held(loop, "loop_max", timeout=WAIT_ROUNDS)
    assert held["loop"]["rounds_rejected"] == 1
    assert w.fxw.spawns() == worker_spawns
    assert w.fxr.calls()[-1]["resume"] == failed_session


def test_lr_r2_pins_on_the_direct_child_relaunch_apply(wpin):
    loop, wk, rv = worker_failed(wpin)
    wpin.fxw.queue(work(2))
    wpin.fxr.queue(verdict_entry("approved"))
    reply = wpin.root_op("relaunch_node", wk, pins={"model": "fxw/m2"})
    if reply.get("ok") is not True:      # spelling of a child's own pins is not pinned
        reply = wpin.root_op("relaunch_node", wk, pins={wk: {"model": "fxw/m2"}})
    assert reply.get("ok") is True, reply
    hold_clears(wpin, loop)
    wpin.wait_state(loop, "done", timeout=WAIT_ROUNDS)
    assert wpin.fxw.calls()[-1]["model"] == "fxw/m2"


# ------------------------------------------------------------------ LR-R3

def test_lr_r3_relaunching_a_running_loop_is_still_refused_and_launches_nothing(w):
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = mkloop(w, 2)
    w.wait_running(wk, timeout=WAIT)
    try:
        reply = relaunch_loop(w, loop)
        assert reply.get("ok") is False, reply
        assert w.get(loop)["state"] == "running"
        w.quiet(1)
        assert w.fxw.spawns() == 1
    finally:
        w.gate("gw", w.fxw)
    assert w.wait_state(loop, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"


def test_lr_r3_a_loop_that_finished_the_failed_round_is_refused_until_held_again(w):
    """Once the hold is cleared the loop is running: a second relaunch is refused."""
    loop, wk, rv = worker_failed(w)
    w.fxw.queue({"gate": "gw", **work(1)})
    w.fxr.queue(verdict_entry("approved"))
    assert relaunch_loop(w, loop).get("ok") is True
    try:
        w.wait_running(wk, timeout=WAIT)
        assert relaunch_loop(w, loop).get("ok") is False
    finally:
        w.gate("gw", w.fxw)
    w.wait_state(loop, "done", timeout=WAIT_ROUNDS)


def test_lr_r3_a_loop_held_at_the_maximum_still_requires_a_larger_maximum(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("rejected", [finding("one")]))
    w.start_scheduler()
    loop, wk, rv = mkloop(w, 1)
    w.wait_held(loop, "loop_max", timeout=WAIT)
    assert err_code(relaunch_loop(w, loop, max_rounds=1)) == "invalid"
    assert w.get(loop)["state"] == "held"


def test_lr_r3_a_failed_child_outside_a_loop_is_not_affected(w):
    w.fxw.queue(FAIL)
    w.start_scheduler()
    n = w.simple("N", "wk")
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "failed"
    assert w.root_op("relaunch_node", n).get("ok") is True
