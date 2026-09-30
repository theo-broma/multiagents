"""Q1: a retry whose launch fails must be recorded, not swallowed.

`_finalize`'s one free retry used to wrap its `_launch` in a blanket
`contextlib.suppress(Exception)`: when the relaunch could not even start (a
missing binary, a refused argv, an OSError), the node ended with the FIRST
death's reason and the only trace was a `retrying` event promising a relaunch
nobody ever attempted.
"""

import asyncio
import json
import os
import subprocess

from multiagents.config import AgentSpec, Config
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner


def _runner(tmp_path):
    """The same throwaway-project seam test_core's retry tests use."""
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *args],
                       capture_output=True, env=env)
    config = Config(
        project={},
        providers={"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 1"],
                                                "resume": ["-c", "exit 1"]}}},
        agents={"worker": AgentSpec("worker", "p", "m")},
        models={}, instruction_dirs=[],
    )
    return Runner(paths, config)


def _run_once_with_retry_launch_raising(tmp_path, exc):
    """First launch dies cheap and silent, the retry's `_launch` raises `exc`.

    Returns the agent id once the node has left the running states.
    """
    r = _runner(tmp_path)
    real_launch = r._launch
    calls = {"n": 0}

    async def flaky_launch(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise exc
        return await real_launch(**kwargs)

    r._launch = flaky_launch

    async def scenario():
        out = await r.start("worker", "go")
        for _ in range(80):
            node = r.tree.get(out["agent_id"])
            if node.status not in ("pending", "running"):
                return out["agent_id"]
            await asyncio.sleep(0.1)
        return out["agent_id"]

    return r, asyncio.run(scenario())


def test_a_failed_retry_launch_is_recorded_and_ends_the_node(tmp_path):
    """The `retrying` event promises a relaunch; a launch that cannot start
    leaves its reason on the node instead of vanishing into a suppressed
    exception."""
    r, agent_id = _run_once_with_retry_launch_raising(
        tmp_path, RuntimeError("no such binary"))

    events = [json.loads(l) for l in
              r.paths.events_file.read_text().splitlines()]
    mine = [e for e in events if e.get("agent") == agent_id]

    assert [e for e in mine if e["kind"] == "retrying"], \
        "the retry was announced"
    failed = [e for e in mine if e["kind"] == "retry_failed"]
    assert len(failed) == 1, "the launch failure is on the record"
    assert failed[0]["detail"] == "RuntimeError: no such binary"

    node = r.tree.get(agent_id)
    assert node.status == "failed", "a failed retry launch ends the node"
    assert "retry launch failed" in node.reason
    assert "no such binary" in node.reason
    assert node.retries == 1, "the attempt itself is still counted"


def test_the_retry_failure_detail_is_truncated(tmp_path):
    """A provider exception carrying kilobytes of prose cannot write all of it
    into the event; the detail is bounded like every other detail field."""
    r, agent_id = _run_once_with_retry_launch_raising(
        tmp_path, RuntimeError("x" * 1000))

    events = [json.loads(l) for l in
              r.paths.events_file.read_text().splitlines()]
    failed = [e for e in events
              if e["kind"] == "retry_failed" and e.get("agent") == agent_id]
    assert len(failed) == 1
    assert len(failed[0]["detail"]) == 300
