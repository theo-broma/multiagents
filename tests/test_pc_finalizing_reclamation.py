"""PC-R2a/R3f: distinguish a dead launch from an active post-mortem."""
from __future__ import annotations

import asyncio
import os
import signal

from test_pc_counting import cp, full, hold, pc, sv, w, world_with
from multiagents.executor.base import running


def test_dead_run_is_reclaimed_while_its_server_is_paused(cp):
    p = cp(1)
    root, other = p.server(), p.server()
    agent_id = root.start("held " + hold(p, "a", talk=True))
    pid = sv.read_pid(p.marker("a.pid"))
    os.kill(root.pid, signal.SIGSTOP)
    try:
        os.kill(pid, signal.SIGKILL)
        assert sv.wait_until(lambda: not running(p.tree.get(agent_id).pid,
                                                p.tree.get(agent_id).pid_start), 10)
        result = other.call("start_agent", 60, args={
            "agent": "worker", "task": "after " + hold(p, "b", talk=True)})
        assert result.get("agent_id") and not result.get("deferred"), result
        sv.read_pid(p.marker("b.pid"))
    finally:
        os.kill(root.pid, signal.SIGCONT)
    # A finalizer that resumes after reclamation cannot take the occupied
    # slot back. It finishes once the replacement releases capacity.
    root.call("wait_for_agents", 30, args={"timeout": 1})
    assert len(p.invocations()) == 2
    p.marker("b.go").touch()
    assert sv.wait_until(lambda: p.tree.get(agent_id).status not in
                         ("running", "stuck", "pending"), 20)
    assert len(p.invocations()) == 2


def test_active_postmortem_keeps_slot_after_process_exit(w, monkeypatch):
    gate = world_with(w)
    entered = asyncio.Event()
    release = asyncio.Event()
    finalize = w.runner._finalize

    async def blocked_finalize(*args):
        entered.set()
        await release.wait()
        return await finalize(*args)

    monkeypatch.setattr(w.runner, "_finalize", blocked_finalize)

    async def scenario():
        (agent_id,) = await full(w, gate)
        gate.open()
        await asyncio.wait_for(entered.wait(), 10)
        node = w.runner.tree.get(agent_id)
        assert not running(node.pid, node.pid_start)
        try:
            assert pc.deferred_for_pc(await w.start("worker", "wait for postmortem"))
        finally:
            release.set()
        await w.until(agent_id)

    asyncio.run(scenario())
