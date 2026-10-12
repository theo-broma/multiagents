"""ST5-R1/R2: production process probes and completed-call boundaries."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from multiagents.executor import docker
from multiagents.executor.local import LocalExecutor
from multiagents.providers import Event
from multiagents.supervisor import Supervisor


@pytest.fixture
def paths(tmp_path):
    run_dir = tmp_path / "runs" / "worker"
    run_dir.mkdir(parents=True)
    return SimpleNamespace(data=tmp_path, run_dir=lambda agent_id: run_dir, root=tmp_path, slug="st5")


@pytest.fixture
def cli():
    source = """
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print('ready', flush=True)
try:
    for line in sys.stdin:
        child.terminate()
        child.wait()
        print('ended', flush=True)
finally:
    if child.poll() is None:
        child.terminate()
        child.wait()
"""
    proc = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
        proc.stdout.close()


def test_local_and_container_probe_follow_cli_ancestry(paths, cli):
    (paths.run_dir("worker") / "agent.pid").write_text(str(cli.pid))
    executor = LocalExecutor(paths=paths)

    def container_probe():
        result = subprocess.run([sys.executable, "-c", docker._DESCENDANTS_CHECK, str(cli.pid)],
                                capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()

    assert executor.has_live_descendants("worker") is True
    assert container_probe() == "descendants:true"
    cli.stdin.write("end\n")
    cli.stdin.flush()
    assert cli.stdout.readline().strip() == "ended"
    # The test process and its other children remain alive, outside this CLI.
    assert executor.has_live_descendants("worker") is False
    assert container_probe() == "descendants:false"


@pytest.mark.parametrize("output,code,expected", [
    ("descendants:true\n", 0, True), ("descendants:false\n", 0, False),
    ("descendants:unknown\n", 0, None), ("descendants:false\n", 1, None),
    ("", 0, None),
])
def test_docker_probe_uses_recorded_container_cli_pid(paths, monkeypatch, output, code, expected):
    (paths.run_dir("worker") / "container.pid").write_text("41000 namespace\n")
    executor = docker.DockerExecutor({}, paths=paths)
    monkeypatch.setattr(executor, "inside", lambda: False)
    def run(argv, **kwargs):
        assert argv[:3] == ["docker", "exec", executor.container]
        assert argv[-1] == "41000"
        assert argv[-2] == docker._DESCENDANTS_CHECK
        return subprocess.CompletedProcess(argv, code, output, "")
    monkeypatch.setattr(docker, "_run", run)
    assert executor.has_live_descendants("worker") is expected


def test_distinct_completed_call_ids_on_one_step_count_and_replays_do_not():
    sup = Supervisor(loop_repeats=3)
    for ident in ("a", "b"):
        event = Event(kind="tool", name="bash", args={"command": "poll"},
                      state="completed", step=1, tool_id=ident, result="fixed")
        assert sup.observe(event) is None
        assert sup.observe(event) is None
    trip = sup.observe(Event(kind="tool", name="bash", args={"command": "poll"},
                             state="completed", step=1, tool_id="c", result="fixed"))
    assert trip and trip.reason == "doom_loop"


def test_arguments_that_differ_after_a_large_prefix_are_distinct():
    sup = Supervisor(loop_repeats=3)
    for i in range(6):
        assert sup.observe(Event(kind="tool", name="bash", state="completed",
                                 args={"command": "x" * 3000 + str(i)}, result="same")) is None


@pytest.mark.parametrize("observation", ["failure", "unknown"])
def test_failed_observation_after_live_child_does_not_grant_a_fresh_window(tmp_path, monkeypatch, observation):
    import asyncio
    from test_st5_silence import SILENCE, make_world, running, trips

    runner, probe, clock, executor = make_world(tmp_path, monkeypatch, container="live")
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            assert trips(runner, agent) == []
            executor.observation = observation
            await clock.tick(1)
            await clock.tick(1)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    asyncio.run(check())
