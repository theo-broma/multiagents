"""Adversary round 3 (M1): a configuration edit after nodes exist.

Every write re-validates the whole plan against the current configuration, so
one node naming an agent that was since removed refuses every write. These
tests fail on 99cc7ae."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _orphan(live):
    gone = live.create(agent="other")
    keep = live.create()
    live.p.agents.pop("other")
    live.p.write()
    return gone, keep


def test_adv3_a_node_whose_agent_was_removed_can_still_be_cancelled(live):
    gone, _ = _orphan(live)
    reply = live.cancel_raw(gone["id"])
    assert reply["ok"] is True, reply
    assert live.get(gone["id"])["state"] == "cancelled"


def test_adv3_a_removed_agent_does_not_freeze_unrelated_nodes(live):
    _, keep = _orphan(live)
    reply = live.cancel_raw(keep["id"])
    assert reply["ok"] is True, reply
