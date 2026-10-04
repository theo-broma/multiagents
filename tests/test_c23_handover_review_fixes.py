"""C23 review regressions, including the two surviving copy/routing mutants."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World, TASK
from multiagents import config, procs, quota_handover as qh
from multiagents.executor.local import LocalExecutor
from multiagents.runner import Runner


@pytest.mark.parametrize("existing", [False, True])
def test_qh_r8_r30_concurrent_target_writer_is_preserved(tmp_path, monkeypatch, existing):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"first\n" + b"second\n" * 20000)
    if existing:
        target.write_bytes(b"first\n")
    changed = b"first\nconcurrent writer\n"
    original = qh.copy_chunk

    def write_concurrently(output, chunk):
        original(output, chunk)
        target.write_bytes(changed)

    monkeypatch.setattr(qh, "copy_chunk", write_concurrently)
    with pytest.raises(ValueError, match="target session changed"):
        qh.transfer_session(source, target)
    assert target.read_bytes() == changed
    assert source.read_bytes().startswith(b"first\nsecond\n")
    assert not list(tmp_path.glob(".handover-*"))


def test_qh_r10_quota_stop_guard_alone_prevents_ping_pong(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a", "other": "model-o"})
    w.set_plan("beta", quota=True)
    original = w.r._qh_candidates
    attempted = []

    async def without_other_guards(node, spec, readings):
        # Neutralise the cooldown AND the candidate-attempt ledger. A fresh,
        # optimistic reading must not reuse an instance that stopped this run.
        with w.r.tree.transaction() as data:
            data["cooldowns"] = {}
            attempt = data["nodes"][node.id].get("handover_attempt")
            if attempt:
                attempt["tried"] = []
        readings = {name: replace(entry, cooldown_until=0) for name, entry in readings.items()}
        return await original(w.r.tree.get(node.id), spec, readings)

    async def transfer_gate(attempt):
        attempted.append(attempt["to"])
        # Bound a mutant's repeated launches so its failure stays diagnostic.
        if attempt["to"] in w.r.tree.get(next(iter(w.r.runs))).quota_stops:
            raise RuntimeError("test guard: repeated exhausted instance")

    monkeypatch.setattr(w.r, "_qh_candidates", without_other_guards)
    monkeypatch.setattr(qh, "before_transfer", transfer_gate)
    asyncio.run(w.start())
    assert attempted == ["beta", "other"]
    assert [call["instance"] for call in w.calls()] == ["alpha", "beta", "other"]


def test_qh_r20_three_segments_and_resumed_turn_keep_totals(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a", "other": "model-o"})
    w.set_plan("beta", quota=True)

    async def scenario():
        result = await w.start(budget_tag="three-segments")
        aid = result["agent_id"]
        node = w.r.tree.get(aid)
        assert [s["provider"] for s in node.segments] == ["alpha", "beta", "other"]
        assert node.usage["cost_usd"] == pytest.approx(.3)
        assert [s["usage"]["input_tokens"] for s in node.segments] == [10, 10, 10]
        assert (await w.steer(aid))["steered"]
        node = w.r.tree.get(aid)
        assert [s["usage"]["cost_usd"] for s in node.segments] == [.1, .1, .2]
        assert node.usage["cost_usd"] == pytest.approx(.4)
        assert node.usage["input_tokens"] == 40
        assert w.r.tree.usage_for_tag("three-segments")["total"] == 48

    asyncio.run(scenario())


_CRASH_DRIVER = '''import asyncio, json, os, sys, time
from pathlib import Path
from pytest import MonkeyPatch
from multiagents import budget, config
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
async def main():
    paths = ProjectPaths(Path(sys.argv[1]))
    run = Runner(paths, config.load(paths, seed=False))
    aid_file, crash_file, gate = map(Path, sys.argv[2:5])
    if sys.argv[5] == "start":
        reserve = run._reserve_launch
        def reserve_target(aid, provider, token, executor):
            hold = reserve(aid, provider, token, executor)
            if provider == "beta":
                # The predecessor has ended. Preserve its docker identity
                # as it would stand until the LOCAL target pid is recorded.
                run.tree.update(aid, exec_identity={"kind":"docker", "container":"old-container"})
            return hold
        run._reserve_launch = reserve_target
        record = run._record_launched
        def crash_before_record(aid, hold, handle):
            if hold.provider == "beta":
                end = time.monotonic() + 8
                wrapper = paths.run_dir(aid) / "wrapper.pid"
                while time.monotonic() < end:
                    if wrapper.exists() and wrapper.read_text().strip() == str(handle.pid):
                        crash_file.write_text(json.dumps({"pid":handle.pid, "attempt":run.tree.get(aid).handover_attempt}))
                        os._exit(0)
                    time.sleep(.01)
                raise RuntimeError("target wrapper did not record itself")
            record(aid, hold, handle)
        run._record_launched = crash_before_record
        result = await run.start("worker", "Implement the assigned quota handover task")
        aid_file.write_text(result["agent_id"])
        await asyncio.sleep(15)
        raise RuntimeError("handover never reached crash point")
    await run.resume_deferred()
    aid = aid_file.read_text()
    assert run.tree.get(aid).exec_identity["kind"] == "local"
    gate.touch()
    end = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < end:
        if run.check(aid)["status"] == "done":
            await run.shutdown(detach=True)
            return
        await asyncio.sleep(.01)
    raise RuntimeError("recovered target did not finish")
with MonkeyPatch.context() as mp:
    mp.setattr(budget, "read_all", lambda *a, **kw: {n:budget.Budget(n,known=True,headroom=.8)
        for n in ("alpha","beta","reserve","other")})
    mp.setattr(budget, "read_provider", lambda n,*a,**kw: budget.Budget(n,known=True,headroom=0 if n=="alpha" else .8))
    asyncio.run(main())
'''


def test_qh_r10_restart_uses_durable_local_target_identity_after_docker_source(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    driver, aid_file, crash_file, gate = (tmp_path / name for name in (
        "driver.py", "agent-id", "crash.json", "release-target"))
    driver.write_text(_CRASH_DRIVER)
    w.set_plan("beta", tool_gate=str(gate))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    argv = [sys.executable, str(driver), str(w.paths.root), str(aid_file), str(crash_file), str(gate)]
    try:
        with (tmp_path / "first.log").open("w") as output:
            first = subprocess.run([*argv, "start"], env=env, stdout=output, stderr=output, timeout=20)
        assert first.returncode == 0, (tmp_path / "first.log").read_text()
        crash = json.loads(crash_file.read_text())
        assert crash["attempt"]["state"] == "launching"
        assert crash["attempt"]["target_exec_identity"] == {"kind": "local", "container": ""}
        with (tmp_path / "second.log").open("w") as output:
            second = subprocess.run([*argv, "restart"], env=env, stdout=output, stderr=output, timeout=20)
        assert second.returncode == 0, (tmp_path / "second.log").read_text()
        node = Runner(w.paths, config.load(w.paths, seed=False)).tree.get(aid_file.read_text())
        assert node.status == "done"
        assert node.handover_attempt["state"] == "completed"
        assert node.handover_attempt["attempt"] == crash["attempt"]["attempt"]
        assert [call["instance"] for call in w.calls()] == ["alpha", "beta"]
        assert len(w.events("handover_started")) == 1
    finally:
        gate.touch()
        if crash_file.exists():
            pid = json.loads(crash_file.read_text())["pid"]
            if procs.living(pid, procs.start_time(pid) or ""):
                from multiagents.executor.base import stop_wrapped
                stop_wrapped(w.paths.run_dir(aid_file.read_text()), pid, procs.start_time(pid) or "")


@pytest.mark.parametrize("seam", ["start", "registration", "launch_return"])
def test_qh_r7_r10_stop_during_launch_kills_target_and_keeps_cancelled(tmp_path, monkeypatch, seam):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    w.set_plan("alpha", quota=False)
    w.set_plan("beta", gate=str(tmp_path / "target-gate"))

    async def scenario():
        aid = (await w.start())["agent_id"]
        reached, release = asyncio.Event(), asyncio.Event()
        target_handles = []
        original_start = LocalExecutor.start

        async def start(executor, *args, **kwargs):
            handle = await original_start(executor, *args, **kwargs)
            if kwargs.get("provider") == "beta":
                target_handles.append(handle)
                if seam == "start":
                    reached.set()
                    await release.wait()
            return handle

        monkeypatch.setattr(LocalExecutor, "start", start)
        original_track = w.r._track_container_run

        async def track(run, executor):
            await original_track(run, executor)
            if run.provider.name == "beta" and seam == "registration":
                reached.set()
                await release.wait()

        monkeypatch.setattr(w.r, "_track_container_run", track)
        original_launch = w.r._launch

        async def launch(**kwargs):
            run = await original_launch(**kwargs)
            if run.provider.name == "beta" and seam == "launch_return":
                reached.set()
                await release.wait()
            return run

        monkeypatch.setattr(w.r, "_launch", launch)
        steer = asyncio.create_task(w.r.steer(aid, "Switch account", provider="beta"))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            stopped = await w.r.stop(aid)
            assert stopped["status"] == "cancelled"
            release.set()
            result = await asyncio.wait_for(steer, 8)
            assert not result.get("steered"), result
            node = w.r.tree.get(aid)
            assert node.status == "cancelled"
            assert node.handover_attempt["state"] == "failed"
            assert target_handles and not procs.living(target_handles[0].pid, target_handles[0].pid_start)
            assert not node.cleanup_hold
            await w.r.resume_deferred()
            assert w.r.tree.get(aid).status == "cancelled"
        finally:
            release.set()
            if not steer.done():
                await steer
            await w.r.stop(aid)

    asyncio.run(scenario())


def test_qh_r7_manual_transfer_does_not_block_another_agents_steer(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode="copy", models={"beta": "model-a"})
    w.set_plan("alpha", quota=False)

    async def scenario():
        first = (await w.start())["agent_id"]
        second = (await w.start())["agent_id"]
        reached, release = asyncio.Event(), asyncio.Event()

        async def gate(attempt):
            reached.set()
            await release.wait()

        monkeypatch.setattr(qh, "before_transfer", gate)
        steer = asyncio.create_task(w.r.steer(first, "Change account", provider="beta"))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            competing = await w.r.steer(first, "Duplicate switch", provider="beta")
            assert "in progress" in competing["error"]
            independent = await asyncio.wait_for(w.r.steer(second, "Independent turn"), 3)
            assert independent["steered"], independent
            release.set()
            assert (await steer)["steered"]
            await w.settle(first)
            await w.settle(second)
        finally:
            release.set()
            if not steer.done():
                await steer
            await w.r.stop(first)
            await w.r.stop(second)

    asyncio.run(scenario())
