"""Adversary round 3 (M1): update_node rules that no earlier test held.

Launch state (`runs`) is host-owned and arrives with M2; these tests write it
into the store directly, as test_nc_m1_hardening2 does for NC-R59.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _host_write(live, node_id, **fields):
    from multiagents.scheduler.store import Store
    store = Store(live.root)
    with store.transaction() as db:
        record = store.nodes(db)[node_id]
        record.update(fields)
        store.save_node(db, record)


@pytest.mark.parametrize("kind", ["simple", "group", "sequence"])
def test_adv3_loop_max_rounds_is_refused_on_a_node_that_is_not_a_loop(live, kind):
    if kind == "simple":
        node = live.create()
    else:
        node = live.create(kind=kind, children=[live.create()["id"]])
    before = live.snapshot()
    reply = live.update_raw(node["id"], loop={"max_rounds": 3})
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before


def test_adv3_a_session_cannot_be_changed_once_the_node_has_launched(live):
    node = live.create()
    _host_write(live, node["id"], template={"instance": "inst-1"}, session="A",
                runs=[{"id": "prior-activation"}])
    before = live.snapshot()
    reply = live.update_raw(node["id"], session="B")
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before


def test_adv3_a_launched_child_cannot_be_removed_from_its_composite(live):
    a, b = live.create(), live.create()
    group = live.create(kind="group", children=[a["id"], b["id"]])
    _host_write(live, a["id"], runs=[{"id": "prior-activation"}])
    before = live.snapshot()
    reply = live.update_raw(group["id"], children=[b["id"]])
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before


def test_adv3_a_launched_child_cannot_be_reordered_in_its_sequence(live):
    a, b = live.create(), live.create()
    seq = live.create(kind="sequence", children=[a["id"], b["id"]])
    _host_write(live, a["id"], runs=[{"id": "prior-activation"}])
    before = live.snapshot()
    reply = live.update_raw(seq["id"], children=[b["id"], a["id"]])
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before
