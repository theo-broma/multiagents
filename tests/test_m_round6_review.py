"""Regression tests for the round-6 review of batch M (reviewer ag-f7ced5).

Three defects, one test each, against the still-binding RM-R1c:

1. P1: the death check treated an expired Docker liveness grace period as
   death — `wrapper_alive()` reports None for an unanswerable container, and
   the FollowHandle's `_alive` turns that into False after
   `UNKNOWN_ALIVE_SECONDS`. "Unknown" is never "dead": ownership is released
   only on a positive confirmation (the wrapper's own exit verdict, the pid
   identity gone, or the executor's RAW container probe saying dead), and
   otherwise the cleanup keeps holding and reports.
2. P1: the outer consumer's `finally` released the supervision lock and set
   `run.done` even while a launch cleanup held the node — a silent-failure
   retry whose process could not be confirmed dead would be handed over
   alive. The `finally` honours the hold.
3. P2: `in_launch` took effect before `_launch` had established its cleanup
   ownership, so a `prepare_home()` failure left the node `pending` for
   ever. The prologue now releases the caller's slot marker itself, so the
   flag taking effect at `_launch`'s entry is safe.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    _budgets, _events, _fakes, _project,
)


def _runner(tmp_path, monkeypatch, *, cap=None):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    project = {"limits": {"max_concurrent": cap}} if cap else {}
    runner = _project(tmp_path, monkeypatch, agent, providers, project)
    _budgets(monkeypatch, acme=1.0)
    return runner, probes


# ---------------------------------------------------------------------------
# Finding 1 (P1): unknown liveness is never death
# ---------------------------------------------------------------------------

def test_an_unanswerable_container_holds_ownership_instead_of_dying(
        tmp_path, monkeypatch):
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)
    monkeypatch.setattr(runner_mod, "LAUNCH_CONFIRM_SECONDS", 0.6)
    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    def on_handle(handle):
        # The grace-expired signal the old death check trusted: the Docker
        # liveness grace has lapsed, so `_alive` reports False even though
        # the only truth available is "unknown".
        handle._alive = lambda: False
        handles.append(handle)

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        on_handle(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    # The container cannot be asked: the RAW answer is None, unknown.
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "docker/test-container",
                        raising=False)
    monkeypatch.setattr(executor_cls, "wrapper_alive",
                        lambda self, agent_id: None, raising=False)

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

    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("error"), result

    nodes = runner.tree.read()["nodes"]
    assert registered and forgotten == [], (
        f"registered {registered}, forgot {forgotten}: occupancy was "
        f"released over an unconfirmed death")
    assert all(node in runner._locks for node in registered), (
        "the supervision lock was released on an unknown answer")
    with runner.startup._lock():
        records = runner.startup._read()
    assert (records.get("acme") or {}).get("runs"), (
        "the startup claim was released on an unknown answer")
    assert all(n.get("status") == "pending" for n in nodes.values()), (
        f"the slot was freed on an unknown answer: {nodes}")
    assert [e for e in _events(runner)
            if e.get("kind") == "launch_cleanup_failed"], (
        "the unconfirmable cleanup was not reported")

    # The held slot is honestly held.
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner.start("worker", "q"))


# ---------------------------------------------------------------------------
# Finding 2 (P1): the outer consumer's finally honours the hold
# ---------------------------------------------------------------------------

class _EndedHandle:
    """A handle whose stream has ended cleanly: no lines, exit 0."""

    offset = 0
    pid = None
    pid_start = ""
    final_result = True

    def __init__(self, run_dir):
        self.run_dir = run_dir

    async def lines(self):
        return
        yield                                  # pragma: no cover

    async def drain_stderr(self):
        pass

    async def wait(self):
        return 0


def test_the_consumer_finally_honours_the_cleanup_hold(tmp_path, monkeypatch):
    """Finding 2 (P1): while a launch cleanup holds the node, `_consume`'s
    finally releases nothing — not the supervision lock, not `run.done`."""
    runner, probes = _runner(tmp_path, monkeypatch)
    spec = AgentSpec("worker", "acme", "acme-large")
    provider = runner.providers["acme"]
    node_id = "ag-consumer"
    run_dir = runner.paths.run_dir(node_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    runner.tree.add(Node(id=node_id, agent="worker", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="running", worktree=""))
    supervisor = runner._supervisor(spec, provider, 900, 180)

    async def consume():
        run = runner_mod.Run(node_id=node_id, provider=provider, spec=spec,
                             handle=_EndedHandle(run_dir), supervisor=supervisor,
                             limits={"timeout": {"value": 900},
                                     "silence_timeout": {"value": 180}})
        run.final_result = True
        await runner._consume(run)
        return run

    # Control: without a hold the finally releases the lock and wakes waiters.
    run = asyncio.run(consume())
    assert node_id not in runner._locks, "the control case kept the lock"
    assert run.done.is_set(), "the control case never signalled done"

    # The hold: the very same teardown releases nothing. RM-R1d: the hold
    # is the durable node field, not a process's memory.
    runner.tree.update(node_id, cleanup_hold={
        "since": 1_800_000_000.0, "owner_pid": 1})
    assert runner._claim(node_id)
    run = asyncio.run(consume())
    assert node_id in runner._locks, (
        "the consumer's finally released the supervision lock over a held "
        "run")
    assert not run.done.is_set(), (
        "the consumer's finally woke the waiters of a held run")


# ---------------------------------------------------------------------------
# Finding 3 (P2): the prologue releases the caller's slot marker itself
# ---------------------------------------------------------------------------

def test_a_prologue_failure_marks_the_node_failed(tmp_path, monkeypatch):
    """Finding 3 (P2): `in_launch` takes effect at `_launch`'s entry, so
    `_launch` itself must release the caller's slot marker when its
    prologue — here `prepare_home()` — fails. Otherwise the node sits
    `pending` for ever, holding a slot no run will account for. The fault
    is an OSError on purpose: a RuntimeError would be caught and marked by
    start()'s inner launch-failure handler, masking the finding."""
    runner, probes = _runner(tmp_path, monkeypatch)

    def boom(*a, **kw):
        raise OSError("prepare_home exploded")

    monkeypatch.setattr(runner_mod, "prepare_home", boom)
    real_start = type(runner.executor()).start

    async def never(self, *args, **kwargs):
        raise AssertionError("the launch must not reach executor.start")

    monkeypatch.setattr(type(runner.executor()), "start", never)

    with pytest.raises(OSError, match="prepare_home exploded"):
        asyncio.run(runner.start("worker", "q"))
    nodes = runner.tree.read()["nodes"]
    assert nodes, "the node should exist"
    assert all(n.get("status") == "failed" for n in nodes.values()), (
        f"a prologue failure left the node holding its slot: {nodes}")
    assert not any(procs.alive(n.get("pid"), n.get("pid_start") or "")
                   for n in nodes.values() if n.get("pid"))
