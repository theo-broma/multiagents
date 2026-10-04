"""Adversary round 2 (M1, NC-R5/NC-R6): cycle detection must include the
implicit sequence edges that a composite's descendants inherit.

Property: a plan is accepted only if every node can eventually launch. By
NC-R5 child i of a sequence depends on child i-1, and a composite's
dependencies gate all its descendants, so a descendant of child i waits for
child i-1. A dependency from child i-1 back onto that descendant is a cycle
(NC-R6: "including implicit sequence edges and ancestor edges").
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _sequence_with_nested_second_child(live, kind="group"):
    first = live.create(task="first step")
    inner = live.create(task="inside the second step")
    second = live.create(kind=kind, children=[inner["id"]])
    seq = live.create(kind="sequence", children=[first["id"], second["id"]])
    return seq, first, second, inner


def test_adv2_dependency_on_a_descendant_of_a_later_sequence_step_is_a_cycle(live):
    """inner waits for `first` (via the sequence edge its parent inherits);
    first depending on inner can never be satisfied."""
    seq, first, second, inner = _sequence_with_nested_second_child(live)
    before = live.snapshot()
    reply = live.update_raw(first["id"], depends_on=[{"node": inner["id"]}])
    assert code(reply) == "invalid", (
        f"sequence {seq['id']} = [{first['id']}, {second['id']}[{inner['id']}]]; "
        f"{first['id']}.depends_on={inner['id']} was accepted: {reply}")
    assert live.snapshot() == before


def test_adv2_same_cycle_through_a_nested_sequence(live):
    """The same deadlock one level deeper: the inherited sequence edge comes
    from the grandparent."""
    first = live.create(task="first step")
    leaf = live.create(task="deep leaf")
    mid = live.create(kind="sequence", children=[leaf["id"]])
    second = live.create(kind="group", children=[mid["id"]])
    live.create(kind="sequence", children=[first["id"], second["id"]])
    reply = live.update_raw(first["id"], depends_on=[{"node": leaf["id"], "require": "finished"}])
    assert code(reply) == "invalid", reply


def test_adv2_cycle_refused_when_created_in_one_write(live):
    """Created the other way round: the dependency exists first and the
    sequence that closes the cycle is the refused write."""
    inner = live.create(task="inside the second step")
    first = live.create(task="first step", depends_on=[{"node": inner["id"]}])
    second = live.create(kind="group", children=[inner["id"]])
    before = live.snapshot()
    reply = live.create_raw({"kind": "sequence", "children": [first["id"], second["id"]]})
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before
