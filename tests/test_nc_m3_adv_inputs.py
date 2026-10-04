"""Adversary, M3: inputs bound to a generation the plan did not approve
(NC-R22 `input`, NC-R33, NC-R47 "rejected generation as input").

A loop's round integrates its work child's result as a generation on the child
AND mirrors it on the loop; the verdict is written only on the loop's copy.
The same commit is therefore "rejected" through the loop's id and "usable"
through the child's id.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def rejected_round(h):
    """loop L = [impl, rev]; round 1: impl integrated gen 1, rev rejected it;
    L held unresolved_round — the state composites() leaves behind."""
    tip = h.world.main_tip()
    impl, rev = h.record(), h.record()
    loop = h.record(kind="loop", children=[impl["id"], rev["id"]],
                    loop={"verdict_child": rev["id"], "max_rounds": 3})
    generation = {"seq": 1, "commit": tip, "run_id": "ag-444444", "verdict": None}
    impl.update(parent=loop["id"], state="done", outcome="completed", generations=[generation])
    rev.update(parent=loop["id"], state="done", outcome="completed")
    loop["loop"]["rounds_rejected"] = 1
    loop.update(state="held", hold={"reason": "unresolved_round", "detail": ""},
                generations=[{**generation, "verdict": "rejected"}])
    h.save(loop, impl, rev)
    return loop, impl


@pytest.mark.parametrize("pin", [None, 1])
def test_adv_the_rejected_generation_is_not_usable_through_the_work_childs_id(h, pin):
    loop, impl = rejected_round(h)
    ref = {"node": impl["id"], **({"generation": pin} if pin else {})}
    reader = h.record(inputs=[ref])
    h.save(reader)
    nodes = h.nodes()
    # Through the loop, the same commit is refused, as NC-R47 asks:
    via_loop = h.record(inputs=[{"node": loop["id"]}])
    assert h.engine.structural(via_loop, {**nodes, via_loop["id"]: via_loop})[0]["code"] == "input"
    blocked = h.engine.structural(nodes[reader["id"]], nodes)
    assert blocked and blocked[0]["code"] == "input", (
        f"a generation its loop rejected is an eligible input via the child: {blocked}")
