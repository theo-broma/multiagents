"""TB-R3 snapshot wakes retain LR-R4's durable episode delivery."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

from multiagents import server
from multiagents.tree import Tree


def test_native_snapshot_hold_uses_the_durable_mark_even_beside_an_error(tmp_path, monkeypatch):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}),
                          tree=tree, self_id=lambda: None)
    hold = {"node_id": "nd-snapshot", "seq": 2, "mark": "2",
            "hold": {"reason": "run_failed"}, "agent_id": "ag-snapshot"}
    change = {**hold, "state": "held", "status": "failed"}

    async def inline_thread(fn, *args):
        return fn(*args)

    async def native_wait(*args):
        return {"holds": [hold], "changed": [change], "error": "lost_connection",
                "next_cursor": 2, "transitions": [{"node_id": "nd-snapshot", "kind": "held"}]}

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "holds": [], "transitions": [], "next_cursor": 1})
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    first = asyncio.run(server._wait_with_holds(run, None, 0))
    assert first["changed"] == [change]
    assert first["error"] == "lost_connection"
    assert first["next_cursor"] == 2
    assert first["transitions"] == [{"node_id": "nd-snapshot", "kind": "held"}]
    assert tree.read()["wait_hold_marks"]["root"] == {hold["node_id"]: hold["mark"]}

    async def quiet_wait(*args):
        return {"changed": [], "timed_out": True}

    # An older empty snapshot must not prune a native snapshot's newer claim.
    assert tree.read()["wait_hold_cursors"]["root"] == 2
    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "holds": [], "transitions": [], "next_cursor": 1})
    monkeypatch.setattr(server, "_wait_for_agents", quiet_wait)
    restarted = SimpleNamespace(config=run.config,
                                tree=Tree(tree.path, tree.events_path), self_id=lambda: None)
    assert asyncio.run(server._wait_with_holds(restarted, None, 0))["changed"] == []
    assert tree.read()["wait_hold_marks"]["root"] == {hold["node_id"]: hold["mark"]}
    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "holds": [copy.deepcopy(hold)], "transitions": [], "next_cursor": 2})
    assert asyncio.run(server._wait_with_holds(restarted, None, 0))["changed"] == []
    assert "node_hold_deliveries" not in tree.read()


def test_native_snapshot_claim_survives_an_older_observer_snapshot(tmp_path, monkeypatch):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    hold = {"node_id": "nd-snapshot", "seq": 2, "mark": "2",
            "hold": {"reason": "run_failed"}, "agent_id": "ag-snapshot"}
    node = {"id": hold["node_id"], "state": "open", "runs": []}

    async def run_wait(*args):
        return {"changed": [], "reason": "no active agents"}

    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}),
                          tree=tree, self_id=lambda: None, capacity=lambda: {},
                          wait_for_any=run_wait)

    def rpc(op, args):
        if op == "list_nodes":
            return {"nodes": [{**node, "state": "held", "hold": hold["hold"]}],
                    "holds": [hold], "_wait_hold_cursor": 2}
        if args["node_ids"]:
            return {"holds": [hold], "transitions": [], "next_cursor": 2}
        return {"holds": [], "transitions": [], "next_cursor": 1}

    async def native_wait(run, ids, timeout):
        return await server._wait_for_agents_and_nodes(run, {node["id"]: node}, timeout)

    monkeypatch.setattr(server, "_node_rpc", rpc)
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    first = asyncio.run(server._wait_with_holds(run, None, 1))
    assert first["changed"][0]["mark"] == "2"
    assert "_wait_hold_cursor" not in first
    assert "next_cursor" not in first
    assert tree.read()["wait_hold_marks"]["root"] == {node["id"]: "2"}
    assert tree.read()["wait_hold_cursors"]["root"] == 2

    async def quiet_wait(*args):
        return {"changed": [], "timed_out": True}

    monkeypatch.setattr(server, "_wait_for_agents", quiet_wait)
    assert asyncio.run(server._wait_with_holds(run, None, 0))["changed"] == []
    assert tree.read()["wait_hold_marks"]["root"] == {node["id"]: "2"}


def test_native_error_marks_a_returned_hold_without_a_matching_change(tmp_path, monkeypatch):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}),
                          tree=tree, self_id=lambda: None)
    hold = {"node_id": "nd-snapshot", "seq": 2, "mark": "2",
            "hold": {"reason": "run_failed"}, "agent_id": "ag-snapshot"}

    async def inline_thread(fn, *args):
        return fn(*args)

    async def native_wait(*args):
        return {"holds": [hold], "error": "lost_connection", "next_cursor": 2}

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "holds": [], "transitions": [], "next_cursor": 1})
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    first = asyncio.run(server._wait_with_holds(run, None, 0))
    assert first["error"] == "lost_connection"
    assert first["holds"] == [hold]
    assert tree.read()["wait_hold_marks"]["root"] == {hold["node_id"]: hold["mark"]}
    assert tree.read()["wait_hold_cursors"]["root"] == 2

    async def quiet_wait(*args):
        return {"changed": [], "timed_out": True}

    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "holds": [hold], "transitions": [], "next_cursor": 2})
    monkeypatch.setattr(server, "_wait_for_agents", quiet_wait)
    assert asyncio.run(server._wait_with_holds(run, None, 0))["changed"] == []
