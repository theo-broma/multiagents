"""Adversary tests for P0-R6: `wait_for_agents` under pauses and interleavings.

Attacks:
- Pause expiring mid-wait must not be reported as active (stale pause latch).
- Pause enacted mid-wait must be reported on completion (missed pause).
- `retry_after_seconds` must reflect remaining duration upon return, not start.
- `still_running` must not report agents that no longer exist in the tree.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402


@pytest.fixture
def runner(tmp_path, monkeypatch):
    return h.make_runner(tmp_path, monkeypatch, git=False)


def _running(r, agent_id: str) -> None:
    r.tree.add(h.Node(id=agent_id, agent="worker", provider="p", model="m",
                      parent=None, depth=1))
    r.tree.set_status(agent_id, "running")


def test_pause_expiring_during_wait_is_not_reported_as_paused(runner):
    """When a pause expires while waiting on an agent, the returned result must

    not claim `paused: True`. Reporting an expired pause tricks the orchestrator
    into unnecessary waiting and halts scheduling.
    """
    _running(runner, "ag-a")
    # Pause expires in 0.5s
    runner.tree.pause(until=time.time() + 0.5, reason="brief quota pause")

    async def finish_later():
        await asyncio.sleep(1.2)
        runner.tree.set_status("ag-a", "done", "finished")

    async def go():
        task = asyncio.create_task(finish_later())
        res = await runner.wait_for_any(["ag-a"], timeout=10.0)
        await task
        return res

    result = asyncio.run(go())
    assert [c["agent_id"] for c in result.get("changed", [])] == ["ag-a"]
    assert result.get("paused") in (None, False), (
        f"P0-R6.3: pause expired during wait but was reported as active: {result}"
    )
    assert result.get("retry_after_seconds") is None, (
        f"retry_after_seconds reported for expired pause: {result}"
    )


def test_pause_set_during_wait_is_reported_when_wait_completes(runner):
    """When a pause is enacted while waiting on an agent, the returned result must

    report `paused: True` so the caller knows the system is paused.
    """
    _running(runner, "ag-a")
    # Initially NOT paused

    async def pause_and_finish():
        await asyncio.sleep(0.4)
        runner.tree.pause(until=time.time() + 60.0, reason="quota wall hit mid-wait")
        await asyncio.sleep(0.8)
        runner.tree.set_status("ag-a", "done", "finished")

    async def go():
        task = asyncio.create_task(pause_and_finish())
        res = await runner.wait_for_any(["ag-a"], timeout=10.0)
        await task
        return res

    result = asyncio.run(go())
    assert [c["agent_id"] for c in result.get("changed", [])] == ["ag-a"]
    assert result.get("paused") is True, (
        f"P0-R6.3: pause was enacted mid-wait but wait_for_any returned without paused: {result}"
    )
    assert result.get("reason") == "quota wall hit mid-wait"
    assert result.get("retry_after_seconds") is not None and result["retry_after_seconds"] > 0


def test_pause_retry_after_seconds_reflects_elapsed_wait_time(runner):
    """`retry_after_seconds` must be recomputed upon return, not frozen at call start."""
    _running(runner, "ag-a")
    pause_until = time.time() + 10.0
    runner.tree.pause(until=pause_until, reason="waiting on reset")

    async def finish_later():
        await asyncio.sleep(2.0)
        runner.tree.set_status("ag-a", "done", "finished")

    async def go():
        task = asyncio.create_task(finish_later())
        res = await runner.wait_for_any(["ag-a"], timeout=10.0)
        await task
        return res

    result = asyncio.run(go())
    assert result.get("paused") is True
    retry = result.get("retry_after_seconds")
    assert retry is not None
    # 2 seconds elapsed of a 10s pause, so remaining must be <= 8.5s
    assert retry <= 8.5, (
        f"retry_after_seconds was frozen from start ({retry}s) instead of reflecting remaining time"
    )


def test_still_running_excludes_agents_deleted_from_tree(runner):
    """An agent removed from the tree during the wait must not appear in still_running."""
    _running(runner, "ag-a")
    _running(runner, "ag-b")

    async def side():
        await asyncio.sleep(0.4)
        with runner.tree.transaction() as data:
            data["nodes"].pop("ag-b", None)
        await asyncio.sleep(0.6)
        runner.tree.set_status("ag-a", "done", "finished")

    async def go():
        task = asyncio.create_task(side())
        res = await runner.wait_for_any(["ag-a", "ag-b"], timeout=10.0)
        await task
        return res

    result = asyncio.run(go())
    assert [c["agent_id"] for c in result.get("changed", [])] == ["ag-a"]
    assert "ag-b" not in result.get("still_running", []), (
        f"ag-b was deleted from tree but reported as still_running: {result}"
    )
