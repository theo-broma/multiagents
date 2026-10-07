"""TB-R3 / review ag-c77b16: pending nodes must not create a 4 Hz poll."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from multiagents import server


class Journal:
    def __init__(self, available=True):
        self.node = {"id": "nd-pending", "state": "open", "runs": [],
                     "blocked": [{"code": "dependency"}]}
        self.available = available
        self.condition = threading.Condition()
        self.transitions = []
        self.calls = []
        self.closed = False

    def snapshot(self):
        return {self.node["id"]: dict(self.node)}

    def publish(self, state, runs):
        with self.condition:
            self.node = dict(self.node, state=state, runs=runs)
            self.transitions.append({"seq": len(self.transitions) + 1,
                                     "node_id": self.node["id"], "kind": state})
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def rpc(self, op, args):
        with self.condition:
            self.calls.append((op, dict(args), time.monotonic()))
            if op == "list_nodes":
                return {"nodes": [dict(self.node)]}
            assert op == "wait_for_nodes"
            if not self.available:
                return {"error": "scheduler_unavailable"}
            assert args["node_ids"] == [self.node["id"]]
            self.condition.wait_for(
                lambda: self.closed or len(self.transitions) > args["cursor"],
                timeout=args["timeout"])
            return {"transitions": self.transitions[args["cursor"]:],
                    "next_cursor": len(self.transitions)}


class RunWait:
    def __init__(self, original=False):
        self.runs = {}
        if original:
            self.runs["ag-original"] = SimpleNamespace(id="ag-original", status="running")
        self.tree = SimpleNamespace(
            active=lambda: [r for r in self.runs.values() if r.status == "running"],
            get=self.runs.get)
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.original_finished = asyncio.Event()
        self.calls = []

    def capacity(self):
        return {}

    async def wait_for_any(self, ids, timeout):
        self.calls.append(ids)
        if ids is None:
            if "ag-original" in self.runs:
                await self.original_finished.wait()
                return {"changed": [{"agent_id": "ag-original", "status": "done"}],
                        "still_running": [], "capacity": {},
                        "limit_notices": [{"key": "original"}]}
            return {"changed": [], "still_running": [], "capacity": {},
                    "reason": "no active agents", "limit_notices": [{"key": "initial"}]}
        self.started.set()
        await asyncio.wait_for(self.finished.wait(), timeout)
        return {"changed": [{"agent_id": ids[0], "status": self.runs[ids[0]].status}],
                "still_running": [], "capacity": {}, "limit_notices": [{"key": "native"}]}


def test_pending_node_blocks_on_journal_without_four_hz_list_calls(monkeypatch):
    journal = Journal()
    monkeypatch.setattr(server, "_node_rpc", journal.rpc)

    async def exercise():
        try:
            return await server._wait_for_agents_and_nodes(RunWait(), journal.snapshot(), 2.2)
        finally:
            journal.close()

    result = asyncio.run(exercise())
    assert result["timed_out"] is True
    assert result["pending_nodes"] == [{"node_id": "nd-pending",
                                         "blocked": [{"code": "dependency"}]}]
    assert len([c for c in journal.calls if c[0] == "list_nodes"]) <= 2, journal.calls
    waits = [args for op, args, _ in journal.calls if op == "wait_for_nodes"]
    assert waits and all(args["timeout"] > 0 for args in waits)


def test_unavailable_notifications_poll_with_backoff(monkeypatch):
    journal = Journal(available=False)
    monkeypatch.setattr(server, "_node_rpc", journal.rpc)

    async def exercise():
        return await server._wait_for_agents_and_nodes(RunWait(), journal.snapshot(), 3.2)

    result = asyncio.run(exercise())
    assert result["timed_out"] is True
    lists = [stamp for op, _, stamp in journal.calls if op == "list_nodes"]
    assert len(lists) <= 3, journal.calls
    assert len(lists) >= 2, "fallback did not refresh pending nodes"
    assert lists[1] - lists[0] >= 1.8, journal.calls


@pytest.mark.parametrize("original", [False, True], ids=["no-active-runs", "active-run"])
@pytest.mark.parametrize("status", ["failed", "awaiting_user"])
def test_launched_snapshot_run_uses_native_wait_without_node_polling(monkeypatch, original, status):
    journal = Journal()
    monkeypatch.setattr(server, "_node_rpc", journal.rpc)

    async def exercise():
        run = RunWait(original=original)
        waiting = asyncio.create_task(
            server._wait_for_agents_and_nodes(run, journal.snapshot(), 4))
        try:
            await asyncio.sleep(0.1)
            run.runs["ag-launched"] = SimpleNamespace(id="ag-launched", status="running")
            journal.publish("running", [{"run_id": "ag-launched"}])
            await asyncio.wait_for(run.started.wait(), 1)
            assert not waiting.done(), "launch alone returned the wait"
            lists_before = len([c for c in journal.calls if c[0] == "list_nodes"])
            await asyncio.sleep(0.6)
            assert len([c for c in journal.calls if c[0] == "list_nodes"]) == lists_before
            run.runs["ag-launched"].status = status
            # Parking has no scheduler transition: it must wake the run wait.
            run.finished.set()
            if status == "failed":
                await asyncio.sleep(0.15)
                assert not waiting.done(), "returned before the scheduler settled the node"
                journal.publish("done", [{"run_id": "ag-launched"}])
            result = await asyncio.wait_for(waiting, 0.5)
            assert result["changed"][0]["agent_id"] == "ag-launched"
            assert result["changed"][0]["status"] == status
            assert result["node_runs"] == {"nd-pending": "ag-launched"}
            assert result["still_running"] == (["ag-original"] if original else [])
            assert result["limit_notices"] == ([{"key": "native"}] if original else
                                               [{"key": "initial"}, {"key": "native"}])
            assert run.calls == [None, ["ag-launched"]]
            assert any(args["cursor"] == 1 for op, args, _ in journal.calls
                       if op == "wait_for_nodes"), "journal cursor did not advance past launch"
        finally:
            journal.close()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(exercise())


def test_simultaneous_native_and_original_completions_keep_both_notices(monkeypatch):
    journal = Journal()
    monkeypatch.setattr(server, "_node_rpc", journal.rpc)

    async def exercise():
        run = RunWait(original=True)
        waiting = asyncio.create_task(
            server._wait_for_agents_and_nodes(run, journal.snapshot(), 4))
        try:
            await asyncio.sleep(0.1)
            run.runs["ag-launched"] = SimpleNamespace(id="ag-launched", status="running")
            journal.publish("running", [{"run_id": "ag-launched"}])
            await asyncio.wait_for(run.started.wait(), 1)
            run.runs["ag-original"].status = "done"
            run.runs["ag-launched"].status = "awaiting_user"
            run.original_finished.set()
            run.finished.set()
            result = await asyncio.wait_for(waiting, 0.5)
            assert result["limit_notices"] == [{"key": "original"}, {"key": "native"}]
        finally:
            journal.close()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(exercise())
