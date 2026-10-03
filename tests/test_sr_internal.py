"""SR: launch-gate and persisted-turn paths without a Docker daemon."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time

import pytest

import test_sr_steer_exit_race as h
from multiagents import gitops
from multiagents.executor.base import running, session_alive, stop_wrapped
from multiagents.executor.docker import DockerExecutor
from multiagents.runner import _Predecessor

w = h.w


def test_sr_r2_unknown_liveness_keeps_sfs_single_cleanup_owner(w, monkeypatch):
    h.up(w)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.1)

    async def go():
        aid = await h.running(w)
        capture = w.runner._steer_predecessor

        def unknown(node_id):
            old = capture(node_id)
            old.probe_raw = lambda: None
            return old
        monkeypatch.setattr(w.runner, "_steer_predecessor", unknown)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"] is False
        assert "confirmed dead" in result["error"]
        hold = w.runner._holds[aid]
        assert hold.steer is not None
        assert not hold.confirmed
        assert aid in w.runner._locks
        assert w.g.spawns() == 1
        assert not w.runner.startup.token_for("acme", aid)
        w.runner._settle_holds()
        assert w.runner._holds[aid] is hold
    asyncio.run(go())


@pytest.mark.parametrize("container_alive", [True, None])
def test_sr_r2_dead_docker_client_is_not_container_death(w, monkeypatch, container_alive):
    h.up(w)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.1)
    monkeypatch.setattr("multiagents.runner.running", lambda *args: False)
    monkeypatch.setattr("multiagents.runner.session_alive", lambda *args: False)
    predecessor = _Predecessor(captured=True, pid=123, pid_start="start",
                               probe_raw=lambda: container_alive)
    assert not asyncio.run(w.runner._steer_predecessor_dead(predecessor))


def test_sr_r2_docker_transport_exit_one_is_unknown_and_refuses_steer(w, monkeypatch):
    h.up(w)
    executor = DockerExecutor({"image": "test"}, paths=w.runner.paths)
    monkeypatch.setattr(executor, "inside", lambda: False)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.1)

    def transport(argv, **kwargs):
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "transport failed")
    monkeypatch.setattr("multiagents.executor.docker._run", transport)

    async def go():
        aid = await h.running(w)
        assert executor.wrapper_alive(aid) is False
        assert w.runner._raw_alive_probe(executor, aid)() is None
        capture = w.runner._steer_predecessor

        def predecessor(node_id):
            old = capture(node_id)
            old.probe_raw = w.runner._raw_alive_probe(executor, node_id)
            return old
        monkeypatch.setattr(w.runner, "_steer_predecessor", predecessor)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"] is False
        assert "confirmed dead" in result["error"]
        assert w.g.spawns() == 1
        assert w.runner._holds[aid].steer is not None
    asyncio.run(go())


def test_sr_r2b_free_retry_does_not_start_beside_a_live_wrapper(w, monkeypatch):
    h.up(w)
    w.g.set(steps=0, talk=False)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.1)

    async def go():
        aid = await w.started("worker", "original prompt")
        assert await h.pc.await_until(lambda: w.g.spawns() == 1, 10)
        # A verdict can be observed while the writer has not yet exited.
        # The retry must judge process death, rather than this file's presence.
        (h.run_dir(w, aid) / "exit_status").write_text("3\n")
        assert await h.pc.await_until(lambda: aid in w.runner._holds, 10)
        assert w.g.spawns() == 1
        assert h.pc.alive(w.g.pids()[0])
        assert w.node(aid).retries == 1
        assert w.runner._holds[aid].steer is not None
        assert "confirm" in w.runner._holds[aid].steer["steps"]
    asyncio.run(go())


def test_sr_r2b_free_retry_waits_then_relaunches_the_original_prompt(w):
    h.up(w)
    w.g.set(gate=None, steps=0, silent=True, talk=False, exit=3)

    async def go():
        aid = await w.started("worker", "original prompt")
        await w.until(aid, timeout=20, states={"failed"})
        assert w.g.spawns() == 2
        assert all(call["pred_alive"] == [] for call in w.g.calls())
        retry = w.g.calls()[1]
        assert "original prompt" in retry["prompt"]   # C3: it arrives on stdin
        assert "original prompt" not in " ".join(retry["argv"])
        assert w.node(aid).retries == 1
    asyncio.run(go())


def test_sr_r1_r2_r4_consult_resumes_a_settled_turn_and_reports_its_duration(w, monkeypatch):
    h.up(w, conversational=True)
    w.g.set(gate=None)
    seen = []
    original = w.runner._steer_predecessor_dead

    async def gate(predecessor):
        result = await original(predecessor)
        seen.append((predecessor.pid, result))
        return result
    monkeypatch.setattr(w.runner, "_steer_predecessor_dead", gate)

    async def go():
        first = await w.runner.consult("worker", "first", timeout=20)
        assert not first.get("error"), first
        aid = first["agent_id"]
        old = w.node(aid)
        w.runner.tree.update(aid, started_at=old.started_at - 100)
        second = await w.runner.consult("worker", "second", timeout=20)
        assert not second.get("error"), second
        assert second["agent_id"] == aid
        assert seen and all(confirmed for _, confirmed in seen)
        assert w.g.calls()[1]["pred_alive"] == []
        assert w.node(aid).follow["turn"] > old.follow["turn"]
        status = w.server.check_agent(aid)
        assert status["elapsed_seconds"] <= 3
        assert status["node_elapsed_seconds"] >= 100
        elapsed = status["elapsed_seconds"]
        await asyncio.sleep(1.2)
        assert w.server.collect_agent(aid)["elapsed_seconds"] == elapsed
    asyncio.run(go())


def test_sr_r2_consult_resume_refuses_unknown_predecessor(w, monkeypatch):
    h.up(w, conversational=True)
    w.g.set(gate=None)
    monkeypatch.setattr("multiagents.runner.LAUNCH_CONFIRM_SECONDS", 0.1)

    async def go():
        first = await w.runner.consult("worker", "first", timeout=20)
        assert not first.get("error"), first
        aid = first["agent_id"]
        monkeypatch.setattr(w.runner, "_steer_predecessor", lambda _: _Predecessor(
            captured=True, probe_raw=lambda: None))
        second = await w.runner.consult("worker", "second", timeout=20)
        assert "confirmed dead" in second["error"]
        assert w.g.spawns() == 1
        assert w.runner._holds[aid].steer is not None
    asyncio.run(go())


def test_sr_r1_r3_r4_adoption_keeps_the_turn_offset_route_options_and_clock(w):
    h.up(w, variant="original", silence_timeout=200)

    async def go():
        aid = await h.running(w)
        assert (await w.server.steer_agent(aid, "next: go"))["steered"]
        before = w.node(aid)
        launch = w.runner.runs[aid].launched_at
        w.p.agents["worker"].update(model="acme/m2", variant="changed",
                                    silence_timeout=3)
        w.reload()
        await h.restart(w)
        run = w.runner.runs[aid]
        assert run.turn_start == before.follow["turn"]
        assert run.launched_at == launch
        assert w.node(aid).turn_started_at == before.turn_started_at
        assert run.spec.model == "acme/m1"
        assert run.spec.extra["variant"] == "original"
        assert run.supervisor.silence_timeout == 200
        assert (await w.server.steer_agent(aid, "next: restart"))["steered"]
        new = w.runner.runs[aid]
        assert new.spec.model == "acme/m1"
        assert new.spec.extra["variant"] == "original"
        assert new.supervisor.silence_timeout == 3
    asyncio.run(go())


@pytest.mark.parametrize("clock_state", ["recorded", "limits_evicted", "missing", "unreadable"])
def test_sr_r3_r4_adoption_never_extends_budget_from_forged_future_times(w, clock_state):
    h.up(w, timeout=3000)

    async def go():
        aid = await h.running(w)
        command_file = h.run_dir(w, aid) / "command.json"
        command = json.loads(command_file.read_text())
        assert "timeout" not in command
        command["launched_at"] = time.time() + 60000
        command_file.write_text(json.dumps(command))
        # The tree is writable too; neither of its clocks can be a fallback.
        w.runner.tree.update(aid, started_at=command["launched_at"],
                             turn_started_at=command["launched_at"])
        trusted_launch = time.time() - 120
        ledger = w.runner.launch_limits
        with ledger.locked() as records:
            records[aid]["launched_at"] = trusted_launch
            ledger.commit(records)
        snapshot_path = ledger._spec_path(aid)
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["launched_at"] = trusted_launch
        snapshot_path.write_text(json.dumps(snapshot))
        if clock_state in ("limits_evicted", "missing"):
            ledger.file.unlink()
        if clock_state == "missing":
            snapshot.pop("launched_at")
            snapshot_path.write_text(json.dumps(snapshot))
        elif clock_state == "unreadable":
            ledger.file.write_text("not json")
            snapshot_path.write_text("not json")

        for _ in range(2):
            await h.restart(w)
            run = w.runner.runs[aid]
            elapsed = time.monotonic() - run.supervisor.started
            if clock_state in ("recorded", "limits_evicted"):
                assert run.launched_at == trusted_launch
                assert elapsed >= 120
                assert elapsed < run.supervisor.wall_timeout
            else:
                assert elapsed > run.supervisor.wall_timeout
                # An expired turn is reported stuck and then ended as failed
                # ("timeout: ended at its ... wall clock"); stuck is only a
                # transient, so a poll can miss it under load. Either report,
                # carrying the timeout reason, is the expiry.
                assert await h.pc.await_until(
                    lambda: w.status(aid) in ("stuck", "failed")
                    and "timeout" in w.node(aid).reason, 30), (
                    w.node(aid), w.runner.paths.events_file.read_text())
        assert w.g.spawns() == 1
    asyncio.run(go())


@pytest.mark.parametrize("host_clock", ["recorded", "missing"])
def test_sr_r3_r4_forged_command_record_never_extends_an_adopted_budget(w, host_clock):
    h.up(w, timeout=60, silence_timeout=200, max_steps=20)

    async def go():
        aid = await h.running(w)
        command_file = h.run_dir(w, aid) / "command.json"
        command = json.loads(command_file.read_text())
        for key in ("launched_at", "timeout", "silence_timeout", "max_steps"):
            assert key not in command, key
        # Everything a budget is made of, forged generously in the run dir.
        command.update(launched_at=time.time() + 60000, timeout=60000,
                       silence_timeout=60000, max_steps=60000)
        command_file.write_text(json.dumps(command))
        ledger = w.runner.launch_limits
        trusted_launch = time.time() - 30
        with ledger.locked() as records:
            records[aid]["launched_at"] = trusted_launch
            if host_clock == "missing":
                records[aid].pop("launched_at")
            ledger.commit(records)
        snapshot_path = ledger._spec_path(aid)
        snapshot = json.loads(snapshot_path.read_text())
        snapshot.pop("launched_at")
        snapshot_path.write_text(json.dumps(snapshot))

        await h.restart(w)
        run = w.runner.runs[aid]
        assert run.supervisor.wall_timeout == 60
        assert run.supervisor.silence_timeout == 200
        assert run.supervisor.max_steps == 20
        elapsed = time.monotonic() - run.supervisor.started
        if host_clock == "recorded":
            assert run.launched_at == trusted_launch
            assert 30 <= elapsed < 60
        else:
            assert elapsed > 60
            assert await h.pc.await_until(lambda: w.status(aid) == "stuck", 30), (
                w.node(aid), w.runner.paths.events_file.read_text())
            assert "timeout" in w.node(aid).reason
        assert w.g.spawns() == 1
    asyncio.run(go())


def test_sr_r3_models_route_limits_refresh_without_refreshing_options(w):
    h.up(w, models={"acme": {"model": "acme/m1", "timeout": 120,
                              "silence_timeout": 200, "max_steps": 20,
                              "variant": "original"}})

    async def go():
        aid = await h.running(w)
        w.p.agents["worker"]["models"]["acme"].update(
            timeout=240, silence_timeout=300, max_steps=40, variant="changed")
        w.reload()
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"], result
        run = w.runner.runs[aid]
        assert run.limits["timeout"]["value"] == 240
        assert run.limits["silence_timeout"]["value"] == 300
        assert run.supervisor.max_steps == 40
        assert run.spec.extra["variant"] == "original"
    asyncio.run(go())


def test_sr_r3_clock_excludes_launch_preflight(w, monkeypatch):
    h.up(w, timeout=3)

    async def go():
        aid = await h.running(w)
        executor = w.runner.executor(w.runner.runs[aid].spec)
        monkeypatch.setattr(executor, "preflight", lambda: (time.sleep(1.1) or []))
        monkeypatch.setattr(w.runner, "executor", lambda spec=None: executor)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"], result
        run = w.runner.runs[aid]
        assert time.monotonic() - run.supervisor.started < 1
        assert w.server.check_agent(aid)["elapsed_seconds"] <= 1
        assert run.launched_at == w.runner.launch_limits.launch_time(aid)
    asyncio.run(go())


def test_sr_r3_clock_excludes_executor_preparation(w, monkeypatch):
    h.up(w, timeout=3)

    async def go():
        aid = await h.running(w)
        executor = w.runner.executor(w.runner.runs[aid].spec)
        start = executor.start

        async def prepare(*args, **kw):
            await asyncio.sleep(1.1)
            return await start(*args, **kw)
        monkeypatch.setattr(executor, "start", prepare)
        monkeypatch.setattr(w.runner, "executor", lambda spec=None: executor)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"], result
        run = w.runner.runs[aid]
        assert time.monotonic() - run.supervisor.started < 1
        assert run.launched_at == run.handle.launched_at
        assert run.launched_at == w.runner.launch_limits.launch_time(aid)
        assert await h.stays(lambda: w.status(aid) == "running", 2)
    asyncio.run(go())


@pytest.mark.parametrize("remove_record", [False, True])
def test_sr_r3_only_the_host_limits_record_preserves_explicit_timeout(w, remove_record):
    h.up(w)

    async def go():
        aid = await h.running(w, timeout=60)
        snapshot = json.loads(w.runner.launch_limits._spec_path(aid).read_text())
        assert "call_timeout" not in snapshot
        assert "timeout" not in snapshot["spec"]
        if remove_record:
            with w.runner.launch_limits.locked() as records:
                records.pop(aid)
                w.runner.launch_limits.commit(records)
            # Legacy spec snapshots and agent-writable command records
            # cannot keep a limit after its authoritative record is gone.
            snapshot["call_timeout"] = 60000
            snapshot["spec"]["timeout"] = 60000
            w.runner.launch_limits._spec_path(aid).write_text(json.dumps(snapshot))
            command_file = h.run_dir(w, aid) / "command.json"
            command = json.loads(command_file.read_text())
            command["timeout"] = 60000
            command_file.write_text(json.dumps(command))
        w.p.agents["worker"]["timeout"] = 2700
        w.reload()
        await h.restart(w)
        assert w.runner.runs[aid].supervisor.wall_timeout == (2700 if remove_record else 60)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"], result
        limit = w.runner.runs[aid].limits["timeout"]
        assert limit["value"] == (2700 if remove_record else 60)
        assert (limit["source"] == "call") is not remove_record
    asyncio.run(go())


def test_sr_r3_invalid_step_limit_refuses_before_stopping_the_predecessor(w):
    h.up(w)

    async def go():
        aid = await h.running(w)
        old = w.g.pids()[0]
        w.p.agents["worker"]["max_steps"] = "invalid"
        w.reload()
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"] is False
        assert "invalid" in result["error"]
        assert h.pc.alive(old)
        assert w.g.spawns() == 1
    asyncio.run(go())


class MountedDocker(DockerExecutor):
    """Run Docker's actual inline wrapper entry on the mounted host paths.

    No daemon is involved; all status-file handling is the Docker executor's.
    The independent raw probe models the container-side liveness decision.
    """
    oom_kill_count = None

    def preflight(self):
        return []

    def git(self, agent_id):
        # `inside()` is False for the liveness path; git stays local, so no
        # `docker exec` (and no docker executable) is needed.
        return gitops.HOST

    def wrapper_alive(self, agent_id):
        try:
            pid = int((self.paths.run_dir(agent_id) / "wrapper.pid").read_text())
        except (OSError, ValueError):
            return None
        return running(pid) or session_alive(pid)

    def liveness(self, agent_id):
        return lambda: self.wrapper_alive(agent_id)

    def wrapper_verdict(self, agent_id):
        return self.wrapper_alive(agent_id)

    def kill_detached(self, agent_id, grace=3):
        pid = int((self.paths.run_dir(agent_id) / "wrapper.pid").read_text())
        return stop_wrapped(self.paths.run_dir(agent_id), pid, grace=grace)

    async def start(self, argv, cwd, env, *, run_dir=None, deadline=0, provider=""):
        # `docker exec --env-file` carries the prompt-transport variables to the
        # wrapper (C3); with no daemon here, `env` does the same on the host.
        carried = [f"{k}={v}" for k, v in env.items() if k.startswith("MULTIAGENTS_PROMPT_")]
        return self._start_wrapped(["env", *carried], argv, run_dir, deadline,
                                   run_dir / "container.pid", env)


def test_sr_r1_docker_shared_status_is_cleared_after_container_predecessor_death(w, monkeypatch):
    h.up(w)
    executor = MountedDocker({"image": "test"}, paths=w.runner.paths)
    monkeypatch.setattr(w.runner, "executor", lambda spec=None: executor)
    monkeypatch.setattr(executor, "inside", lambda: False)
    checked = []
    raw = executor.wrapper_alive

    def probe(aid):
        answer = raw(aid)
        checked.append(answer)
        return answer
    monkeypatch.setattr(executor, "wrapper_alive", probe)

    async def go():
        aid = await h.running(w)
        previous = h.wrapper_pid(w, aid)
        old = w.g.pids()[0]
        h.slow_stop(monkeypatch, old, 0.8)
        result = await w.server.steer_agent(aid, "next: go")
        assert result["steered"], result
        assert False in checked
        assert not running(previous)
        assert w.g.calls()[1]["pred_alive"] == []
        assert not (h.run_dir(w, aid) / "exit_status").exists()
        assert await h.stays(lambda: w.status(aid) == "running", 0.8)
        w.g.open()
        assert (await w.until(aid))[aid] == "done"
        assert (h.run_dir(w, aid) / "exit_status").read_text().strip() == "0"
    asyncio.run(go())
