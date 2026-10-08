"""LR-R4: bounded, durable hold delivery without consuming the journal."""
from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from multiagents import server
from multiagents.scheduler.model import create_record
from multiagents.scheduler.rpc import Service
from multiagents.scheduler.store import issue_run_capability, root_capability
from multiagents.tree import Tree


@pytest.fixture
def waits(tmp_path, monkeypatch):
    snapshot = {"holds": [], "next_cursor": 0, "transitions": []}
    nodes = []
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")

    def rpc(op, args):
        if op == "list_nodes":
            return {"nodes": copy.deepcopy(nodes)}
        assert op == "wait_for_nodes"
        return copy.deepcopy(snapshot)

    async def native_wait(*args):
        return {"changed": [], "timed_out": True}

    monkeypatch.setattr(server, "_node_rpc", rpc)
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)

    def run():
        # Recreate the tree object just as a restarted MCP server does.
        return SimpleNamespace(
            config=SimpleNamespace(project={"scheduler": {"enabled": True}}),
            tree=Tree(tree.path, tree.events_path), self_id=lambda: None)

    def wait(ids=None):
        return asyncio.run(server._wait_with_holds(run(), ids, 0))

    return SimpleNamespace(snapshot=snapshot, nodes=nodes, tree=tree, run=run,
                           wait=wait, rpc=rpc)


def episode(waits, node="nd-held", seq=1):
    hold = {"node_id": node, "seq": seq, "mark": str(seq),
            "hold": {"reason": "run_failed", "detail": "retry"}}
    waits.snapshot.update(holds=[hold], next_cursor=seq)
    return hold


@pytest.mark.parametrize("cleared", ["open", "gone"])
def test_lr_r4_delivery_marks_are_pruned_and_identical_new_holds_are_delivered(waits, cleared):
    first = episode(waits)
    assert waits.wait()["changed"] == [first]
    assert waits.wait()["changed"] == []
    waits.snapshot.update(holds=[], next_cursor=2)
    waits.nodes[:] = [] if cleared == "gone" else [{"id": "nd-held", "children": [], "state": "open"}]
    assert waits.wait()["changed"] == []
    assert waits.tree.read()["wait_hold_marks"]["root"] == {}
    second = episode(waits, seq=3)
    assert second["hold"] == first["hold"]
    assert waits.wait()["changed"] == [second]
    assert waits.wait()["changed"] == []
    assert "next_cursor" not in waits.wait()


def test_lr_r4_marks_do_not_accumulate_with_removed_nodes(waits):
    for seq in range(1, 21):
        current = episode(waits, f"nd-{seq}", seq)
        assert waits.wait()["changed"] == [current]
        assert waits.tree.read()["wait_hold_marks"]["root"] == {current["node_id"]: str(seq)}


def test_lr_r4_outside_holds_do_not_claim_delivery(waits):
    outside = episode(waits)
    waits.nodes[:] = [{"id": "nd-requested", "children": []}]
    result = waits.wait(["nd-requested"])
    assert result["changed"] == []
    assert waits.tree.read()["wait_hold_marks"]["root"] == {}
    assert waits.wait()["changed"] == [outside]


def test_lr_r4_a_composite_wait_delivers_descendant_holds(waits):
    hold = episode(waits)
    waits.nodes[:] = [{"id": "nd-parent", "children": ["nd-held"]},
                      {"id": "nd-held", "children": []}]
    assert waits.wait(["nd-parent"])["changed"] == [hold]
    assert waits.wait(["nd-parent"])["changed"] == []


def test_lr_r4_concurrent_callers_claim_a_hold_only_once(waits, monkeypatch):
    hold = episode(waits)
    waits.nodes[:] = [{"id": "nd-held", "children": []}]
    barrier = threading.Barrier(2)

    def rpc(op, args):
        if op == "list_nodes":
            # Both callers have observed the unclaimed episode before either
            # reaches its durable delivery transaction.
            barrier.wait(timeout=5)
        return waits.rpc(op, args)

    monkeypatch.setattr(server, "_node_rpc", rpc)

    async def concurrent():
        return await asyncio.gather(*[
            server._wait_with_holds(waits.run(), ["nd-held"], 0) for _ in range(2)])

    results = asyncio.run(concurrent())
    assert [change for result in results for change in result["changed"]] == [hold]
    assert sum(bool(result.get("timed_out")) for result in results) == 1


def test_lr_r4_a_delayed_snapshot_cannot_prune_a_newer_delivery(waits, monkeypatch):
    first = episode(waits)
    assert waits.wait()["changed"] == [first]
    old = {"holds": [], "transitions": [], "next_cursor": 2}
    second = episode(waits, seq=3)
    assert waits.wait()["changed"] == [second]
    replies = [old, copy.deepcopy(waits.snapshot)]

    def rpc(op, args):
        assert op == "wait_for_nodes"
        return replies.pop(0) if replies else copy.deepcopy(waits.snapshot)

    monkeypatch.setattr(server, "_node_rpc", rpc)
    assert waits.wait()["changed"] == []
    assert waits.tree.read()["wait_hold_marks"]["root"] == {"nd-held": "3"}


def test_lr_r4_a_delayed_hold_cannot_resurrect_a_cleared_episode(waits, monkeypatch):
    episode(waits)
    assert waits.wait()["changed"]
    old = copy.deepcopy(waits.snapshot)
    waits.snapshot.update(holds=[], next_cursor=2)
    assert waits.wait()["changed"] == []
    replies = [old, copy.deepcopy(waits.snapshot)]

    def rpc(op, args):
        assert op == "wait_for_nodes"
        return replies.pop(0) if replies else copy.deepcopy(waits.snapshot)

    monkeypatch.setattr(server, "_node_rpc", rpc)
    assert waits.wait()["changed"] == []
    assert waits.tree.read()["wait_hold_marks"]["root"] == {}


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = Service(tmp_path, "now")
    service.store.initialize()
    monkeypatch.setattr(service, "configuration", lambda: SimpleNamespace(limits={}))
    return service


def save_hold(service, subject="root", parent=None):
    node = create_record({"kind": "simple", "agent": "worker", "task": "work",
                          "parent": parent}, subject)
    node.update(state="held", hold={"reason": "run_failed", "detail": "retry"})
    with service.store.transaction() as db:
        service.store.save_node(db, node)
        service.store.transition(db, "held", node["id"], node["hold"])
    return node


def test_lr_r4_acknowledgement_does_not_erase_a_current_hold_delivery(waits, service, tmp_path):
    node = save_hold(service)
    request = {"token": root_capability(tmp_path)}
    args = {"node_ids": [], "timeout": 0, "include_held_ids": True}
    before = service.wait(request, None, args)
    waits.snapshot.update(before)
    first = waits.wait()
    assert first["changed"] == before["holds"]
    assert "_held_node_ids" not in first
    with service.store.transaction() as db:
        service.store.set_meta(db, "ack", before["next_cursor"])
    after = service.wait(request, None, args)
    assert after["holds"] == []
    assert after["_held_node_ids"] == [node["id"]]
    assert after["next_cursor"] == before["next_cursor"]
    waits.snapshot.update(after)
    assert waits.wait()["changed"] == []
    # An in-flight pre-ack response has the same journal head. Its episode
    # was already delivered and must still be recognised across a reset.
    waits.snapshot.update(before)
    assert waits.wait()["changed"] == []
    assert waits.tree.read()["wait_hold_marks"]["root"] == {node["id"]: before["holds"][0]["mark"]}


def test_lr_r4_held_node_snapshot_does_not_leak_outside_the_callers_subtree(service, tmp_path):
    parent = create_record({"kind": "group"}, "root")
    mine = save_hold(service, "ag-owner", parent["id"])
    outsider = save_hold(service, "ag-owner")
    parent["children"] = [mine["id"]]
    with service.store.transaction() as db:
        service.store.save_node(db, parent)
    token = issue_run_capability(tmp_path, "ag-owner", parent["id"], {"read"})
    reply = service.wait({"token": token}, None, {
        "node_ids": [], "timeout": 0, "include_held_ids": True})
    assert reply["_held_node_ids"] == [mine["id"]]
    assert outsider["id"] not in str(reply)
