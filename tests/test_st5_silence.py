"""ST5-R2/R3: fake-clock watchdogs over real, gate-controlled CLI processes.

Only public Runner/Tree results and run/event artifacts are asserted. The
local executor gets a real child. The simulated container supplies process
inspection at the executor boundary, deliberately using pids absent on the
host so the docker exec client's ancestry cannot accidentally pass the test.

ST Decision 7 (091d1d84) confirms the executor observation seam:
has_live_descendants(agent_id) -> bool | None, for local and docker alike.
None or an exception grants no descendant exemption.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h
from multiagents import runner as runner_module
from multiagents import supervisor as supervisor_module
from multiagents.executor.local import LocalExecutor
from multiagents.providers import resolve_inheritance
from multiagents.supervisor import Supervisor
from test_st5_doom_loop import event, feed, start, result, clock, providers

WAIT_BOUND = 5.0
POLL_INTERVAL = 0.031337
SILENCE = 10.0
DEFAULTS = Path(__file__).resolve().parents[1] / "src/multiagents/defaults/providers.yaml"


def polls(sup):
    """Allow the existing disk watchdog's first comparison poll."""
    return [trip for _ in range(2) if (trip := sup.check_timers()) is not None]


def silent_supervisor(clock, *, wall=1000):
    sup = Supervisor(silence_timeout=SILENCE, wall_timeout=wall,
                     started=clock.now, last_event=clock.now)
    sup.note_progress("unchanged-tree")
    return sup


def test_st5_r2_open_tool_suppresses_silence(clock, providers):
    sup = silent_supervisor(clock)
    assert feed(sup, providers, "codex", [start("suite", "pytest -n 4")]) == []
    clock.now += SILENCE * 3
    assert polls(sup) == []


def test_st5_r2_crossed_completions_keep_the_other_tool_open(clock, providers):
    sup = silent_supervisor(clock)
    assert feed(sup, providers, "codex", [start("a", "pytest"), start("b", "build"),
                                          result("b", "built")]) == []
    clock.now += SILENCE * 3
    assert polls(sup) == [], "the result for b cannot finish a"


@pytest.mark.parametrize("completion", ["unrelated", "b"])
def test_st5_r2_unmatched_or_duplicate_completion_does_not_close_an_open_tool(clock, providers, completion):
    sup = silent_supervisor(clock)
    assert feed(sup, providers, "codex", [start("a", "pytest"), start("b", "build"),
                                          result("b", "built"), result(completion, "finished")]) == []
    clock.now += SILENCE * 3
    assert polls(sup) == [], "only a matching completion can finish a"


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_st5_r2_silence_restarts_when_the_last_open_tool_ends(clock, providers, state):
    sup = silent_supervisor(clock)
    feed(sup, providers, "codex", [start("suite", "pytest")])
    clock.now += SILENCE * 3
    polls(sup)
    feed(sup, providers, "codex", [{**result("suite", "finished"), "state": state}])
    clock.now += SILENCE - 0.01
    assert polls(sup) == []
    clock.now += 0.02
    trips = polls(sup)
    assert [trip.reason for trip in trips] == ["silence"]
    assert "10" in trips[0].detail and "s" in trips[0].detail


def test_st5_r2_wall_clock_still_applies_to_an_open_tool(clock, providers):
    sup = silent_supervisor(clock, wall=20)
    feed(sup, providers, "codex", [start("suite", "pytest")])
    clock.now += 21
    trip = sup.check_timers()
    assert trip is not None and trip.reason == "timeout"


class TimeView:
    def __init__(self, clock):
        self.clock = clock

    def monotonic(self):
        return self.clock.now

    def __getattr__(self, name):
        return getattr(time, name)


class ManualClock:
    """The watch-loop's clock/sleep seam; unrelated asyncio timers stay real.

    tick waits for one whole poll to finish, including its I/O, by waiting
    for the next poll's sleep to start. That synchronizes negative assertions
    without fixed sleeps, arbitrary scheduling yields, or production internals.
    """
    def __init__(self):
        self.now = time.monotonic()
        self.sleepers = asyncio.Queue()
        self.waiter = None

    async def sleep(self, seconds, *args, **kwargs):
        if seconds != POLL_INTERVAL:
            return await asyncio.sleep(seconds, *args, **kwargs)
        future = asyncio.get_running_loop().create_future()
        await self.sleepers.put(future)
        return await future

    async def tick(self, seconds):
        if self.waiter is None:
            self.waiter = await asyncio.wait_for(self.sleepers.get(), WAIT_BOUND)
        self.now += seconds
        self.waiter.set_result(None)
        self.waiter = await asyncio.wait_for(self.sleepers.get(), WAIT_BOUND)


class AsyncioView:
    def __init__(self, clock):
        self.sleep = clock.sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


CLI = r'''#!{python}
import json, os, subprocess, sys, time
from pathlib import Path
probe = Path({probe!r})
mode = {mode!r}
deadline = time.monotonic() + {bound!r}
def wait_for(name):
    while not (probe / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("gate expired: " + name)
        time.sleep(.005)
def text(value):
    if mode == "tool":
        print(json.dumps({{"kind": "text", "text": value}}), flush=True)
    else:
        print(json.dumps({{"type": "text", "sessionID": "st5", "part": {{"text": value}}}}), flush=True)
(probe / "cli.pid").write_text(str(os.getpid()))
child = None
try:
    if mode == "child":
        code = "import sys,time; from pathlib import Path; p=Path(sys.argv[1]); end=time.monotonic()+float(sys.argv[2]);\nwhile not p.exists() and time.monotonic()<end: time.sleep(.005)"
        child = subprocess.Popen([sys.executable, "-c", code, str(probe / "child-end"), str({bound!r})],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (probe / "child.pid").write_text(str(child.pid))
    if mode == "tool":
        print(json.dumps({{"kind": "tool", "name": "command_execution", "args": {{"command": "pytest"}},
                          "tool_id": "suite", "state": "in_progress"}}), flush=True)
    text("ready")
    if mode == "child":
        wait_for("child-end")
        child.wait(timeout={bound!r})
        (probe / "child-ended").write_text("yes")
    if mode == "tool":
        wait_for("tool-end")
        print(json.dumps({{"kind": "step", "state": "completed", "tool_id": "suite", "result": "passed"}}), flush=True)
        text("tool-ended")
    wait_for("finish")
    text("done")
finally:
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait(timeout={bound!r})
'''


class ContainerEnvironment(LocalExecutor):
    """Simulated container namespace at the executor's observation boundary.

    The host launcher is a genuine process, while these synthetic container
    pids are unrelated to it. Parent maps include the wrapper and another
    agent's live child. Neither is an exemption for this CLI.
    """
    kind = "docker"

    def __init__(self, runner, observation, *, kind="docker"):
        super().__init__(runner.providers)
        self.kind = kind
        self.config = {"mount_cli_from_host": True}
        self.paths = runner.paths
        self.observation = observation
        self.parents = {41000: 40000, 42000: 41000, 44000: 43000, 43000: 40000}
        self.live = {40000, 41000, 42000, 43000, 44000}

    async def start(self, argv, cwd, env, **kwargs):
        handle = await super().start(argv, cwd, env, **kwargs)
        self.paths.run_dir(env["MULTIAGENTS_AGENT_ID"]).joinpath("container.pid").write_text("41000\n")
        return handle

    def has_live_descendants(self, agent_id):
        if self.observation == "failure":
            raise OSError("container process inspection unavailable")
        if self.observation == "unknown":
            return None
        root = int((self.paths.run_dir(agent_id) / "container.pid").read_text())
        for pid in self.live:
            seen = set()
            parent = self.parents.get(pid)
            while parent and parent not in seen:
                if parent == root:
                    return True
                seen.add(parent)
                parent = self.parents.get(parent)
        return False


async def until(predicate):
    deadline = time.monotonic() + WAIT_BOUND
    while not predicate():
        assert time.monotonic() < deadline, "fixture: observable condition did not arrive"
        await asyncio.sleep(.005)


def log_events(runner):
    path = runner.paths.events_file
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def trips(runner, agent):
    return [entry for entry in log_events(runner)
            if entry.get("agent") == agent and entry.get("kind") == "stuck"]


def stream_has(runner, agent, words):
    path = runner.paths.run_dir(agent) / "stream.jsonl"
    return path.exists() and words in path.read_text()


def logged_activity(runner, agent, evidence, captured=""):
    # ST5-R3 explicitly makes the suppression evidence observable in logs;
    # wording and event names remain free.
    entries = [entry for entry in log_events(runner) if entry.get("agent") == agent
               and entry.get("kind") not in {"stuck", "started", "spawned"}]
    text = (json.dumps(entries) + captured).lower()
    return evidence in text and any(word in text for word in ("silence", "watchdog", "suppress", "activity"))


def make_world(tmp_path, monkeypatch, *, mode="idle", container=None, wall=1000,
               executor_kind="docker"):
    probe = tmp_path / "probe"
    probe.mkdir()
    script = tmp_path / "fake-cli.py"
    script.write_text(CLI.format(python=sys.executable, probe=str(probe), mode=mode,
                                 bound=WAIT_BOUND))
    script.chmod(0o755)
    data = resolve_inheritance(yaml.safe_load(DEFAULTS.read_text())["providers"])
    name = "codex" if mode == "tool" else "opencode"
    # Exercise the shipped stream rules but launch the synthetic CLI directly.
    provider = {"bin": str(script), "spawn": {"args": ["--fake"]}, "stream": data[name]["stream"]}
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"worker": h.AgentSpec("worker", name, "test", writes=True)},
                           providers={name: provider},
                           project={"limits": {"silence_timeout": SILENCE, "default_timeout": wall,
                                                "doom_loop_repeats": 3}})
    clock = ManualClock()
    monkeypatch.setattr(runner_module, "time", TimeView(clock))
    monkeypatch.setattr(supervisor_module, "time", TimeView(clock))
    monkeypatch.setattr(runner_module, "asyncio", AsyncioView(clock))
    monkeypatch.setattr(h.Runner, "WATCH_POLL_SECONDS", POLL_INTERVAL)
    executor = None
    if container is not None:
        executor = ContainerEnvironment(runner, container, kind=executor_kind)
        if container == "other-agent":
            executor.live.remove(42000)
        monkeypatch.setattr(runner, "executor", lambda spec=None: executor)
    return runner, probe, clock, executor


@contextlib.asynccontextmanager
async def running(runner, probe):
    agent = None
    try:
        launched = await runner.start("worker", "exercise ST5")
        assert "agent_id" in launched, launched
        agent = launched["agent_id"]
        await until(lambda: stream_has(runner, agent, "ready"))
        yield agent
    finally:
        (probe / "child-end").touch()
        (probe / "tool-end").touch()
        (probe / "finish").touch()
        if agent is not None:
            await asyncio.wait_for(runner.stop(agent), WAIT_BOUND)


def test_st5_r2_local_opencode_real_child_suppresses_silence(tmp_path, monkeypatch):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, mode="child")
    async def check():
        async with running(runner, probe) as agent:
            await until(lambda: (probe / "child.pid").exists())
            child = int((probe / "child.pid").read_text())
            assert os.kill(child, 0) is None, "fixture: the suite child is live"
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            assert trips(runner, agent) == []
    asyncio.run(check())


def test_st5_r2_runner_started_tool_suppresses_silence(tmp_path, monkeypatch):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, mode="tool")
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            assert trips(runner, agent) == []
    asyncio.run(check())


def test_st5_r2_container_opencode_descendant_suppresses_silence(tmp_path, monkeypatch):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, container="live")
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            assert trips(runner, agent) == []
    asyncio.run(check())


def test_st5_r2_local_other_agents_real_suite_does_not_exempt(tmp_path, monkeypatch):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch)
    other_probe = tmp_path / "other-agent"
    other_probe.mkdir()
    script = tmp_path / "other-cli.py"
    script.write_text(CLI.format(python=sys.executable, probe=str(other_probe),
                                 mode="child", bound=WAIT_BOUND))
    # Another CLI's genuine suite is live in this same local environment;
    # neither its process group nor its descendants belong to the tested CLI.
    other = subprocess.Popen([sys.executable, str(script)], cwd=tmp_path,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    async def check():
        async with running(runner, probe) as agent:
            await until(lambda: (other_probe / "child.pid").exists())
            child = int((other_probe / "child.pid").read_text())
            os.kill(child, 0)
            await clock.tick(SILENCE + 1)
            await clock.tick(1)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    try:
        asyncio.run(check())
    finally:
        (other_probe / "child-end").touch()
        (other_probe / "finish").touch()
        try:
            other.wait(timeout=WAIT_BOUND)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(other.pid, signal.SIGKILL)
            other.wait(timeout=WAIT_BOUND)


@pytest.mark.parametrize("environment", [None, "other-agent", "failure", "unknown"])
def test_st5_r2_idle_other_agent_and_failed_observation_do_not_exempt(tmp_path, monkeypatch, environment):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, container=environment)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE + 1)
            await clock.tick(1)
            seen = trips(runner, agent)
            assert [entry["reason"] for entry in seen] == ["silence"]
            assert "s" in seen[0]["detail"] and any(str(n) in seen[0]["detail"] for n in (11, 12))
    asyncio.run(check())


@pytest.mark.parametrize("environment", ["failure", "unknown"])
@pytest.mark.parametrize("executor_kind", ["local", "docker"])
def test_st5_r3_failed_observation_is_logged(tmp_path, monkeypatch, caplog, environment, executor_kind):
    caplog.set_level(logging.DEBUG)
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, container=environment,
                                        executor_kind=executor_kind)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE + 1)
            await clock.tick(1)
            records = (json.dumps(log_events(runner)) + caplog.text).lower()
            assert any(word in records for word in ("inspection", "observation", "descendant"))
            assert any(word in records for word in ("failed", "unavailable", "unknown", "error"))
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    asyncio.run(check())


@pytest.mark.parametrize("observation", ["failure", "unknown"])
def test_st5_r2_local_observation_failure_does_not_exempt_a_real_child(tmp_path, monkeypatch, observation):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, mode="child",
                                        container=observation, executor_kind="local")
    async def check():
        async with running(runner, probe) as agent:
            await until(lambda: (probe / "child.pid").exists())
            os.kill(int((probe / "child.pid").read_text()), 0)
            await clock.tick(SILENCE + 1)
            await clock.tick(1)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    asyncio.run(check())


@pytest.mark.parametrize("ends_first", ["tool", "child"])
def test_st5_r2_either_an_open_tool_or_a_descendant_keeps_the_agent_active(tmp_path, monkeypatch, ends_first):
    runner, probe, clock, executor = make_world(tmp_path, monkeypatch, mode="tool", container="live")
    async def end(activity, agent):
        if activity == "tool":
            (probe / "tool-end").touch()
            await until(lambda: stream_has(runner, agent, "tool-ended"))
        else:
            executor.live.remove(42000)
        await clock.tick(0)

    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            assert trips(runner, agent) == []
            await end(ends_first, agent)
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            assert trips(runner, agent) == [], "the other source of activity remains live"
            await end("child" if ends_first == "tool" else "tool", agent)
            await clock.tick(SILENCE - .01)
            assert trips(runner, agent) == []
            await clock.tick(.02)
            await clock.tick(0)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    asyncio.run(check())


@pytest.mark.parametrize("environment", ["local", "container", "tool"])
def test_st5_r2_silence_restarts_when_activity_ends(tmp_path, monkeypatch, environment):
    mode = "child" if environment == "local" else "tool" if environment == "tool" else "idle"
    runner, probe, clock, executor = make_world(tmp_path, monkeypatch, mode=mode,
                                               container="live" if environment == "container" else None)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            # End activity without an assistant event. The child-end case is
            # the essential distinction from restarting at the last text.
            if environment == "local":
                (probe / "child-end").touch()
                await until(lambda: (probe / "child-ended").exists())
            elif environment == "container":
                executor.live.remove(42000)
            else:
                (probe / "tool-end").touch()
                await until(lambda: stream_has(runner, agent, "tool-ended"))
            await clock.tick(0)  # observe that the final child/tool has ended
            # Inspect only the new quiet episode; premature alerts are covered
            # separately, so an existing implementation reaches this assertion.
            before = len(trips(runner, agent))
            await clock.tick(SILENCE - .01)
            assert len(trips(runner, agent)) == before
            await clock.tick(.02)
            await clock.tick(0)
            fresh = trips(runner, agent)[before:]
            assert [entry["reason"] for entry in fresh] == ["silence"]
            assert "10" in fresh[0]["detail"]
    asyncio.run(check())


@pytest.mark.parametrize("environment", ["local", "container", "tool"])
def test_st5_r3_suppressed_silence_logs_activity_evidence(tmp_path, monkeypatch, caplog, environment):
    caplog.set_level(logging.DEBUG)
    mode = "child" if environment == "local" else "tool" if environment == "tool" else "idle"
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch, mode=mode,
                                        container="live" if environment == "container" else None)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            await clock.tick(1)
            if environment == "tool":
                assert logged_activity(runner, agent, "tool", caplog.text)
            else:
                assert (logged_activity(runner, agent, "child", caplog.text)
                        or logged_activity(runner, agent, "descendant", caplog.text))
    asyncio.run(check())


def test_st5_r2_container_waits_for_the_last_descendant_to_end(tmp_path, monkeypatch):
    runner, probe, clock, executor = make_world(tmp_path, monkeypatch, container="live")
    executor.parents[42001] = 41000
    executor.live.add(42001)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(SILENCE * 3)
            executor.live.remove(42000)
            await clock.tick(SILENCE * 2)
            await clock.tick(1)
            assert trips(runner, agent) == [], "one live descendant still exempts the CLI"
            executor.live.remove(42001)
            await clock.tick(0)
            await clock.tick(SILENCE - .01)
            assert trips(runner, agent) == []
            await clock.tick(.02)
            await clock.tick(0)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["silence"]
    asyncio.run(check())


@pytest.mark.parametrize("environment", ["local", "container", "tool"])
def test_st5_r2_wall_clock_still_bounds_a_live_descendant(tmp_path, monkeypatch, environment):
    runner, probe, clock, _ = make_world(tmp_path, monkeypatch,
                                        mode="child" if environment == "local" else "tool" if environment == "tool" else "idle",
                                        container="live" if environment == "container" else None,
                                        wall=20)
    async def check():
        async with running(runner, probe) as agent:
            await clock.tick(21)
            assert [entry["reason"] for entry in trips(runner, agent)] == ["timeout"]
    asyncio.run(check())
