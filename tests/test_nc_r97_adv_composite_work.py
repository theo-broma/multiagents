"""Adversary, NC-R97 end to end: a loop whose work child is a composite.

A loop's mirror of a generation is written only when the integrating node's
direct parent is the loop (engine.py, integration). With
`loop = [sequence[wk], rv]` the loop never holds a generation: the reviewer's
launch reads `parent["generations"][-1]` (engine.py:433) and raises
IndexError inside the evaluation, every tick. NC-R97's gate for outside
consumers also requires the loop's own mirror, which never exists here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, blocked_codes, commit_entry, verdict_entry  # noqa: E402

WAIT = 8


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


def composite_loop(w):
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"))
    w.fxr.queue(verdict_entry("approved"))
    w.start_scheduler()
    wk = w.simple("W", "wk", prose="do the work")
    seq = w.comp("sequence", [wk])
    rv = w.simple("R", "rv", prose="review the work")
    loop = w.comp("loop", [seq, rv], loop={"verdict_child": rv, "max_rounds": 3})
    w.wait_state(wk, "done", timeout=WAIT)
    return loop, wk


def test_adv_a_loop_with_a_composite_work_child_reviews_and_releases_its_work_to_outside_consumers(w):
    loop, wk = composite_loop(w)
    cons = w.simple("CONS", "cons", inputs=[{"node": wk}])
    done = w.wait_state(loop, "done", timeout=3 * WAIT)
    assert done["outcome"] == "approved", done
    w.wait_state(cons, "done", timeout=2 * WAIT)


def test_adv_a_loop_with_a_composite_work_child_does_not_stall_unrelated_nodes(w):
    composite_loop(w)
    w.quiet(2)
    w.fxc.queue(commit_entry("other.txt", "x\n", "unrelated"))
    other = w.simple("OTHER", "cons", prose="unrelated work")
    w.wait_state(other, "done", timeout=2 * WAIT)
