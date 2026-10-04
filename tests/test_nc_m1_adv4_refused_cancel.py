"""Adversary round 4 (M1): a refused cancel leaves no transition behind.

Replaces tests/test_nc_m1_adv3_rpc.py::test_adv3_a_refused_cancel_leaves_no_transition_behind,
whose refusal (an unrelated node naming a removed agent) the config-drift fix
rightly no longer makes. Here the cancel is refused by NC-R6 for a reason that
stays: the cancelled subtree itself references an unknown node. The refusal
comes from whole-plan validation, after cancel_node has already queued its
`cancelled` transitions, so committing instead of rolling back the refused
transaction (mutant x01) leaves them in the stream. A refusal that comes
before anything is queued (a terminal node, a running descendant) cannot see
that mutant.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _host_write(live, node_id, **fields):
    from multiagents.scheduler.store import Store
    store = Store(live.root)
    with store.transaction() as db:
        record = store.nodes(db)[node_id]
        record.update(fields)
        store.save_node(db, record)


def test_adv4_a_cancel_refused_by_validation_leaves_no_transition_behind(live):
    a, b = live.create(), live.create()
    group = live.create(kind="group", children=[a["id"], b["id"]])
    # A dangling dependency inside the subtree being cancelled: NC-R6 refuses
    # the write only once the cancel has marked group, a and b cancelled.
    _host_write(live, b["id"], depends_on=[{"node": "nc-ghost", "require": "success"}])
    before = live.snapshot(), live.transitions(), live.event_kinds()
    reply = live.cancel_raw(group["id"])
    assert reply["ok"] is False and code(reply) == "invalid", reply
    assert (live.snapshot(), live.transitions(), live.event_kinds()) == before
    for node in (group, a, b):
        assert live.get(node["id"])["state"] == "open"

