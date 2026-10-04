"""Adversary, M3: a read between the eligibility check and the claim it
authorises (NC-R22, NC-R33 reopening, NC-R59).

The tick decides a node is ready from a snapshot, awaits Runner's admission
probe, then claims. During that await the RPC thread can reopen the node's
dependency or input (`relaunch_node`). The claim transaction re-checks only the
node's own state and revision.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def root_request(h, op, args, request_id):
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    return h.service.request({"op": op, "token": token, "args": args, "request_id": request_id})


@pytest.mark.parametrize("edge", ["depends_on", "inputs"])
def test_adv_dependency_reopened_during_admission_probe_does_not_launch_the_dependent(h, monkeypatch, edge):
    """Sequence: A done/completed with generation 1; B open, `edge` -> A.
    tick: B ready -> await probe; meanwhile root relaunch_node(A) (A open);
    probe admits -> claim. Expected: B not claimed (blocked dependency/input),
    and the tick does not crash."""
    tip = h.world.main_tip()
    a = h.record()
    a.update(state="done", outcome="completed",
             generations=[{"seq": 1, "commit": tip, "run_id": "ag-000000", "verdict": None}])
    b = h.record(**{edge: [{"node": a["id"]}]})
    h.save(a, b)

    reopened = []

    async def start(*args, **kwargs):
        if not reopened:
            reply = root_request(h, "relaunch_node",
                                 {"id": a["id"], "revision": h.nodes()[a["id"]]["revision"]}, "rl-1")
            assert reply.get("ok"), reply
            reopened.append(reply)
        return {"admitted": True}

    spawned = []

    def spawn(attempt):
        spawned.append(attempt)
        h.engine.stopped.set()

    monkeypatch.setattr(h.engine.runner, "start", start)
    monkeypatch.setattr(h.engine, "spawn", spawn)
    asyncio.run(h.engine.tick())
    assert reopened, "the probe was never reached; the race was not exercised"
    assert h.nodes()[a["id"]]["state"] == "open"
    claimed = [x for x in h.journal().values() if x["node_id"] == b["id"]]
    assert claimed == [] and spawned == [], (
        f"B was claimed on a reopened {edge} edge: {claimed}")
