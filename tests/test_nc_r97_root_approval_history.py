"""NC-R97: root approval selects a generation without rewriting its verdict."""
import pytest

from multiagents.scheduler.model import create_record
from multiagents.scheduler.results import input_generation


@pytest.mark.parametrize("via", ["loop", "child"])
@pytest.mark.parametrize("recorded", [False, True])
def test_nc_r97_root_approval_applies_only_to_its_generation(via, recorded):
    child = create_record({"kind": "simple", "agent": "coder", "task": "work"}, "root")
    reviewer = create_record({"kind": "simple", "agent": "coder", "task": "review"}, "root")
    loop = create_record({"kind": "loop", "children": [child["id"], reviewer["id"]],
                          "loop": {"verdict_child": reviewer["id"], "max_rounds": 3}}, "root")
    generations = [{"seq": seq, "commit": "a" * 40, "run_id": f"work-{seq}",
                    "verdict": "rejected"} for seq in (1, 2)]
    loop.update(state="done", outcome="approved", closed_by="root", generations=generations)
    child.update(parent=loop["id"], state="done", outcome="completed",
                 generations=[{**g, "seq": g["seq"] + 10} for g in generations])
    # An explicit closure is authoritative even if a newer generation exists.
    approved_seq = 1 if recorded else 2
    if recorded:
        loop["closure"] = {"by": "root", "outcome": "approved",
                           "generation": {k: generations[0][k] for k in ("seq", "run_id", "commit")}}
    nodes = {n["id"]: n for n in (loop, child, reviewer)}
    node = loop if via == "loop" else child
    offset = 0 if via == "loop" else 10
    assert input_generation(node, {}, nodes)["seq"] == approved_seq + offset
    assert input_generation(node, {"generation": approved_seq + offset}, nodes) is not None
    assert input_generation(node, {"generation": 3 - approved_seq + offset}, nodes) is None
    assert all(g["verdict"] == "rejected" for n in (loop, child) for g in n["generations"])
