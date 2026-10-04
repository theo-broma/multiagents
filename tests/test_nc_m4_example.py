"""M4 — NC-R46, the user's example, end to end with fake agents.

`implement` template: tests rejected once then approved (test_rounds 2);
implementation rejected until `impl_rounds` (3) -> `loop_max` notified; root
relaunches with another model for implementer-C and a raised maximum; approved;
reviewer-B is one provider session across both loops; `done` notified;
`merge_node` merges into main.

Assumptions where the contract is silent (kept loose): see `test_nc_m4_loops.py`
(prompt spelling of the generation, verdict arguments); `merge_node` merges the
loop branch squashed into the current branch of the project (`HEAD`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402


# Bounds on every wait: red tests fail on their own assertion within seconds.
WAIT = 8            # one launch / one transition
WAIT_ROUNDS = 30    # several activations in a row (loops, sessions)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxt = world.provider("fxt")
    world.fxr = world.provider("fxr")
    world.fxi = world.provider("fxi")
    for role, prov in (("tester", "fxt"), ("reviewer", "fxr"), ("implementer", "fxi")):
        world.agent(role, prov, writes=True)
    yield world
    world.close()


def test_nc_r46_the_user_example(w):
    w.fxt.queue(commit_entry("tests.txt", "t1\n", "tests 1"), commit_entry("tests.txt", "t2\n", "tests 2"))
    w.fxi.queue(*[commit_entry("impl.txt", f"impl {i}\n", f"impl {i}") for i in range(1, 5)])
    w.fxr.queue(
        verdict_entry("rejected", [finding("TESTS-GAP-1")]),     # tests loop, round 1
        verdict_entry("approved"),                                # tests loop, round 2
        verdict_entry("rejected", [finding("IMPL-BUG-1")]),      # impl loop
        verdict_entry("rejected", [finding("IMPL-BUG-2")]),
        verdict_entry("rejected", [finding("IMPL-BUG-3")]),      # -> loop_max (impl_rounds = 3)
        verdict_entry("approved"))                                # after the relaunch
    w.start_scheduler()
    top = w.instantiate_ok("implement", {
        "spec_path": "SPEC.md", "tests_task": "write the tests", "implement_task": "implement it",
        "tester": "tester", "reviewer": "reviewer", "implementer": "implementer"})
    tests_loop, impl_loop = w.children(top)
    impl_node = w.by_agent(impl_loop, "implementer")[0]
    reviewers = w.by_agent(top, "reviewer")
    assert len(reviewers) == 2

    held = w.wait_held(impl_loop, "loop_max", timeout=WAIT_ROUNDS)
    assert held["loop"]["rounds_rejected"] == 3
    assert w.get(tests_loop)["state"] == "done" and w.get(tests_loop)["outcome"] == "approved"
    assert w.get(tests_loop)["loop"]["rounds_rejected"] == 1
    assert w.fxt.spawns() == 2 and w.fxi.spawns() == 3 and w.fxr.spawns() == 5
    root_view = w.get(top)
    assert root_view["state"] == "held", "a held child holds the sequence (NC-R5)"
    assert w.transitions(impl_loop).count("loop_max") == 1
    ts = w.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.5})["transitions"]
    assert any(t["kind"].removeprefix("node.") == "loop_max" for t in ts), "loop_max not notified to root"

    # findings reached the implementer, once each
    prompts = [c["prompt"] for c in w.fxi.calls()]
    assert "IMPL-BUG-1" in prompts[1] and "IMPL-BUG-1" not in prompts[2]
    assert "IMPL-BUG-2" in prompts[2]

    reply = w.root_op("relaunch_node", impl_loop, max_rounds=5,
                      pins={impl_node: {"model": "fxi/m2"}})
    assert reply.get("ok") is True, reply
    final = w.wait_state(top, "done", timeout=WAIT_ROUNDS)
    assert final["outcome"] == "approved"
    assert w.get(impl_loop)["outcome"] == "approved" and w.get(impl_loop)["loop"]["rounds_rejected"] == 3
    assert [c["model"] for c in w.fxi.calls()] == ["fxi/m1"] * 3 + ["fxi/m2"]
    assert w.fxr.spawns() == 6 and w.fxi.spawns() == 4

    # reviewer-B: one provider session and one stable path across both loops
    calls = w.fxr.calls()
    assert all(c["resume"] == calls[0]["session"] for c in calls[1:]), "the alias did not keep its session"
    assert calls[0]["resume"] is None
    assert len({c["cwd"] for c in calls}) == 1
    assert {c["model"] for c in calls} == {"fxr/m1"}, "the model change applies to implementer-C, not the alias"

    # notified, then merged into main
    kinds = [t["kind"].removeprefix("node.") for t in
             w.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.5})["transitions"]
             if t.get("node_id") == top]
    assert "done" in kinds
    merged = w.root_op("merge_node", top)
    assert merged.get("ok") is True, merged
    assert w.show("HEAD:impl.txt") == "impl 4\n" and w.show("HEAD:tests.txt") == "t2\n"
