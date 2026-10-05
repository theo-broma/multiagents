"""Adversary, NC-R97: ways a rejected or unjudged generation still reaches a consumer.

Orchestrator decision (2026-10-05, not yet in the contract text): a loop gates
approval only for consumers outside it; consumers inside may read pending
work; a rejected generation is blocked for every consumer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from multiagents.scheduler import control  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def blocked(h, consumer):
    nodes = h.nodes()
    return [b["code"] for b in h.engine.structural(nodes[consumer["id"]], nodes)]


def commits(h):
    """c1 on main's tip, c2 descending from c1."""
    tip = h.world.main_tip()
    tree = h.results.git("rev-parse", tip + "^{tree}", check=True).out
    c1 = h.results.commit(tree, [tip], "loop work, generation 1")
    c2 = h.results.commit(tree, [c1], "later work on the same node branch")
    return c1, c2


def contains(h, commit, ancestor):
    return h.results.git("merge-base", "--is-ancestor", ancestor, commit).ok


def test_adv_root_closing_the_work_child_approved_hands_its_rejected_generation_to_an_inside_consumer(h):
    """loop L = [B = group[S, C], R]. Round 1: S's generation 1 is rejected by
    R (both mirrors say `rejected`); the round resets, S is `open` again. The
    root then closes S itself `approved` (close_node accepts an open node), and
    S's closure records generation 1. C, inside L, names S.

    The rejection is the loop's and nothing has overturned it: the loop is still
    running. Outside consumers are blocked; C is handed the rejected commit."""
    c1, _ = commits(h)
    s, c, r = h.record(), h.record(), h.record()
    group = h.record(kind="group", children=[s["id"], c["id"]])
    loop = h.record(kind="loop", children=[group["id"], r["id"]],
                    loop={"verdict_child": r["id"], "max_rounds": 3})
    group["parent"] = r["parent"] = loop["id"]
    s["parent"] = c["parent"] = group["id"]
    generation = {"seq": 1, "commit": c1, "run_id": "ag-111111", "verdict": "rejected"}
    s.update(generations=[dict(generation)])
    loop.update(state="running", generations=[dict(generation)])
    loop["loop"]["rounds_rejected"] = 1
    group["state"] = "running"
    c["inputs"] = [{"node": s["id"]}]
    outside = h.record(inputs=[{"node": s["id"]}])
    h.save(loop, group, s, c, r, outside)
    with h.service.store.transaction() as db:
        nodes = h.service.store.nodes(db)
        control.decide(h.service, db, "close_node",
                       {"id": s["id"], "revision": nodes[s["id"]]["revision"], "outcome": "approved"}, nodes)
    nodes = h.nodes()
    assert nodes[s["id"]]["state"] == "done" and nodes[s["id"]]["closure"]["generation"]["seq"] == 1
    assert nodes[loop["id"]]["generations"][0]["verdict"] == "rejected"
    assert blocked(h, outside) == ["input"]
    assert blocked(h, c) == ["input"], "an inside consumer is eligible on a generation its loop rejected"


def test_adv_inner_root_approval_overrides_the_outer_loops_rejection_for_a_consumer_inside_the_outer(h):
    """outer = [group[inner = [work, inner_rev], middle], outer_rev]. The root
    closed `inner` approved on generation G; `outer`'s reviewer rejected G.
    `middle` is inside outer, outside inner, and names `work`. A rejected
    generation is blocked for every consumer; middle gets G."""
    c1, _ = commits(h)
    work, inner_rev, outer_rev, middle = (h.record() for _ in range(4))
    inner = h.record(kind="loop", children=[work["id"], inner_rev["id"]],
                     loop={"verdict_child": inner_rev["id"], "max_rounds": 3})
    group = h.record(kind="group", children=[inner["id"], middle["id"]])
    outer = h.record(kind="loop", children=[group["id"], outer_rev["id"]],
                     loop={"verdict_child": outer_rev["id"], "max_rounds": 3})
    work["parent"] = inner_rev["parent"] = inner["id"]
    inner["parent"] = middle["parent"] = group["id"]
    group["parent"] = outer_rev["parent"] = outer["id"]
    group["state"] = "running"
    identity = {"seq": 1, "commit": c1, "run_id": "ag-222222"}
    work.update(state="done", outcome="completed", generations=[{**identity, "verdict": None}])
    inner.update(state="done", outcome="approved", closed_by="root",
                 closure={"by": "root", "outcome": "approved", "generation": dict(identity)},
                 generations=[{**identity, "verdict": None}])
    outer.update(state="running", generations=[{**identity, "verdict": "rejected"}])
    middle["inputs"] = [{"node": work["id"]}]
    outside = h.record(inputs=[{"node": work["id"]}])
    h.save(work, inner_rev, outer_rev, middle, inner, group, outer, outside)
    assert blocked(h, outside) == ["input"]
    assert blocked(h, middle) == ["input"], "the outer loop's rejection is overridden by the inner root approval"


def test_adv_a_composite_verdict_childs_commit_carries_unjudged_loop_work_to_an_outside_consumer(h):
    """loop L = [S, V = sequence[R1, R2]]. S's generation 1 (c1) is integrated
    and unjudged. R1, inside the verdict child, is not the loop's direct verdict
    child, so its commits integrate as an ordinary generation (c2, based on the
    node branch tip, which already contains c1). NC-R97 exempts the whole
    verdict-child subtree from L's gate, so an outside consumer naming R1 is
    eligible and launches on c2 -- i.e. on S's unreviewed work."""
    c1, c2 = commits(h)
    s, r1, r2 = h.record(), h.record(), h.record()
    verdict = h.record(kind="sequence", children=[r1["id"], r2["id"]])
    loop = h.record(kind="loop", children=[s["id"], verdict["id"]],
                    loop={"verdict_child": verdict["id"], "max_rounds": 3})
    s["parent"] = verdict["parent"] = loop["id"]
    r1["parent"] = r2["parent"] = verdict["id"]
    gen1 = {"seq": 1, "commit": c1, "run_id": "ag-333333", "verdict": None}
    s.update(state="done", outcome="completed", generations=[dict(gen1)])
    loop.update(state="running", generations=[dict(gen1)])
    verdict["state"] = "running"
    r1.update(state="done", outcome="completed",
              generations=[{"seq": 1, "commit": c2, "run_id": "ag-444444", "verdict": None}])
    outside = h.record(inputs=[{"node": r1["id"]}])
    h.save(loop, s, verdict, r1, r2, outside)
    assert blocked(h, outside) == ["input"] or not contains(
        h, h.results.prepare(h.nodes()[outside["id"]], h.nodes())["input_commit"], c1), (
        "an outside consumer launches on a commit containing an unjudged loop generation")


@pytest.mark.parametrize("verdict", [None, "rejected"])
def test_adv_a_sibling_on_the_shared_node_branch_carries_loop_work_to_an_outside_consumer(h, verdict):
    """group T = [L = [S, R], X]. S's generation (c1) is integrated into
    nodes/T before review. X, a plain sibling, then finishes: its generation
    c2 is a descendant of c1 (integration onto the shared branch). An outside
    consumer naming X is eligible and launches on c2, which contains S's
    unjudged (or rejected) work."""
    c1, c2 = commits(h)
    s, r, x = h.record(), h.record(), h.record()
    loop = h.record(kind="loop", children=[s["id"], r["id"]],
                    loop={"verdict_child": r["id"], "max_rounds": 3})
    top = h.record(kind="group", children=[loop["id"], x["id"]])
    s["parent"] = r["parent"] = loop["id"]
    loop["parent"] = x["parent"] = top["id"]
    top["state"] = "running"
    gen1 = {"seq": 1, "commit": c1, "run_id": "ag-555555", "verdict": verdict}
    s.update(state="done", outcome="completed", generations=[dict(gen1)])
    loop.update(state="running", generations=[dict(gen1)])
    x.update(state="done", outcome="completed",
             generations=[{"seq": 1, "commit": c2, "run_id": "ag-666666", "verdict": None}])
    outside = h.record(inputs=[{"node": x["id"]}])
    h.save(top, loop, s, r, x, outside)
    assert blocked(h, outside) == ["input"] or not contains(
        h, h.results.prepare(h.nodes()[outside["id"]], h.nodes())["input_commit"], c1), (
        "an outside consumer launches on a commit containing an unjudged/rejected loop generation")
