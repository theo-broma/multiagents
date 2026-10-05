"""NC-R97 — only an approved generation is usable as input.

An input naming a loop, or its work child (unpinned or pinned to a
`generation`), resolves only to a generation the loop has approved. A generation
not yet judged blocks the consumer with `input`, exactly as a rejected one does
(NC-R47, NC-R96). Closing the loop's root with `approved` counts as approval and
is recorded distinctly from a reviewer's verdict.

Two layers, both through the public surface:
- `Harness` (deterministic): node records laid out by hand, read through the
  engine's `structural` evaluation (the `blocked` codes of NC-R22), as
  tests/test_nc_m3_adv_inputs.py does.
- `M4World` (end to end): a real scheduler, fixture agents, a reviewer held at a
  gate so the review is pending for as long as the test wants.

The root-approved close is covered only in the deterministic layer: reaching a
closeable loop with an unjudged generation end to end needs `unresolved_round`
(NC-R34/R35, M4), which is itself still red in this tree
(tests/test_nc_m4_loops.py::test_nc_r35_a_verdict_child_that_ends_without_a_verdict_holds_the_round_unresolved).

What the contract does NOT say, and these tests therefore do not assert: the
name of any field that tells a root-approved close from a reviewer verdict on
the *generation*. The only documented marker is NC-R37's node-level
`closed_by: root`; that is asserted, and nothing else.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from nc_fixture.m4_world import (M4World, blocked_codes, commit_entry,  # noqa: E402
                                 verdict_entry)

WAIT = 8           # one launch / one transition
QUIET = 2          # ticks the scheduler gets to (wrongly) launch a blocked consumer


# =========================================================== deterministic layer

@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def loop_with(h, generations, *, loop_state="running", loop_hold=None, loop_outcome=None):
    """loop L = [impl, rev]. `generations` is a list of (commit, verdict);
    the same generations are mirrored on the loop and on its work child, with
    the verdict on the loop's copy, as a real round leaves them (NC-R33/R34).
    The work child is `done`; the review is whatever `loop_state` says."""
    tip = h.world.main_tip()
    impl, rev = h.record(), h.record()
    loop = h.record(kind="loop", children=[impl["id"], rev["id"]],
                    loop={"verdict_child": rev["id"], "max_rounds": 5})
    gens = [{"seq": i + 1, "commit": commit or tip, "run_id": f"ag-4000{i}0", "verdict": None}
            for i, (commit, _) in enumerate(generations)]
    impl.update(parent=loop["id"], state="done", outcome="completed", generations=gens)
    rev.update(parent=loop["id"], state="open")
    loop.update(state=loop_state, generations=[
        {**g, "verdict": verdict} for g, (_, verdict) in zip(gens, generations)])
    if loop_hold:
        loop["hold"] = {"reason": loop_hold, "detail": ""}
    if loop_outcome:
        loop["outcome"] = loop_outcome
    h.save(loop, impl, rev)
    return loop, impl


def codes(h, node_id_or_ref_owner, ref):
    consumer = h.record(inputs=[ref])
    h.save(consumer)
    nodes = h.nodes()
    return [b["code"] for b in h.engine.structural(nodes[consumer["id"]], nodes)]


def refs(loop, impl, pin):
    pinned = {"generation": pin} if pin else {}
    return {"loop": {"node": loop["id"], **pinned}, "work child": {"node": impl["id"], **pinned}}


@pytest.mark.parametrize("via", ["loop", "work child"])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_a_generation_whose_review_is_pending_is_not_usable(h, via, pin):
    loop, impl = loop_with(h, [(None, None)], loop_state="running")
    assert codes(h, via, refs(loop, impl, pin)[via]) == ["input"], (
        f"an unreviewed generation is an eligible input via the {via}")


@pytest.mark.parametrize("via", ["loop", "work child"])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_a_round_whose_reviewer_gave_no_verdict_is_not_usable(h, via, pin):
    loop, impl = loop_with(h, [(None, None)], loop_state="held", loop_hold="unresolved_round")
    assert codes(h, via, refs(loop, impl, pin)[via]) == ["input"]


@pytest.mark.parametrize("via", ["loop", "work child"])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_the_same_input_is_usable_once_the_reviewer_approves(h, via, pin):
    loop, impl = loop_with(h, [(None, "approved")], loop_state="done", loop_outcome="approved")
    assert codes(h, via, refs(loop, impl, pin)[via]) == []


@pytest.mark.parametrize("via", ["loop", "work child"])
def test_nc_r97_approval_of_the_latest_round_is_enough_before_the_loop_finishes(h, via):
    # Approved verdict recorded; the loop itself has not yet been marked done.
    loop, impl = loop_with(h, [(None, "approved")], loop_state="running")
    consumer_codes = codes(h, via, {"node": impl["id"]})
    assert via != "work child" or consumer_codes == [], consumer_codes


def test_nc_r97_a_pin_to_the_pending_generation_does_not_fall_back_to_an_older_approved_one(h):
    tip = h.world.main_tip()
    loop, impl = loop_with(h, [(tip, "approved"), (tip, None)], loop_state="running")
    assert codes(h, "child", {"node": impl["id"], "generation": 2}) == ["input"]
    assert codes(h, "child", {"node": impl["id"], "generation": 1}) == []


def test_nc_r97_unpinned_resolves_to_the_latest_approved_even_when_a_newer_one_is_pending(h):
    loop, impl = loop_with(h, [(None, "approved"), (None, None)], loop_state="running")
    assert codes(h, "child", {"node": impl["id"]}) == []


def test_nc_r97_a_rejected_then_pending_history_is_not_usable(h):
    loop, impl = loop_with(h, [(None, "rejected"), (None, None)], loop_state="running")
    assert codes(h, "child", {"node": impl["id"]}) == ["input"]
    assert codes(h, "loop", {"node": loop["id"]}) == ["input"]


def test_nc_r97_a_pin_to_a_generation_that_does_not_exist_blocks(h):
    loop, impl = loop_with(h, [(None, "approved")], loop_state="done", loop_outcome="approved")
    assert codes(h, "child", {"node": impl["id"], "generation": 7}) == ["input"]


def test_nc_r97_a_root_approved_close_makes_the_generation_usable_under_both_ids(h):
    # closed approved with no reviewer verdict: the generation's verdict is null
    loop, impl = loop_with(h, [(None, None)], loop_state="done", loop_outcome="approved")
    loop["closed_by"] = "root"
    h.save(loop)
    assert codes(h, "loop", {"node": loop["id"]}) == []
    assert codes(h, "child", {"node": impl["id"]}) == []
    assert codes(h, "child", {"node": impl["id"], "generation": 1}) == []


@pytest.mark.parametrize("outcome", ["exhausted", "failed"])
def test_nc_r97_a_close_that_is_not_approved_does_not_approve_anything(h, outcome):
    loop, impl = loop_with(h, [(None, None)], loop_state="done", loop_outcome=outcome)
    loop["closed_by"] = "root"
    h.save(loop)
    assert codes(h, "child", {"node": impl["id"]}) == ["input"]
    assert codes(h, "loop", {"node": loop["id"]}) == ["input"]


def test_nc_r97_a_plain_node_still_feeds_its_completed_result(h):
    # NC-R33: for a non-verdict node the generation is usable once its run is done.
    node = h.record()
    node.update(state="done", outcome="completed", generations=[
        {"seq": 1, "commit": h.world.main_tip(), "run_id": "ag-500000", "verdict": None}])
    h.save(node)
    assert codes(h, "plain", {"node": node["id"]}) == []


# ================================================================ end to end

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.fxc = world.provider("fxc")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    world.agent("cons", "fxc", writes=True)
    yield world
    world.close()


def work(n=1):
    return commit_entry(f"f{n}.txt", f"v{n}\n", f"round {n}")


def pending_round(w, reviewer_entry):
    """Scheduler up, loop [wk, rv] created, the work child done and the review
    in the state `reviewer_entry` scripts. Returns (loop, wk)."""
    w.fxw.queue(work(1))
    w.fxr.queue(reviewer_entry)
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_state(wk, "done", timeout=WAIT)
    return loop, wk


def consumer(w, ref):
    return w.simple("CONS", "cons", inputs=[ref])


def refs_e2e(loop, wk, pin):
    pinned = {"generation": pin} if pin else {}
    return {"loop": {"node": loop, **pinned}, "work child": {"node": wk, **pinned}}


@pytest.mark.parametrize("via", ["loop", "work child"])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_end_to_end_the_consumer_waits_for_the_verdict_then_launches(w, via, pin):
    loop, wk = pending_round(w, {"gate": "review", **verdict_entry("approved")})
    cons = consumer(w, refs_e2e(loop, wk, pin)[via])
    w.quiet(QUIET)
    node = w.get(cons)
    assert node["state"] == "open" and "input" in blocked_codes(node), node
    assert w.fxc.spawns() == 0, "the consumer was launched against unreviewed work"
    w.fxr.open_gate("review")
    w.wait_state(cons, "done", timeout=WAIT)
    assert w.fxc.spawns() == 1


def test_nc_r97_end_to_end_a_reviewer_approval_is_not_recorded_as_the_roots(w):
    w.fxw.queue(work(1))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    done = w.wait_state(loop, "done", timeout=WAIT)
    assert done["outcome"] == "approved"
    assert done.get("closed_by") != "root", "a reviewer's approval is recorded as the orchestrator's"
    cons = consumer(w, {"node": wk})
    w.wait_state(cons, "done", timeout=WAIT)
