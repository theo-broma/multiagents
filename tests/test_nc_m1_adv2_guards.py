"""Adversary round 2 (M1): guards for validation rules no M1 test exercised.

Each test here PASSES on 319ebab. Each one fails under a mutant of the
implementation that the whole M1 suite let survive (named in the docstring),
so these are the tests that make those lines protected. Rules from NC-R6,
NC-R59 and NC-R73; every refusal must also leave the plan unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc, problems  # noqa: E402,F401


def _refused(live, write):
    before = live.snapshot()
    seen = len(live.transitions())
    reply = write()
    assert code(reply) == "invalid", reply
    assert problems(reply), reply
    assert live.snapshot() == before
    assert len(live.transitions()) == seen
    return reply


def test_adv2_group_created_with_the_same_child_twice_is_invalid(live):
    """NC-R73. Mutant: drop the duplicate-child check in model.attach."""
    a = live.create(task="a")
    _refused(live, lambda: live.create_raw({"kind": "group", "children": [a["id"], a["id"]]}))


def test_adv2_group_updated_with_the_same_child_twice_is_invalid(live):
    a = live.create(task="a")
    group = live.create(kind="group", children=[a["id"]])
    _refused(live, lambda: live.update_raw(group["id"], children=[a["id"], a["id"]]))


def test_adv2_a_composite_cannot_adopt_a_cancelled_node(live):
    """NC-R6 (write to a cancelled node). Mutant: drop the terminal-child check."""
    stray = live.create(task="cancelled before adoption")
    assert live.cancel_raw(stray["id"])["ok"]
    _refused(live, lambda: live.create_raw({"kind": "group", "children": [stray["id"]]}))


def test_adv2_a_node_cannot_be_created_under_a_cancelled_parent(live):
    """NC-R6. Mutant: drop the terminal-parent check in model.attach."""
    group = live.create(kind="group")
    assert live.cancel_raw(group["id"])["ok"]
    _refused(live, lambda: live.create_raw(
        {"kind": "simple", "agent": "worker", "task": "late", "parent": group["id"]}))


def test_adv2_root_removing_a_child_detaches_it_on_both_sides(live):
    """NC-R59 (root edits `children`). Mutant: skip detaching removed children,
    which leaves containment inconsistent and refuses every removal."""
    a, b = live.create(task="a"), live.create(task="b")
    group = live.create(kind="group", children=[a["id"], b["id"]])
    reply = live.update_raw(group["id"], children=[a["id"]])
    assert reply.get("ok") is True, reply
    assert live.get(group["id"])["children"] == [a["id"]]
    assert live.get(b["id"])["parent"] is None
    assert live.get(a["id"])["parent"] == group["id"]


def test_adv2_loop_verdict_child_must_be_the_last_child(live):
    """NC-R6/NC-R35. Mutant: accept any child as verdict_child."""
    work, review = live.create(task="work"), live.create(task="review", agent="reviewer")
    _refused(live, lambda: live.create_raw(
        {"kind": "loop", "children": [work["id"], review["id"]],
         "loop": {"verdict_child": work["id"], "max_rounds": 2}}))


@pytest.mark.parametrize("task", [" ", "\t\n", "   \n  "])
def test_adv2_whitespace_only_task_is_invalid(live, task):
    """NC-R73. Mutant: `not task` instead of `not task.strip()`."""
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": task}))


@pytest.mark.parametrize("require", ["failed", "exhausted", "Success", ""])
def test_adv2_require_outside_the_three_values_is_invalid(live, require):
    """NC-R73. Mutant: one more value in the accepted set."""
    target = live.create(task="target")
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                            "depends_on": [{"node": target["id"], "require": require}]}))


@pytest.mark.parametrize("generation", [0, -1])
def test_adv2_input_generation_must_be_positive(live, generation):
    """NC-R4. Mutant: `< 0` instead of `< 1`."""
    source = live.create(task="source")
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                            "inputs": [{"node": source["id"], "generation": generation}]}))


@pytest.mark.parametrize("hours", ["09:00-24:30", "22:00-24:01"])
def test_adv2_a_window_cannot_end_past_midnight(live, hours):
    """NC-R38 (via NC-R6). Mutant: drop `eh == 24 and em != 0`."""
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                            "window": {"days": ["mon"], "ranges": [hours]}}))


@pytest.mark.parametrize("pins", [{"temperature": "0"}, {"model": ""}, {"model": 3}])
def test_adv2_pins_accept_only_model_effort_provider_strings(live, pins):
    """NC-R4. Mutant: one more key accepted in `pins`."""
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                            "pins": pins}))


@pytest.mark.parametrize("extra", [{"agent": "worker"}, {"task": "composites carry no task"},
                                   {"pins": {"model": "acme/m1"}}])
def test_adv2_agent_task_pins_are_refused_on_a_composite(live, extra):
    """NC-R4 (simple only). Mutant: drop the composite agent/task/pins check."""
    _refused(live, lambda: live.create_raw({"kind": "group", **extra}))


@pytest.mark.parametrize("urgent", [1, "yes", None])
def test_adv2_urgent_must_be_a_boolean(live, urgent):
    """NC-R4. Mutant: drop the bool check (1 == True would slip through)."""
    _refused(live, lambda: live.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                            "urgent": urgent}))
