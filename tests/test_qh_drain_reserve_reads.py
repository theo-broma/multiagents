"""`_qh_drain_reserve` parses `tree.json` per await taken, not per queued request.

It used to call `_qh_propose()` and `tree.get(...)` for every queued request,
and each of those parses the whole file (4.3 MB on a live tree): about 2N
parses per adoption pass for N requests, every five seconds.

Pinned here:

* with no queued request a pass costs at most one `Tree.read`;
* with 1 and 50 queued requests the reads stay within the first, one per await
  the pass took, and one after its single write — they are not 2N;
* a change made to the tree during an await is still seen by the next request,
  which is why the snapshot is dropped after every await.

The production `_qh_drain_reserve` runs against an object holding a real
`Tree` and the few collaborators it calls; a full `Runner` would add a config
and budget readers without adding any behaviour under test.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents.paths import ProjectPaths                    # noqa: E402
from multiagents.quota_handover import QuotaHandover           # noqa: E402
from multiagents.tree import Node, Tree                        # noqa: E402


class _Handover:
    _qh_drain_reserve = QuotaHandover._qh_drain_reserve
    _qh_nodes = QuotaHandover._qh_nodes
    _qh_idle = QuotaHandover._qh_idle
    _qh_propose = QuotaHandover._qh_propose
    _qh_request_state = QuotaHandover._qh_request_state

    def __init__(self, tree, on_await=None):
        self.tree = tree
        self.gate = SimpleNamespace(closed=False)
        self.config = SimpleNamespace(agents={"worker": object()})
        self.awaits = 0
        self.events = []
        self.on_await = on_await

    def _qh_reserved(self): return "reserve"
    def _qh_now(self): return 0
    def _qh_settings(self): return {}
    def _qh_names(self, roster): return ["alpha"]
    def _usable_spec(self, roster, name): return object()
    def _qh_event(self, node, kind, *a, **kw): self.events.append((kind, kw.get("request_id")))

    async def _qh_budgets(self): return {}

    async def _qh_usable(self, name, spec, readings, node=None, *, floor=False, data=None):
        self.awaits += 1
        await asyncio.sleep(0)
        if self.on_await:
            self.on_await(self)
        return False


def _tree(tmp_path, nodes, queue):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    paths.tree_file.write_text(json.dumps({"nodes": nodes, "quota_reserve": queue}))
    return tree


def _node(agent_id, status="completed"):
    return asdict(Node(id=agent_id, agent="worker", provider="alpha", model="m",
                       parent="", depth=0, status=status))


def _request(i, node_id=""):
    return {"id": f"reserve-{i:03d}", "agent": "worker", "task": "t", "node_id": node_id,
            "kwargs": {}, "state": "queued", "queued_at": 0, "readings": {}}


def _count_reads(monkeypatch):
    reads = []
    real = Tree.read

    def counting(self):
        reads.append(1)
        return real(self)

    monkeypatch.setattr(Tree, "read", counting)
    return reads


@pytest.mark.parametrize("queued", [0, 1, 50])
def test_drain_parses_the_tree_per_await_not_per_request(tmp_path, monkeypatch, queued):
    tree = _tree(tmp_path, {}, [_request(i) for i in range(queued)])
    handover = _Handover(tree)
    reads = _count_reads(monkeypatch)

    assert asyncio.run(handover._qh_drain_reserve()) == []

    assert handover.awaits == queued
    if queued == 0:
        assert len(reads) <= 1
    else:
        # The first read, one per await, and one after the pass's own write
        # (the single request moved to `requested`).
        assert len(reads) <= 1 + handover.awaits + 1, (
            f"{len(reads)} parses for {queued} queued request(s) and {handover.awaits} await(s)")
        print(f"queued={queued} awaits={handover.awaits} reads={len(reads)}")
    states = [r["state"] for r in json.loads(tree.path.read_text())["quota_reserve"]]
    assert states == (["requested"] + ["queued"] * (queued - 1))[:queued]
    assert [e[0] for e in handover.events] == ["reserve_request"] * min(queued, 1)


def test_a_change_made_during_an_await_is_seen_by_the_next_request(tmp_path):
    nodes = {"ag-1": _node("ag-1"), "ag-2": _node("ag-2")}
    tree = _tree(tmp_path, nodes, [_request(1, "ag-1"), _request(2, "ag-2")])

    def cancel_second(handover):
        if handover.awaits == 1:
            data = json.loads(tree.path.read_text())
            data["nodes"]["ag-2"]["status"] = "cancelled"
            tree.path.write_text(json.dumps(data))

    handover = _Handover(tree, on_await=cancel_second)
    asyncio.run(handover._qh_drain_reserve())

    states = {r["id"]: r["state"] for r in json.loads(tree.path.read_text())["quota_reserve"]}
    assert states == {"reserve-001": "requested", "reserve-002": "cancelled"}
    assert handover.awaits == 1, "the cancelled request must not have been probed"
