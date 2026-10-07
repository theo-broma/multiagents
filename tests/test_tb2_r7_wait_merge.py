"""TB2-R7: a hold and a transition waking one wait keep the transition cursor."""
from __future__ import annotations

import asyncio
import types

from multiagents import server


def test_simultaneous_hold_and_transition_keep_next_cursor(monkeypatch):
    hold = {"node_id": "nd-h", "reason": "needs_info"}
    calls = []

    def rpc(method, params):
        calls.append(params)
        # Initial probe sees nothing; the next one sees the hold.
        holds = [] if len(calls) == 1 else [hold]
        return {"holds": holds, "transitions": [], "next_cursor": 99}

    async def wait(run, agent_ids, timeout):
        return {"changed": [{"node_id": "nd-t", "state": "done"}],
                "timed_out": False, "next_cursor": 7}

    monkeypatch.setattr(server, "_node_rpc", rpc)
    monkeypatch.setattr(server, "_wait_for_agents", wait)
    run = types.SimpleNamespace(config=types.SimpleNamespace(
        project={"scheduler": {"enabled": True}}))

    async def both_done(tasks, return_when):
        await asyncio.gather(*tasks)
        return set(tasks), set()

    monkeypatch.setattr(server.asyncio, "wait", both_done)
    result = asyncio.run(server._wait_with_holds(run, None, 5.0))
    assert result["next_cursor"] == 7
    assert result["holds"] == [hold]
    assert result["timed_out"] is False
    assert {"node_id": "nd-t", "state": "done"} in result["changed"]
    assert hold in result["changed"]
