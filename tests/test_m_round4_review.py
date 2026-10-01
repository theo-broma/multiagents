"""Regression tests for the round-4 review of batch M (reviewer ag-53986b).

Four findings, one test each, against the amended contract
(context/specs/m-routing-fixes.md, RM-R1b and RM-R4e):

1. P1: a second cancellation during the failed-launch cleanup skipped the
   supervision lock's release while start() freed the slot. RM-R1b: the
   whole cleanup is shielded, and the claim goes only after it has run.
2. P2: a launch that registered its container occupancy and then failed
   never called `occupancy.forget()`.
3. P2 (RM-R4e): a hit that observes the clock behind the entry has caught a
   backward step; the entry is permanently expired — it answers unknown and
   is dropped, so the next access re-reads the provider and no forward
   recovery ever makes the expired entry authoritative again.
4. P2 (RM-R4e): concurrent hits serialise their updates, so `seen` never
   moves backwards and no interval is counted twice.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import budget as bmod  # noqa: E402
from multiagents import procs  # noqa: E402
from multiagents.executor.base import running as _running  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    _budgets, _fakes, _project, _start,
)


def _runner(tmp_path, monkeypatch, *, cap=None):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    project = {"limits": {"max_concurrent": cap}} if cap else {}
    runner = _project(tmp_path, monkeypatch, agent, providers, project)
    _budgets(monkeypatch, acme=1.0)
    return runner, probes


# ---------------------------------------------------------------------------
# Findings 1-2 (RM-R1b): the failed-launch cleanup
# ---------------------------------------------------------------------------

def test_a_second_cancellation_during_the_stop_skips_nothing(tmp_path, monkeypatch):
    """Finding 1, restated under RM-R1c (binding; it superseded the retry
    design this test first pinned): the stop's call is cancelled after the
    kill has landed. The cleanup's releases are decided by the death
    CONFIRMATION, never by the stop's outcome — the lock, the occupancy and
    the slot are all released, and nothing is left `pending`."""
    runner, probes = _runner(tmp_path, monkeypatch, cap=1)

    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def flaky_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        real_stop = handle.stop
        state = {"raised": False}

        async def stop_cancelled_during_settling():
            if not state["raised"]:
                state["raised"] = True
                try:
                    await real_stop()                  # the kill lands...
                finally:
                    raise asyncio.CancelledError       # ...the call is cancelled
            return await real_stop()

        handle.stop = stop_cancelled_during_settling
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", flaky_start)

    real_track = runner._track_container_run
    state = {"cancelled": False}

    async def track_once(run, executor):
        if not state["cancelled"]:
            state["cancelled"] = True
            raise asyncio.CancelledError          # the FIRST cancellation
        return await real_track(run, executor)

    monkeypatch.setattr(runner, "_track_container_run", track_once)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.start("worker", "q"))

    handle = handles[0]
    # Death is asserted at the moment the cleanup returns (RM-R1c, review
    # ag-b859f1): no settling window, no polling. `_running` is zombie-aware,
    # so a process that only dies LATE fails this check.
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the process was not positively dead when the cleanup returned")
    nodes = runner.tree.read()["nodes"]
    assert all(node_id not in runner._locks for node_id in nodes), (
        f"the supervision lock survived the cleanup: {runner._locks}")
    assert all(n.get("status") != "pending" for n in nodes.values()), nodes
    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records

    result = _start(runner)                      # the slot is genuinely free
    assert result.get("agent_id") and not result.get("error"), result


def test_a_failed_launch_forgets_the_registered_occupancy(tmp_path, monkeypatch):
    """Finding 2: once the tracking registered the run in the shared
    occupancy record, a launch that fails after that forgets it."""
    runner, probes = _runner(tmp_path, monkeypatch)

    executor_cls = type(runner.executor())
    monkeypatch.setattr(executor_cls, "oom_kill_count", lambda self: 7,
                        raising=False)
    monkeypatch.setattr(executor_cls, "container", "docker/test-container",
                        raising=False)

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

    real_set_status = runner.tree.set_status
    state = {"raised": False}

    def set_status(agent_id, status, reason=""):
        if not state["raised"] and status == "running":
            state["raised"] = True
            raise RuntimeError("injected status fault")
        return real_set_status(agent_id, status, reason)

    monkeypatch.setattr(runner.tree, "set_status", set_status)

    result = _start(runner)
    assert state["raised"], "the launch never reached the faulted status write"
    assert registered, "the launch never registered its occupancy"
    assert result.get("error"), result
    assert forgotten == registered, (
        f"registered {registered} but forgot {forgotten}: the occupancy "
        f"record keeps a run that was stopped")


# ---------------------------------------------------------------------------
# Findings 3-4 (RM-R4e): the cache age under a backward clock
# ---------------------------------------------------------------------------

def test_an_observed_backward_step_expires_the_entry_permanently(
        tmp_path, monkeypatch):
    """Finding 3, restated under RM-R4f (the binding design superseded
    RM-R4e's serve-then-drop): a hit that sees the clock behind the entry
    invalidates it and re-reads IN THE SAME CALL — the invalidated budget is
    never returned, the caller gets the fetched one. This holds whatever the
    invalidated entry's age metadata says, including none at all."""
    wall = [1_800_000_000.0]
    raw = [bmod.Budget("acme", known=True, headroom=0.0, stale_seconds=0.0)]
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

    for ageless in (False, True):
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
            f"ageless={ageless}: the observed backward step did not re-read "
            f"in the same call")
        assert third.headroom == 0.9, (
            f"ageless={ageless}: the invalidated budget was returned instead "
            f"of the fetched one: {third}")
        assert not third.stale, third


def test_concurrent_hits_never_move_seen_backwards(tmp_path, monkeypatch):
    """Finding 4: two hits arriving with different wall clocks serialise
    their updates. Afterwards either the entry was expired (the earlier hit
    observed the step) or `seen` stands at the later clock and exactly one
    interval was counted — never both, and never a backwards `seen`."""
    real_time = time.time
    t0 = 1_800_000_000.0
    budget = bmod.Budget("acme", known=True, headroom=1.0)
    tmp = Path(tmp_path)

    # Edited by the opus PS run (review ag-e6b702): the cache reads its
    # clock UNDER `_cache_lock`, so the fake clock may no longer block inside
    # the read — a barrier there deadlocks against the lock. The two hits are
    # released together by a barrier BEFORE either takes the lock, and each
    # thread's first clock read returns its own wall; which one reaches the
    # lock first is still left to the race, and the property is unchanged.
    for _ in range(100):
        walls: dict[int, float] = {}
        barrier = threading.Barrier(2, timeout=10)
        first_read: set[int] = set()

        def fake_time():
            ident = threading.get_ident()
            if ident in walls and ident not in first_read:
                first_read.add(ident)
                return walls[ident]
            return real_time()

        monkeypatch.setattr(bmod.time, "time", fake_time)
        bmod.invalidate_cache()
        bmod._cache["acme"] = bmod._CacheEntry(t0, budget)
        bmod._cache_source["acme"] = str(tmp)

        def hit(late: bool):
            ident = threading.get_ident()
            walls[ident] = t0 + 30 if late else t0 + 20
            barrier.wait()
            bmod.read_provider("acme", object(), None, tmp,
                               max_reading_age=3600.0)

        threads = [threading.Thread(target=hit, args=(False,)),
                   threading.Thread(target=hit, args=(True,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
        monkeypatch.setattr(bmod.time, "time", real_time)

        entry = bmod._cache.get("acme")
        if entry is None:
            continue        # dropped entirely: fine
        if not entry.budget.known:
            # RM-R4f: the earlier hit observed the step, invalidated the
            # entry, and the same call's re-read published its own
            # replacement (an unknown reading — the fetch has no provider).
            assert entry.elapsed == 0.0, entry
            continue
        latest = t0 + 30
        assert entry.seen == latest, (
            f"seen moved backwards: {entry.seen}, expected {latest}")
        assert entry.elapsed == latest - t0, (
            f"the interval between the hits was counted twice: "
            f"{entry.elapsed}, expected {latest - t0}")
