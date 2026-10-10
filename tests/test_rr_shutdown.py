"""RR-R4: nested-server shutdown respects host attempt ownership.

Exercise the public Runner lifecycle used by the MCP server with a genuine
provider process and scheduler. Transport signals and quota handover are not exercised here; this file
tests the public shutdown operation the server invokes.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lr_loop_resume import make_world
from test_nc_m2_guard_recovery import local
from nc_fixture.agent import alive
from multiagents.config import load
from multiagents.runner import Runner

WAIT = 5


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


def test_rr_r4_nested_shutdown_leaves_scheduler_owned_process_and_completion_intact(local, monkeypatch):
    from test_nc_m2_guard_recovery import deposit, claim
    from multiagents.scheduler.results import Results
    from multiagents.scheduler.engine import save_attempt
    from nc_fixture.agent import task

    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    results = Results(world.paths, engine.service.configuration())
    attempt.update(results.prepare(node, {node["id"]: node}))
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    results.ensure_branch(node)
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-nested")
    monkeypatch.setenv("MULTIAGENTS_DEPTH", "1")

    async def exit_nested():
        nested = Runner(world.paths, load(world.paths, seed=False))
        try:
            started = await nested.start("worker", task("OWNED", gate="work"),
                                         launch_context=engine.context(node, attempt))
            assert started.get("agent_id"), started
            await asyncio.to_thread(world.until, lambda: world.fx.calls(), WAIT,
                                    what="scheduler-owned provider launch")
            pid = world.fx.calls()[0]["pid"]
            engine.launched(attempt, nested.tree.get(started["agent_id"]))
            await nested.shutdown(detach=False)
            assert alive(pid), "nested shutdown terminated scheduler-owned provider"
            monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)
            monkeypatch.delenv("MULTIAGENTS_DEPTH", raising=False)
            await engine.runner.adopt()
            world.gate("work")
            async def completed():
                await engine.reconcile()
                reply = engine.service.request({"op": "get_node", "args": {"id": node["id"]},
                    "token": world.root_token(), "request_id": "read-completion"})
                return reply["result"] if reply["result"]["state"] == "done" else None
            deadline = asyncio.get_running_loop().time() + WAIT
            done = None
            while asyncio.get_running_loop().time() < deadline and done is None:
                done = await completed()
                if done is None:
                    await asyncio.sleep(0.05)
            assert done and done["outcome"] == "completed", done
            assert len(done["runs"]) == 1 and done["runs"][0]["run_id"] == started["agent_id"]
        finally:
            world.gate("work")
            await nested.shutdown(detach=False)
            await engine.runner.shutdown(detach=False)

    asyncio.run(exit_nested())


def test_rr_r4_nested_shutdown_still_stops_its_own_legacy_run(w, monkeypatch):
    w.project["scheduler"]["enabled"] = False
    w.write_config()
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-nested")
    monkeypatch.setenv("MULTIAGENTS_DEPTH", "1")
    monkeypatch.setenv("MULTIAGENTS_CAN_SPAWN", "1")
    w.fxw.queue({"gate": "legacy"})

    async def exit_nested():
        nested = Runner(w.paths, load(w.paths, seed=False))
        try:
            reply = await nested.start("wk", "legacy work")
            assert reply.get("agent_id"), reply
            await asyncio.to_thread(w.until, lambda: w.fxw.calls(), WAIT,
                                    what="legacy provider")
            pid = w.fxw.calls()[0]["pid"]
            await nested.shutdown(detach=False)
            assert not alive(pid), "legacy provider survived its owning server's shutdown"
        finally:
            w.gate("legacy", w.fxw)
            await nested.shutdown(detach=False)

    asyncio.run(exit_nested())
