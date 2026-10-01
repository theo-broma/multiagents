"""Regression tests for the round-5 review of batch M (reviewer ag-467011).

Two areas, redesigned and binding (context/specs/m-routing-fixes.md,
RM-R1c and RM-R4f):

- RM-R1c: ONE cleanup task stops the process, CONFIRMS its death, then
  releases the supervision lock, the container occupancy, the startup claim
  and the reserved concurrency slot, and only then drops tracking and
  signals `run.done`. The caller awaits `asyncio.shield(the_same_task)`
  through any number of its own cancellations — no retry limit, no
  replacement tasks, nothing detached, no unobserved exception. Death that
  cannot be confirmed holds ownership and is reported.
- RM-R4f: under `_cache_lock`, identity is checked first; a hit that
  observes a backward step invalidates the entry and takes the ordinary
  fetch path in the same call — the invalidated budget is never returned,
  with or without age metadata; the replacement budget and its identity
  publish together under the lock, with provider I/O outside it.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import budget as bmod  # noqa: E402
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.executor.base import running as _running  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    _budgets, _events, _fakes, _project, _start,
)


def _runner(tmp_path, monkeypatch, *, cap=None):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    project = {"limits": {"max_concurrent": cap}} if cap else {}
    runner = _project(tmp_path, monkeypatch, agent, providers, project)
    _budgets(monkeypatch, acme=1.0)
    return runner, probes


def _launch_with_grabbing_handle(runner, monkeypatch, handles, on_handle=None):
    """executor.start records its handle and hands it to `on_handle`, whose
    instance patches shadow the class, so a test can bend stop or the
    liveness probe for one launch."""
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def patched_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        if on_handle is not None:
            on_handle(handle)
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", patched_start)


def _fault_first_track(runner, monkeypatch):
    """The first launch faults at the OOM-tracking await, exactly in the
    window RM-R1c governs; later launches are untouched."""
    real_track = runner._track_container_run
    state = {"cancelled": False}

    async def track_once(run, executor):
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError
        return await real_track(run, executor)

    monkeypatch.setattr(runner, "_track_container_run", track_once)
    return state


# ---------------------------------------------------------------------------
# RM-R1c: the one cleanup task
# ---------------------------------------------------------------------------

def test_one_cleanup_task_releases_only_after_confirmed_death(
        tmp_path, monkeypatch):
    """Findings 1 and 2 (P1, P3): cancellations land repeatedly while the
    cleanup's stop is in flight. Exactly ONE stop attempt runs — no
    replacement tasks — the caller keeps shielding the same task, nothing is
    detached, and the lock, occupancy, claim and slot are released only once
    death is confirmed."""
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)
    handles = []
    stop_calls = {"n": 0}

    def on_handle(handle):
        real_stop = handle.stop

        async def counting_stop(*a, **kw):
            stop_calls["n"] += 1
            await asyncio.sleep(0.15)       # cancellations land in here
            return await real_stop(*a, **kw)

        handle.stop = counting_stop
        handles.append(handle)

    _launch_with_grabbing_handle(runner, monkeypatch, handles, on_handle)
    state = _fault_first_track(runner, monkeypatch)

    async def run():
        task = asyncio.create_task(runner.start("worker", "q"))
        while not state["cancelled"]:
            await asyncio.sleep(0.005)
        cancels = 0
        while not task.done():
            task.cancel()
            cancels += 1
            await asyncio.sleep(0.02)
        outcome = await asyncio.gather(task, return_exceptions=True)
        return cancels, outcome

    cancels, outcome = asyncio.run(run())
    assert cancels > 0, "the canceller never fired"
    assert any(isinstance(o, asyncio.CancelledError) for o in outcome), outcome
    assert stop_calls["n"] == 1, (
        f"the stop ran {stop_calls['n']} times: replacement tasks or "
        f"unobserved attempts")
    handle = handles[0]
    # Death is asserted at the moment the cleanup returns (RM-R1c, review
    # ag-b859f1): no settling window, no polling. `_running` is zombie-aware,
    # so a process that only dies LATE fails this check.
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the process was not positively dead when the cleanup returned")
    nodes = runner.tree.read()["nodes"]
    assert all(node_id not in runner._locks for node_id in nodes), runner._locks
    assert all(n.get("status") != "pending" for n in nodes.values()), nodes
    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records

    result = _start(runner)             # the slot is genuinely free
    assert result.get("agent_id") and not result.get("error"), result


def test_death_that_cannot_be_confirmed_holds_ownership_and_reports(
        tmp_path, monkeypatch):
    """Finding 1 (P1): nothing is released merely because stop returned.
    When death cannot be confirmed — the container cannot be asked, so the
    RAW probe answers None, unknown — ownership (the supervision lock), the
    claim, the occupancy and the slot are all held, and the cleanup failure
    is reported. (Repointed at RM-R1c's round-6 refinement: the confirmation
    asks the executor's raw probe, never the FollowHandle's grace-period
    `_alive`, which reports unknown as dead.)"""
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)
    monkeypatch.setattr(runner_mod, "LAUNCH_CONFIRM_SECONDS", 0.6)
    handles = []
    executor_cls = type(runner.executor())
    monkeypatch.setattr(executor_cls, "wrapper_alive",
                        lambda self, agent_id: None, raising=False)
    real_start = executor_cls.start

    async def grabbing_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", grabbing_start)
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "docker/test-container",
                        raising=False)
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
    assert result.get("error") and "injected status fault" in result["error"], \
        result

    nodes = runner.tree.read()["nodes"]
    assert registered, "the launch never registered its occupancy"
    assert forgotten == [], (
        f"occupancy was forgotten without confirmed death: {forgotten}")
    assert all(node in runner._locks for node in registered), (
        "the supervision lock was released without confirmed death")
    with runner.startup._lock():
        records = runner.startup._read()
    assert (records.get("acme") or {}).get("runs"), (
        "the startup claim was released without confirmed death")
    assert all(n.get("status") == "pending" for n in nodes.values()), (
        f"the slot was freed without confirmed death: {nodes}")
    reported = [e for e in _events(runner)
                if e.get("kind") == "launch_cleanup_failed"]
    assert reported, "the cleanup failure was not reported"

    # And the held slot is honestly held: the next start is refused.
    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner.start("worker", "q"))


# ---------------------------------------------------------------------------
# RM-R4f: the cache hit
# ---------------------------------------------------------------------------

def test_a_backward_step_invalidates_and_rereads_in_the_same_call(
        tmp_path, monkeypatch):
    """Finding 3 (P2): a hit that observes the clock behind the entry
    invalidates it and takes the ordinary fetch path in the same call. The
    invalidated budget is never returned — the caller gets the fetched
    one — whatever the invalidated entry's age metadata said."""
    wall = [1_800_000_000.0]
    raw = [bmod.Budget("acme", known=True, headroom=0.0)]
    calls = {"n": 0}

    def script(*a, **kw):
        calls["n"] += 1
        return raw[0]

    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    monkeypatch.setattr(bmod, "_from_script", script)
    bmod.invalidate_cache()

    def read():
        return bmod.read_provider("acme", object(), None, tmp_path,
                                  max_reading_age=3600.0)

    for ageless in (True, False):
        bmod.invalidate_cache()
        raw[0] = (bmod.Budget("acme", known=True, headroom=0.0)
                  if ageless else
                  bmod.Budget("acme", known=True, headroom=0.0, stale_seconds=0.0))
        calls["n"] = 0
        assert not read().stale and calls["n"] == 1
        wall[0] += 10
        assert not read().stale and calls["n"] == 1     # a hit
        wall[0] -= 30                                   # the step, observed
        raw[0] = bmod.Budget("acme", known=True, headroom=0.9, stale_seconds=0.0)
        third = read()
        assert calls["n"] == 2, (
            f"ageless={ageless}: the observed backward step did not take the "
            f"fetch path in the same call")
        assert third.headroom == 0.9, (
            f"ageless={ageless}: the invalidated budget was returned instead "
            f"of the fetched one: {third}")
        assert not third.stale, third
        wall[0] += 40                                   # a hit on the new entry
        assert read().headroom == 0.9 and calls["n"] == 2


def test_provider_io_runs_outside_the_lock_and_identity_publishes(
        tmp_path, monkeypatch):
    """Finding 4 (P2): provider I/O happens outside `_cache_lock` — a fetch
    in flight can take it — and what a read returns is always the reading
    of the config-dir identity the caller asked for."""
    dir_a = Path(tmp_path) / "a"
    dir_b = Path(tmp_path) / "b"
    lock_free = {"ok": True}
    real_lock = bmod._cache_lock

    def script(name, provider, executor, config_dir, project_config, **kw):
        if not real_lock.acquire(timeout=2):
            lock_free["ok"] = False
        else:
            real_lock.release()
        room = 0.9 if str(config_dir) == str(dir_a) else 0.1
        return bmod.Budget("acme", known=True, headroom=room)

    monkeypatch.setattr(bmod, "_from_script", script)
    bmod.invalidate_cache()

    for round_no in range(20):
        for config_dir, room in ((dir_a, 0.9), (dir_b, 0.1)):
            bmod.invalidate_cache()
            reading = bmod.read_provider("acme", object(), None, config_dir)
            assert reading.headroom == room, (
                f"round {round_no}: config {config_dir} got {reading.headroom}")
    assert lock_free["ok"], "provider I/O ran inside the cache lock"
