"""A stop during a wrapped docker agent's launch must still end its setsid children.

Known product race, found by TS Run A (context/ts/manifests/README.md,
"Finding"). `agentwrap.main` starts the agent, and only then records its pid
in `container.pid`. `DockerExecutor.kill_detached` reads that file on the
host before its kill script runs in the container. A stop that reads it
inside the gap gets no agent pid. The script then signals only the wrapper,
whose `killpg` reaches the agent's process group, and a child that called
`setsid` outlives the stop for good.

test_sandbox_git_docker_stop.py checks the steady-state stop, once the pid is
recorded. This file keeps the launch window covered, deterministically: no
test sleeps waiting for a race to happen.

- The container's Python gets a `sitecustomize` (through the env file, as
  every agent variable is passed). It holds the wrapper at the `os.replace`
  that lands `container.pid`, after the agent is running, until a release
  file appears.
- On the host, `_recorded_pid` is wrapped. The first time the stop reads
  `container.pid`, it gets what is there (nothing, since the wrapper is held),
  releases the gate, and returns once the file exists. So the stop reads
  inside the gap, and the gap closes as the stop proceeds, as it does in the
  race. A fix that reads again, waits, or looks inside the container sees the
  agent.

The test is expected to fail on its assertion until the fix lands, then it
XPASSes and strict turns that red. A fixture that cannot reach the gap
raises RuntimeError, which `raises=AssertionError` does not excuse.
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
import multiagents.executor.docker as docker_mod  # noqa: E402
from test_sandbox_git_docker_stop import (  # noqa: E402,F401
    AGENT, _outside, agent_script, alive, assert_ended, executor, image)

GATE_ENV = "TS_LAUNCH_GATE"

SITECUSTOMIZE = f'''
import os
_gate = os.environ.pop({GATE_ENV!r}, None)
if _gate:
    _replace = os.replace
    def replace(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == "container.pid":
            open(_gate + ".entered", "w").close()
            while not os.path.exists(_gate + ".released"):
                __import__("time").sleep(0.01)
        return _replace(src, dst, *args, **kwargs)
    os.replace = replace
'''


def _until(check, what: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise RuntimeError(f"fixture: {what} never happened")


def _pid_in(path: Path) -> int | None:
    try:
        head = path.read_text().split()[:1]
    except OSError:
        return None
    return int(head[0]) if head and head[0].isdigit() else None


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="product race: stop during launch leaves setsid children"
                          " — context/tickets/2026-09-30-unfiled.md")
def test_ts_a_stop_inside_the_launch_window_leaves_no_setsid_child(tmp_path, monkeypatch):
    image(tmp_path, monkeypatch, pkill=False)
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    gate = tmp_path / "gate"
    released = Path(f"{gate}.released")
    ex = executor(tmp_path)
    run_dir = ex.paths.run_dir(AGENT)
    pid_file = run_dir / "container.pid"

    reads = []
    real_recorded_pid = docker_mod._recorded_pid

    def recorded_pid(where, name, *args, **kwargs):
        value = real_recorded_pid(where, name, *args, **kwargs)
        if name == "container.pid" and not released.exists():
            reads.append(value)
            released.touch()
            _until(pid_file.exists, "the wrapper recording the agent once released")
        return value

    monkeypatch.setattr(docker_mod, "_recorded_pid", recorded_pid)

    box: dict = {"pids": {}}

    async def go():
        env = {"MULTIAGENTS_AGENT_ID": AGENT, "HOME": str(tmp_path),
               "PYTHONPATH": str(site), GATE_ENV: str(gate)}
        box["handle"] = await ex.start(agent_script(tmp_path), tmp_path, env,
                                       run_dir=run_dir)
        await asyncio.to_thread(_until, Path(f"{gate}.entered").exists,
                                "the wrapper reaching the gap")
        for name in ("child-in-group", "child-left-group"):
            box["pids"][name] = await asyncio.to_thread(
                _until, lambda n=name: _pid_in(tmp_path / n), f"{name} starting")
        if pid_file.exists() or not all(alive(p) for p in box["pids"].values()):
            raise RuntimeError("fixture: the stop is not inside the launch window")
        await box["handle"].stop(grace=1)

    try:
        asyncio.run(go())
        if reads != [""]:
            raise RuntimeError(f"fixture: the stop read container.pid as {reads!r},"
                               " not once inside the gap")
        assert_ended(box["pids"], ["child-left-group"], "a stop during launch")
    finally:
        released.touch()
        extra = [_pid_in(run_dir / "wrapper.pid"), _pid_in(pid_file)]
        for pid in [*box["pids"].values(), *extra]:
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
