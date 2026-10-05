"""NC-R97: finishing work cannot bypass a loop that has not recorded it."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("pin", [None, 1])
def test_nc_r97_finished_work_waits_for_its_loop_generation(tmp_path, monkeypatch, nested, pin):
    h = Harness(tmp_path, monkeypatch)
    try:
        work, reviewer = h.record(), h.record()
        branch = h.record(kind="sequence", children=[work["id"]]) if nested else work
        loop = h.record(kind="loop", children=[branch["id"], reviewer["id"]],
                        loop={"verdict_child": reviewer["id"], "max_rounds": 3})
        branch.update(parent=loop["id"])
        if nested:
            work.update(parent=branch["id"])
        reviewer.update(parent=loop["id"])
        generation = {"seq": 1, "commit": h.world.main_tip(),
                      "run_id": "ag-123456", "verdict": None}
        work.update(state="done", outcome="completed", generations=[generation])
        loop.update(state="running", generations=[])
        ref = {"node": work["id"], **({"generation": pin} if pin else {})}
        consumer = h.record(inputs=[ref])
        h.save(loop, branch, work, reviewer, consumer)

        def blocked():
            nodes = h.nodes()
            return [b["code"] for b in h.engine.structural(nodes[consumer["id"]], nodes)]

        # The child has finished, but the loop's mirrored record is not saved yet.
        assert blocked() == ["input"]
        loop["generations"] = [dict(generation)]
        h.save(loop)
        assert blocked() == ["input"]
        loop["generations"][0]["verdict"] = "approved"
        h.save(loop)
        assert blocked() == []
    finally:
        h.close()
