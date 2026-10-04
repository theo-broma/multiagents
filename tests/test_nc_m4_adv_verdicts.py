"""M4 adversary: verdicts that outlive their activation, and generations that
become usable under an id other than the loop's (NC-R34, R35, R37, R69, R96).

Each test drives the real scheduler with the fixture agents of
`nc_fixture.m4_agent`; the scenarios are the ones the tester's files do not
walk: a verdict given by an activation that then fails, an input naming the
work child while the loop has no verdict, and a root close rewriting what the
reviewer said.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402

WAIT = 8
WAIT_ROUNDS = 20


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def test_adv_a_verdict_from_an_activation_that_then_failed_does_not_approve_the_generation(w):
    """The reviewer gives `approved` and exits 1; its retry crashes. The loop
    treats the round as having no verdict (it retried, then held run_failed),
    yet the generation keeps `approved`, and through the work child's id it is
    an eligible input of a node that depends on nothing else."""
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("approved", exit=1), {"crash": True})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    held = w.wait_held(loop, "run_failed", timeout=WAIT_ROUNDS)
    assert w.fxr.verdicts()[0]["replies"][-1]["ok"] is True   # the verdict was accepted
    assert w.fxr.spawns() == 2                               # and the loop still retried
    reader = w.simple("READER", "wk", inputs=[{"node": wk}])
    w.quiet(3)
    assert w.fxw.by_tag("READER") == [], (
        "a generation whose loop never approved it launched a dependent through the work child")
    assert held["generations"][-1]["verdict"] != "approved", (
        f"a held run_failed loop carries an approved generation: {held['generations']}")
    assert w.get(reader)["state"] == "open"


def test_adv_an_unreviewed_generation_is_not_usable_through_the_work_childs_id(w):
    """NC-R96: an input naming the work child blocks exactly as one naming the
    loop does. The loop is held `unresolved_round`; via the loop the input
    blocks, via the work child the unreviewed commit is handed out."""
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue({"text": "no verdict"})
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_held(loop, "unresolved_round", timeout=WAIT)
    via_loop = w.simple("VIALOOP", "wk", inputs=[{"node": loop}])
    via_child = w.simple("VIACHILD", "wk", inputs=[{"node": wk}])
    w.quiet(3)
    assert w.fxw.by_tag("VIALOOP") == []
    assert w.fxw.by_tag("VIACHILD") == [], (
        "the work child's unreviewed generation was used as an input while its loop blocks it")
    assert w.get(via_child)["state"] == w.get(via_loop)["state"] == "open"


def test_adv_close_approved_does_not_rewrite_the_reviewers_rejection(w):
    """NC-R34 records the reviewer's verdict on the generation; NC-R37 records a
    root `approved` close as the orchestrator's decision (`closed_by`). The
    close rewrites the reviewer's `rejected` on the generation (and on its
    mirror on the work child) to `approved`, so the record says the reviewer
    approved work it rejected."""
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("rejected", [finding("broken")]))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(1)
    held = w.wait_held(loop, "loop_max", timeout=WAIT)
    assert held["generations"][-1]["verdict"] == "rejected"
    assert w.root_op("close_node", loop, outcome="approved").get("ok") is True
    after = w.get(loop)
    assert after["closed_by"] == "root"
    assert after["generations"][-1]["verdict"] == "rejected", (
        f"the reviewer's verdict was overwritten by the close: {after['generations'][-1]}")
    assert [g["verdict"] for g in w.get(wk)["generations"]] == ["rejected"]
