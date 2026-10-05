"""Adversary, NC-R97 + NC-R33 reopening: a root-approved loop relaunched.

NC-R97: a root-approved close "is recorded distinctly from a reviewer's verdict,
so the record shows who approved". Relaunch (control.py, engine.reset_round)
resets the loop but leaves `closed_by` and `closure` from the earlier close.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402

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


def test_adv_a_reviewer_approval_after_relaunch_is_not_recorded_as_the_roots(w):
    w.fxw.queue(commit_entry("f1.txt", "v1\n", "round 1"), commit_entry("f2.txt", "v2\n", "round 2"))
    w.fxr.queue(verdict_entry("rejected", [finding("one")]), verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(1)
    w.wait_held(loop, "loop_max", timeout=WAIT)
    assert w.root_op("close_node", loop, outcome="approved").get("ok")
    assert w.get(loop)["closed_by"] == "root"
    assert w.root_op("relaunch_node", loop, max_rounds=2).get("ok")
    done = w.wait_state(loop, "done", timeout=3 * WAIT)
    assert done["outcome"] == "approved"
    assert done["generations"][-1]["verdict"] == "approved"
    assert done.get("closed_by") != "root", (
        "the reviewer approved round 2, but the record still says the root closed it", done.get("closure"))
