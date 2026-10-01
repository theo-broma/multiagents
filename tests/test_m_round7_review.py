"""Regression tests for the round-7 review of batch M (reviewer ag-b859f1).

Three defects, one test each:

1. P1: the death check accepted an `exit_status` file as death before the
   pid or the raw container probe was consulted. RM-R1c says explicitly
   that an exit-status result alone is not proof: ownership is released
   only on positive confirmation from the actual process (pid identity
   gone) or the container (the raw probe definitely reporting dead).
2. P1: `identity` was assigned only under `home_policy == "per-agent"`, so
   with `security.home_policy: shared` every launch raised
   UnboundLocalError. A launch under `shared` must simply work.
3. P2: the earlier auxiliaries polled for death after the cleanup returned,
   letting a process that died late pass. Death is asserted at the moment
   the cleanup returns (the round-4/round-5 files now do exactly that);
   this file pins the same immediate assertion on the confirmed-death
   flow.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.base import running as _running  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    _budgets, _events, _fakes, _project, _start,
)


def _runner(tmp_path, monkeypatch, *, cap=None, project=None):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    base = {"limits": {"max_concurrent": cap}} if cap else {}
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {**base, **(project or {})})
    _budgets(monkeypatch, acme=1.0)
    return runner, probes


# ---------------------------------------------------------------------------
# Finding 1 (P1): an exit_status file alone is not death
# ---------------------------------------------------------------------------

def test_an_exit_status_file_alone_is_not_death(tmp_path, monkeypatch):
    """The handle points at a LIVE process while an `exit_status` file
    holding `0` sits in the run dir. RM-R1c: the file is not proof — the
    cleanup confirms death from the actual process or the container, finds
    neither, and holds everything."""
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)
    monkeypatch.setattr(runner_mod, "LAUNCH_CONFIRM_SECONDS", 0.6)
    sleeper = subprocess.Popen(["sleep", "30"])
    assert procs.start_time(sleeper.pid)
    handles = []
    executor_cls = type(runner.executor())
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "docker/test-container",
                        raising=False)
    monkeypatch.setattr(executor_cls, "wrapper_alive",
                        lambda self, agent_id: None, raising=False)
    real_start = executor_cls.start

    def on_handle(handle):
        # The forged situation: a verdict on file, a live pid underneath.
        (handle.run_dir / "exit_status").write_text("0")
        handle.pid = sleeper.pid
        handle.pid_start = procs.start_time(sleeper.pid)
        handles.append(handle)

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        on_handle(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    real_set_status = runner.tree.set_status
    state = {"raised": False}

    def set_status(agent_id, status, reason=""):
        if not state["raised"] and status == "running":
            state["raised"] = True
            raise RuntimeError("injected status fault")   # after registration
        return real_set_status(agent_id, status, reason)

    monkeypatch.setattr(runner.tree, "set_status", set_status)
    registered, forgotten = [], []
    real_register = runner.occupancy.register
    real_forget = runner.occupancy.forget

    def register(container, node_id, pid, pid_start):
        registered.append(node_id)
        return real_register(container, node_id, pid, pid_start)

    def forget(container, node_id):
        forgotten.append(node_id)
        return real_forget(container, node_id)

    monkeypatch.setattr(runner.occupancy, "register", register)
    monkeypatch.setattr(runner.occupancy, "forget", forget)

    try:
        result = asyncio.run(runner.start("worker", "q"))
        assert result.get("error"), result

        nodes = runner.tree.read()["nodes"]
        assert registered, "the launch never registered its occupancy"
        assert forgotten == [], (
            f"occupancy was forgotten on an exit-status file: {forgotten}")
        assert all(node in runner._locks for node in registered), (
            "the supervision lock was released on an exit-status file")
        with runner.startup._lock():
            records = runner.startup._read()
        assert (records.get("acme") or {}).get("runs"), (
            "the startup claim was released on an exit-status file")
        assert all(n.get("status") == "pending" for n in nodes.values()), (
            f"the slot was freed on an exit-status file: {nodes}")
        assert [e for e in _events(runner)
                if e.get("kind") == "launch_cleanup_failed"], (
            "the unconfirmable cleanup was not reported")
        with pytest.raises(RuntimeError, match="max_concurrent"):
            asyncio.run(runner.start("worker", "q"))
    finally:
        sleeper.kill()
        sleeper.wait()


# ---------------------------------------------------------------------------
# Finding 2 (P1): shared home policy launches at all
# ---------------------------------------------------------------------------

def test_a_launch_under_shared_home_policy_works(tmp_path, monkeypatch):
    """`identity` lived inside the per-agent branch, so
    `security.home_policy: shared` raised UnboundLocalError on every start,
    steer and turn."""
    runner, probes = _runner(
        tmp_path, monkeypatch,
        project={"security": {"home_policy": "shared"}})

    result = _start(runner)

    assert result.get("agent_id") and not result.get("error"), result
    node = runner.tree.get(result["agent_id"])
    assert node is not None and node.status != "pending", node


# ---------------------------------------------------------------------------
# Finding 3 (P2): death is asserted at the moment the cleanup returns
# ---------------------------------------------------------------------------

def test_death_is_asserted_immediately_when_the_cleanup_returns(
        tmp_path, monkeypatch):
    """Finding 3 (P2): no settling window, no polling after the cleanup —
    the process must be positively dead at the instant ownership is
    released. A process that only dies later fails this check."""
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)
    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    def on_handle(handle):
        handles.append(handle)

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        on_handle(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    real_track = runner._track_container_run
    state = {"cancelled": False}

    async def track_once(run, executor):
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError      # cancelled mid OOM-tracking
        return await real_track(run, executor)

    monkeypatch.setattr(runner, "_track_container_run", track_once)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.start("worker", "q"))

    handle = handles[0]
    # No sleep, no poll: the cleanup returned, so the process is judged now.
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the process was not positively dead when the cleanup returned")
    nodes = runner.tree.read()["nodes"]
    assert all(node_id not in runner._locks for node_id in nodes), runner._locks
    assert all(n.get("status") != "pending" for n in nodes.values()), nodes
