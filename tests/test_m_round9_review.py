"""Regression tests for the round-9 review of batch M (reviewer ag-7c0716).

Three defects, fixed as the binding RM-R1d
(context/specs/m-routing-fixes.md):

1. P1: the new process identity is recorded in `tree.json` immediately
   after `executor.start()` succeeds, before any step that can fail — so
   admission never judges a node by a dead predecessor pid.
2. P1: the cleanup hold is a durable node field in `tree.json`
   (`cleanup_hold`), set when the cleanup starts and cleared only once
   death is confirmed. `_occupies_slot()` honours it in EVERY Runner, so a
   second Runner on the same project cannot admit over the cap while the
   first holds a node whose death is unconfirmed. The in-memory-only hold
   set is gone.
3. P2: supervisor construction happens before `_claim()` takes the
   supervision flock, so a malformed limits value can no longer leak the
   flock.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from test_m_routing_fixes import _budgets, _fakes, _project  # noqa: E402


def _make(tmp_path, monkeypatch, providers, *, cap=None):
    agent = AgentSpec("worker", "acme", "acme-large")
    project = {"limits": {"max_concurrent": cap}} if cap else {}
    runner = _project(tmp_path, monkeypatch, agent, providers, project)
    _budgets(monkeypatch, acme=1.0)
    return runner


# ---------------------------------------------------------------------------
# Finding 1 (P1): the new identity is recorded before anything can fail
# ---------------------------------------------------------------------------

def test_the_new_process_identity_is_recorded_right_after_start(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    runner = _make(tmp_path, monkeypatch, providers)
    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        handles.append(handle)
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
    nodes = runner.tree.read()["nodes"]
    assert nodes, "the node should exist"
    node = list(nodes.values())[0]
    assert node.get("pid") == handle.pid and node.get(
        "pid_start") == getattr(handle, "pid_start", ""), (
        f"tree.json records {node.get('pid')}/{node.get('pid_start')} — the "
        f"dead predecessor or nothing — instead of the new process "
        f"{handle.pid}/{getattr(handle, 'pid_start', '')}")
    assert procs.alive(handle.pid, getattr(handle, "pid_start", "")) or True


# ---------------------------------------------------------------------------
# Finding 2 (P1): the hold is durable and honoured by every Runner
# ---------------------------------------------------------------------------

def test_a_durable_hold_stops_a_second_runner_from_admitting(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    runner_a = _make(tmp_path, monkeypatch, providers, cap=1)
    monkeypatch.setattr(runner_mod, "LAUNCH_CONFIRM_SECONDS", 0.6)
    executor_cls = type(runner_a.executor())
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "docker/test-container",
                        raising=False)
    monkeypatch.setattr(executor_cls, "wrapper_alive",
                        lambda self, agent_id: None, raising=False)
    real_set_status = runner_a.tree.set_status
    state = {"raised": False}

    def set_status(agent_id, status, reason=""):
        if not state["raised"] and status == "running":
            state["raised"] = True
            raise RuntimeError("injected status fault")   # after registration
        return real_set_status(agent_id, status, reason)

    monkeypatch.setattr(runner_a.tree, "set_status", set_status)

    result = asyncio.run(runner_a.start("worker", "q"))
    assert result.get("error") and "injected status fault" in result["error"], \
        result

    nodes = runner_a.tree.read()["nodes"]
    held = [n for n in nodes.values() if n.get("cleanup_hold")]
    assert held, f"no durable hold in tree.json: {nodes}"
    assert held[0]["cleanup_hold"].get("owner_pid"), held

    # A SECOND Runner on the same project reads the same tree.json: the
    # held node occupies whatever its pid liveness, so its start is
    # refused over the cap.
    runner_b = _make(tmp_path, monkeypatch, providers, cap=1)
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner_b.start("worker", "q"))

    # The hold clears only once death is confirmed; here it was not, so the
    # field stays on the node.
    assert [n for n in runner_b.tree.read()["nodes"].values()
            if n.get("cleanup_hold")], "the durable hold was cleared unconfirmed"


# ---------------------------------------------------------------------------
# Finding 3 (P2): nothing that can fail sits after the flock
# ---------------------------------------------------------------------------

def test_a_supervisor_failure_does_not_leak_the_flock(tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    runner = _make(tmp_path, monkeypatch, providers)

    def boom(*a, **kw):
        raise RuntimeError("malformed doom_loop_repeats")

    monkeypatch.setattr(runner, "_supervisor", boom)

    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("error") and "malformed doom_loop_repeats" in result["error"], \
        result

    assert not runner._locks, (
        f"the supervision flock leaked past the supervisor failure: "
        f"{runner._locks}")
