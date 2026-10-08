"""LR-R1/R2: both changed nodes appear in the relaunch journal."""
import pytest

from test_lr_loop_resume import w, worker_failed


@pytest.mark.parametrize("target", ["loop", "child"])
def test_lr_r1_r2_relaunch_journals_the_child_and_loop_once(w, target):
    loop, child, _ = worker_failed(w)
    # Keep the resumed run behind a gate while inspecting the relaunch itself.
    w.fxw.queue({"gate": "lr-reopened"})
    try:
        assert w.root_op("relaunch_node", loop if target == "loop" else child)["ok"]
        assert w.transitions(child).count("reopened") == 1
        assert w.transitions(loop).count("reopened") == 1
    finally:
        w.gate("lr-reopened", w.fxw)
