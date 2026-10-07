"""Adversary tests attacking the bug-2cebea fix (commit 39327a4).

Contract: context/specs/stuck-lifecycle.md (SL-R1..SL-R7).
These tests demonstrate defects in:
- Runner.consult() silent loss of reply text on free retry
- wait_for_any missing subsequent trips after an agent clears
- Opaque tools failing to clear stuck state when agent visibly moves on
- Stale trip reasons persisting on cleared running and successful done nodes
- Stuck nodes with pid=None permanently occupying concurrency slots
- wait_for_any hanging on dead stuck processes and reporting them as still running
- _preflight max_children counting dead stuck child nodes
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h
from multiagents.tree import Node
from multiagents.runner import _occupies_slot
from test_stuck_lifecycle import (
    fake_provider,
    make,
    tool,
    text,
    gate,
    open_gate,
    until,
    status,
    finish,
    dead_process,
)


def test_consult_free_retry_loses_reply_text(tmp_path, monkeypatch):
    """Defect 1: Runner._finalize() hands retried.done = run.done to maintain
    continuity for waiters. But Runner._consult_turn() captures `run` before
    the retry, and upon awakening reads `reply = "\\n".join(run.text_parts).strip()`.
    Because `run` is the dead first attempt that had no output, consult() returns
    an empty reply (''), silently discarding the second attempt's response."""
    prov, probe = fake_provider(tmp_path, [
        [["exit", 1]],
        [text("Hello! Here is the detailed response you asked for."), ["exit", 0]],
    ])
    spec = h.AgentSpec("advisor", "p", "m", conversational=True)
    r = make(tmp_path, monkeypatch, {"p": prov}, agents={"advisor": spec})

    async def go():
        return await r.consult("advisor", "Can you help me?", timeout=15)

    result = asyncio.run(go())
    assert (probe / "invocations").read_text() == "2", "fixture: retry must have run"
    assert result.get("reply") != "", (
        f"consult() lost the retried agent's reply text: {result}"
    )


def test_wait_for_any_misses_second_trip_after_clearing(tmp_path, monkeypatch):
    """Defect 2 (SL-R5): 'The wait still returns when an agent becomes stuck
    during the call (the documented finishes or gets stuck), so a new trip is
    never missed.'
    However, wait_for_any records `baseline_stuck` as a static set at entry.
    If an agent is stuck at start, clears to running (SL-R3), and then trips
    again, `agent_id in baseline_stuck` is still True!
    classify() treats it as baseline_stuck, does not include it in `changed`,
    and the new trip is missed."""
    plan = [
        tool("loop1"), tool("loop1"),
        gate("start_wait"),
        tool("diff_tool", x=1),
        tool("loop2"), tool("loop2"),
        gate("end"),
        text("done"),
        ["exit", 0],
    ]
    prov, probe = fake_provider(tmp_path, [plan])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        assert await until(lambda: status(r, agent) == "stuck", 20)

        # Start waiting while stuck (agent enters baseline_stuck)
        wait_task = asyncio.create_task(r.wait_for_any([agent], timeout=10))
        await asyncio.sleep(1.0)

        # Release agent: it executes diff_tool (clearing stuck), then loop2 (tripping again)
        open_gate(probe, "start_wait")

        # The wait should wake up on the second trip
        try:
            res = await asyncio.wait_for(wait_task, timeout=5.0)
            return agent, res
        finally:
            open_gate(probe, "end")
            await r.stop(agent)

    agent, res = asyncio.run(go())
    assert not res.get("timed_out"), f"Wait timed out, missing second trip: {res}"
    changed = {c["agent_id"]: c for c in res.get("changed", [])}
    assert agent in changed, f"Agent not reported in changed: {res}"
    assert changed[agent]["status"] == "stuck"


def test_opaque_tool_fails_to_clear_stuck(tmp_path, monkeypatch):
    """Defect 3 (SL-R3): 'stuck clears when the agent visibly moves on. A live
    node in stuck returns to running on the first of: a tool call whose loop
    signature differs from the call that tripped'.
    When an agent calls an opaque tool (such as view_file in agy),
    Supervisor.observe() sets signature=None and skips updating last_digest.
    _maybe_clear_stuck() compares supervisor.last_digest == run.trip_signature
    and fails to clear stuck! The agent remains stuck despite moving on."""
    plan = [
        tool("grep_it", q="a"), tool("grep_it", q="a"),
        gate("a"),
        tool("peek", path="foo.py"),
        gate("b"),
        ["exit", 0],
    ]
    prov, probe = fake_provider(tmp_path, [plan], opaque_tools=["peek"])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        try:
            assert await until(lambda: status(r, agent) == "stuck", 20)
            open_gate(probe, "a")
            await asyncio.sleep(2.0)
            return agent, status(r, agent)
        finally:
            open_gate(probe, "b")
            await r.stop(agent)

    agent, st = asyncio.run(go())
    assert st == "running", (
        f"Calling opaque tool 'peek' failed to clear stuck state: {st}"
    )


def test_cleared_run_leaves_stuck_reason_on_running_and_done(tmp_path, monkeypatch):
    """Defect 4 (SL-R2 & SL-R3): When stuck clears back to running,
    _maybe_clear_stuck calls tree.set_status(node_id, 'running') with reason=''.
    Tree.set_status only assigns node['reason'] if reason is non-empty, so
    node.reason remains the old trip message.
    When the agent finishes cleanly (exit 0), _finalize calls
    set_status(node_id, 'done', '') which again does not clear node.reason.
    A successful finished run is permanently labeled with 'doom_loop: ...'."""
    plan = [
        tool("read_it"), tool("read_it"),
        gate("a"),
        tool("other_tool", x=1),
        gate("b"),
        text("All work completed successfully!"),
        ["exit", 0],
    ]
    prov, probe = fake_provider(tmp_path, [plan])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        assert await until(lambda: status(r, agent) == "stuck", 20)
        open_gate(probe, "a")
        assert await until(lambda: status(r, agent) == "running", 20)
        running_reason = r.tree.get(agent).reason

        open_gate(probe, "b")
        await finish(r, agent)

        done_node = r.tree.get(agent)
        return running_reason, done_node.status, done_node.reason

    running_reason, done_status, done_reason = asyncio.run(go())
    assert "doom_loop" not in running_reason, (
        f"Running node after clear still has stuck reason: {running_reason}"
    )
    assert done_status == "done"
    assert "doom_loop" not in done_reason, (
        f"Done node falsely displays stuck reason: {done_reason}"
    )


def test_stuck_node_with_no_pid_occupies_slot_and_blocks_start(tmp_path, monkeypatch):
    """Defect 5 (SL-R4): 'A node occupies a concurrency slot if and only if
    it is pending, running, or stuck and live. A finished node never occupies
    a slot, whatever its label.'
    However, _occupies_slot implements:
    `return node.pid is None or procs.alive(node.pid, node.pid_start)`
    For any stuck node with pid=None, it returns True!
    It occupies a concurrency slot and blocks start_agent when max_concurrent is reached."""
    prov, _ = fake_provider(tmp_path, [[["exit", 0]]])
    r = make(tmp_path, monkeypatch, {"p": prov}, limits={"max_concurrent": 1})

    r.tree.add(Node(id="ag-nopid", agent="worker", provider="p", model="m",
                    parent=None, depth=1, status="stuck",
                    reason="doom_loop: read_it called 5x", pid=None, pid_start=""))

    node = r.tree.get("ag-nopid")
    assert not _occupies_slot(node), "Stuck node with pid=None must not occupy a slot"
    assert r.capacity()["running"] == 0, f"Capacity counted pid=None node: {r.capacity()}"

    async def go():
        return await r.start("worker", "go")

    res = asyncio.run(go())
    assert res.get("agent_id")


def test_wait_for_any_hangs_on_dead_stuck_process(tmp_path, monkeypatch):
    """Defect 6 (SL-R4 & SL-R5): While wait_for_any checks _occupies_slot at
    the start, its internal polling loop classify() never checks liveness.
    If a live stuck process is killed/dies during the wait, wait_for_any
    fails to notice, hangs until timeout, and reports timed_out: True with
    the dead process in still_running."""
    prov, probe = fake_provider(tmp_path, [[tool("a"), tool("a"), gate("block")]])
    r = make(tmp_path, monkeypatch, {"p": prov})

    async def go():
        agent = (await r.start("worker", "go"))["agent_id"]
        assert await until(lambda: status(r, agent) == "stuck", 20)

        wait_task = asyncio.create_task(r.wait_for_any([agent], timeout=3.0))
        await asyncio.sleep(0.5)

        run = r.runs[agent]
        os.kill(run.handle.pid, signal.SIGKILL)

        res = await wait_task
        return agent, res

    agent, res = asyncio.run(go())
    assert not res.get("timed_out"), f"wait_for_any timed out waiting on dead process: {res}"
    assert agent not in res.get("still_running", []), (
        f"Dead process reported as still running: {res}"
    )


def test_preflight_max_children_counts_dead_stuck_child(tmp_path, monkeypatch):
    """Defect 7 (SL-R4): _preflight checks sibling limits with:
    `siblings = [c for c in self.tree.children_of(parent) if c.status in {'pending', 'running', 'stuck'}]`
    It does not check _occupies_slot(c). Consequently, a dead stuck child
    permanently counts towards max_children and blocks subagent spawning."""
    prov, _ = fake_provider(tmp_path, [[["exit", 0]]])
    child_spec = h.AgentSpec("child", "p", "m", max_children=1)
    parent_spec = h.AgentSpec("parent", "p", "m", can_spawn=True)

    r = make(tmp_path, monkeypatch, {"p": prov},
             agents={"parent": parent_spec, "child": child_spec},
             limits={"max_children": 1, "max_concurrent": 5})

    r.tree.add(Node(id="ag-parent01", agent="parent", provider="p", model="m",
                    parent=None, depth=1, status="running", pid=os.getpid(),
                    children=["ag-child01"]))

    pid, start = dead_process()
    r.tree.add(Node(id="ag-child01", agent="child", provider="p", model="m",
                    parent="ag-parent01", depth=2, status="stuck",
                    reason="doom_loop: something", pid=pid, pid_start=start))

    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-parent01")
    monkeypatch.setenv("MULTIAGENTS_AGENT_DEPTH", "1")
    monkeypatch.setenv("MULTIAGENTS_CAN_SPAWN", "1")

    child_node = r.tree.get("ag-child01")
    assert not _occupies_slot(child_node), "Dead child must not occupy a slot"

    async def spawn():
        return await r.start("child", "do task")

    res = asyncio.run(spawn())
    assert res.get("agent_id"), f"Spawn failed: {res}"
