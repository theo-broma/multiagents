"""NC-R97 gates external consumers while leaving loop work and review runnable."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402


@pytest.mark.parametrize("location", ["verdict", "group_sibling"])
@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_internal_consumers_can_read_unjudged_work(tmp_path, monkeypatch, location, recorded, pin):
    h = Harness(tmp_path, monkeypatch)
    try:
        work, reviewer = h.record(), h.record()
        internal = reviewer if location == "verdict" else h.record()
        branch = work if location == "verdict" else h.record(
            kind="group", children=[work["id"], internal["id"]])
        loop = h.record(kind="loop", children=[branch["id"], reviewer["id"]],
                        loop={"verdict_child": reviewer["id"], "max_rounds": 3})
        branch.update(parent=loop["id"])
        reviewer.update(parent=loop["id"])
        if location == "group_sibling":
            branch.update(state="running")
            work.update(parent=branch["id"])
            internal.update(parent=branch["id"])
        generation = {"seq": 1, "commit": h.world.main_tip(),
                      "run_id": "ag-123456", "verdict": None}
        work.update(state="done", outcome="completed", generations=[dict(generation)])
        loop.update(state="running", generations=[dict(generation)] if recorded else [])
        ref = {"node": work["id"], **({"generation": pin} if pin else {})}
        internal["inputs"] = [ref]
        outside = h.record(inputs=[ref])
        h.save(loop, branch, work, reviewer, internal, outside)

        def blocked(consumer):
            nodes = h.nodes()
            return [b["code"] for b in h.engine.structural(nodes[consumer["id"]], nodes)]

        assert blocked(internal) == []
        assert blocked(outside) == ["input"]
        nodes = h.nodes()
        prepared = h.results.prepare(nodes[internal["id"]], nodes)
        assert prepared["input_commit"] == generation["commit"]
        # The internal exception concerns pending review, never rejection.
        loop["generations"] = [{**generation, "verdict": "rejected"}]
        h.save(loop)
        assert blocked(internal) == ["input"]
        assert blocked(outside) == ["input"]
    finally:
        h.close()


@pytest.mark.parametrize("inner_approved", [False, True])
@pytest.mark.parametrize("outer_approved", [False, True])
def test_nc_r97_nested_loops_gate_only_consumers_outside_each_loop(
        tmp_path, monkeypatch, inner_approved, outer_approved):
    h = Harness(tmp_path, monkeypatch)
    try:
        work, inner_reviewer, outer_reviewer, middle = (h.record() for _ in range(4))
        inner = h.record(kind="loop", children=[work["id"], inner_reviewer["id"]],
                         loop={"verdict_child": inner_reviewer["id"], "max_rounds": 3})
        group = h.record(kind="group", children=[inner["id"], middle["id"]])
        outer = h.record(kind="loop", children=[group["id"], outer_reviewer["id"]],
                         loop={"verdict_child": outer_reviewer["id"], "max_rounds": 3})
        work["parent"] = inner_reviewer["parent"] = inner["id"]
        inner["parent"] = middle["parent"] = group["id"]
        group["parent"] = outer_reviewer["parent"] = outer["id"]
        group["state"] = "running"
        generation = {"seq": 1, "commit": h.world.main_tip(),
                      "run_id": "ag-123456", "verdict": None}
        work.update(state="done", outcome="completed", generations=[dict(generation)])
        inner.update(state="running", generations=[
            {**generation, "verdict": "approved" if inner_approved else None}])
        outer.update(state="running", generations=[
            {**generation, "verdict": "approved" if outer_approved else None}])
        ref = {"node": work["id"]}
        inner_reviewer["inputs"] = middle["inputs"] = [ref]
        outside = h.record(inputs=[ref])
        h.save(work, inner_reviewer, outer_reviewer, middle, inner, group, outer, outside)
        nodes = h.nodes()
        for consumer, available in ((inner_reviewer, True), (middle, inner_approved),
                                    (outside, inner_approved and outer_approved)):
            blocked = h.engine.structural(nodes[consumer["id"]], nodes)
            assert [b["code"] for b in blocked] == ([] if available else ["input"])
            if available:
                assert h.results.prepare(nodes[consumer["id"]], nodes)["input_commit"] == generation["commit"]
    finally:
        h.close()
