"""Adversary round 3 (M1): reads take a read transaction, so a host seam that
holds the write lock (capability issue, M2 transitions) does not stall them."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc  # noqa: E402,F401


@pytest.mark.parametrize("op,args", [("scheduler_status", {}), ("list_nodes", {}),
                                     ("get_node", None), ("list_templates", {}),
                                     ("wait_for_nodes", {"timeout": 0})])
def test_adv3_a_read_is_answered_while_a_host_write_transaction_is_open(live, op, args):
    from multiagents.scheduler.store import Store
    node = live.create()
    if args is None:
        args = {"id": node["id"]}
    token = live.root_token()
    with Store(live.root).transaction() as db:
        db.execute("UPDATE meta SET value=value WHERE key='ack'")
        started = time.monotonic()
        reply = live.rpc(op, args, token=token, timeout=10)
        elapsed = time.monotonic() - started
    assert reply["ok"] is True, reply
    assert elapsed < 5, elapsed
