"""TB2-R4: preserve journal delivery and failures when combining waits."""
import asyncio
from types import SimpleNamespace

import pytest

from multiagents import server


def composite_wait(monkeypatch, replies, native_wait=None):
    calls = []

    def rpc(op, args):
        calls.append((op, args))
        return replies.pop(0)

    async def inline_thread(fn, *args):
        return fn(*args)

    monkeypatch.setattr(server, "_node_rpc", rpc)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    run = SimpleNamespace(wait_for_any=native_wait,
                          _idle_capacity_note=lambda: {"capacity": {"free_slots": 1}})
    return run, calls


def test_tb2_r4_returns_unacknowledged_initial_descendant_transition(monkeypatch):
    transition = {"node_id": "nd-child", "state": "done"}
    run, calls = composite_wait(monkeypatch, [
        {"next_cursor": 8, "transitions": [transition]},
    ])
    reply = asyncio.run(server._wait_for_composites(run, ["nd-parent"], [], 1))
    assert reply["changed"] == [transition]
    assert reply["next_cursor"] == 8
    assert reply["timed_out"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("error_source", ["node", "run"])
def test_tb2_r4_simultaneous_success_keeps_other_wait_error(monkeypatch, error_source):
    error = {"error": "unavailable"}
    success = {"changed": [{"agent_id": "run-child", "status": "done"}]}

    async def native_wait(ids, timeout):
        return error if error_source == "run" else success

    run, _ = composite_wait(monkeypatch, [
        {"next_cursor": 8, "transitions": []},
        error if error_source == "node" else
        {"next_cursor": 9, "transitions": [{"node_id": "nd-child", "state": "done"}]},
    ], native_wait)
    reply = asyncio.run(server._wait_for_composites(run, ["nd-parent"], ["run-child"], 1))
    assert reply["error"] == "unavailable"
    assert reply["changed"]


def test_tb2_r4_exception_during_pending_wait_cancellation_propagates(monkeypatch):
    async def native_wait(ids, timeout):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise ConnectionError("lost connection during cancellation")

    run, _ = composite_wait(monkeypatch, [
        {"next_cursor": 8, "transitions": []},
        {"error": "unavailable"},
    ], native_wait)
    with pytest.raises(ConnectionError, match="lost connection during cancellation"):
        asyncio.run(server._wait_for_composites(run, ["nd-parent"], ["run-child"], 1))
