"""Regression tests for the round-3 review of batch M (reviewer ag-f21a0c).

Five findings, one test each:

1. P1: cancelling start() after executor.start() succeeded left the process
   alive while the node read `failed` and the claims were released — a slot
   may go only once its process is stopped or supervised to the end.
2. P2: a cached `read_at` reading counted the cache time twice.
3. P2: the reading-age bookkeeping is now carried INSIDE the cache entry, so
   a replacement reading can never inherit a stale entry's age.
4. P2: the age floor froze ageing after a backward clock step; cache time is
   now accumulated from the wall clock's forward movement alone (the
   monotonic property RM-R4d asks for, without going blind to the synthetic
   time the tests drive through `time.time`).
5. P2: the post-claim effort settlement sits inside the claim-release
   `finally`, so an exception there cannot leak the startup record or the
   half-open probe.

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
from multiagents import procs  # noqa: E402
from multiagents.executor.base import running as _running  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    SUFFIXES, _budgets, _fakes, _project, _start,
)


# ---------------------------------------------------------------------------
# Finding 1 (P1): cancellation after executor.start succeeded
# ---------------------------------------------------------------------------

def test_cancelling_start_after_the_process_started_stops_it(tmp_path, monkeypatch):
    """The window between `executor.start` returning and the consumer task
    existing: a cancellation there must stop the process — not mark the node
    `failed` over a live one and let the next start book the slot twice."""
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"limits": {"max_concurrent": 1}})
    _budgets(monkeypatch, acme=1.0)

    handles = []
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def recording_start(self, *args, **kwargs):
        handle = await real_start(self, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(executor_cls, "start", recording_start)

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
    # Judged at the moment the cleanup returns; a reaped zombie is dead.
    assert not _running(handle.pid, getattr(handle, "pid_start", "")), (
        "the cancelled start left its process running unsupervised")
    nodes = runner.tree.read()["nodes"]
    assert nodes and all(n.get("status") in {"failed", "done", "running"}
                         for n in nodes.values()), nodes
    assert all(n.get("status") != "pending" for n in nodes.values()), (
        "a cancelled start left a node holding a slot")
    assert not runner.runs, runner.runs
    assert all(node_id not in runner._locks for node_id in nodes), runner._locks
    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records

    # The slot is genuinely free and the next start runs alone under cap 1.
    result = _start(runner)
    assert result.get("agent_id") and not result.get("error"), result
    follow = runner.tree.get(result["agent_id"])
    assert follow is not None and follow.status in {"running", "done", "stuck"}, follow


# ---------------------------------------------------------------------------
# Findings 2-4 (P2): the reading age in the cache
# ---------------------------------------------------------------------------

def test_a_cached_read_at_reading_is_not_aged_twice(tmp_path, monkeypatch):
    """Finding 2: a hit computed `now - read_at` and then added
    `now - cached_at` — the same cached time twice. The true age after the
    advance is 600 s; the double count reported 700."""
    wall = [1_800_000_000.0]
    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    monkeypatch.setattr(bmod, "_from_script", lambda *a, **kw: bmod.Budget(
        "acme", known=True, headroom=0.0, read_at=1_800_000_000.0 - 500))
    bmod.invalidate_cache()
    first = bmod.read_provider("acme", object(), None, tmp_path,
                               max_reading_age=650.0)
    assert not first.stale
    wall[0] += 100
    second = bmod.read_provider("acme", object(), None, tmp_path,
                                max_reading_age=650.0)
    assert not second.stale, (
        "the cached time was counted twice: 600 s old, reported 700")
    reading = bmod.Budget("acme", known=True, headroom=0.0,
                          read_at=1_800_000_000.0 - 500)
    assert bmod._reading_age(reading, wall[0], cached_at=1_800_000_000.0,
                             elapsed=100.0) == 600.0


def test_replacement_reading_gets_fresh_age_bookkeeping(tmp_path, monkeypatch):
    """Finding 3: the age bookkeeping belongs to the cache entry. A hit on a
    replaced reading must judge the NEW reading only."""
    wall = [1_800_000_000.0]
    raw = [bmod.Budget("acme", known=True, headroom=0.0, stale_seconds=3590.0)]
    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    monkeypatch.setattr(bmod, "_from_script", lambda *a, **kw: raw[0])
    bmod.invalidate_cache()

    first = bmod.read_provider("acme", object(), None, tmp_path,
                               max_reading_age=3600.0)
    assert not first.stale and first.stale_seconds == 3590.0

    wall[0] += 20
    raw[0] = bmod.Budget("acme", known=True, headroom=0.0, stale_seconds=10.0)
    fresh = bmod.read_provider("acme", object(), None, tmp_path,
                               max_reading_age=3600.0, force=True)
    assert not fresh.stale and fresh.stale_seconds == 10.0

    wall[0] += 20
    third = bmod.read_provider("acme", object(), None, tmp_path,
                               max_reading_age=3600.0)
    assert not third.stale, (
        "the replacement reading inherited the replaced entry's cache time")
    assert third.stale_seconds == 10.0, third


def test_a_backwards_step_does_not_freeze_a_cached_reading_age(tmp_path, monkeypatch):
    """Finding 4 (the reviewer's numbers): stale_seconds 3570, advance 20 s,
    step back 10 s, advance 11 s. RM-R4f superseded the floor and the
    served-expired answer: the hit that OBSERVES the step invalidates the
    entry and takes the ordinary fetch path IN THE SAME CALL — the
    invalidated budget is never returned, and the caller gets whatever the
    provider itself reports on the re-read (a floor would have reported
    3591; serving the entry would have reported stale)."""
    wall = [1_800_000_000.0]
    calls = {"n": 0}

    def script(*a, **kw):
        calls["n"] += 1
        return bmod.Budget("acme", known=True, headroom=0.0, stale_seconds=3570.0)

    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    monkeypatch.setattr(bmod, "_from_script", script)
    bmod.invalidate_cache()

    assert not bmod.read_provider("acme", object(), None, tmp_path,
                                  max_reading_age=3600.0).stale
    wall[0] += 20
    assert not bmod.read_provider("acme", object(), None, tmp_path,
                                  max_reading_age=3600.0).stale
    wall[0] -= 10
    third = bmod.read_provider("acme", object(), None, tmp_path,
                               max_reading_age=3600.0)
    assert calls["n"] == 2, (
        "the observed backward step did not invalidate and re-read in the "
        "same call")
    assert not third.stale, third
    wall[0] += 11
    fourth = bmod.read_provider("acme", object(), None, tmp_path,
                                max_reading_age=3600.0)
    assert calls["n"] == 2, fourth        # a hit on the re-read's own entry
    assert not fourth.stale, fourth


# ---------------------------------------------------------------------------
# Finding 5 (P2): an exception during the post-claim effort settlement
# ---------------------------------------------------------------------------

def test_an_exception_during_effort_settlement_releases_the_startup_claim(
        tmp_path, monkeypatch):
    """The settlement runs inside the claim-release `finally`: an exception
    there releases the claim and leaves the half-open probe claimable."""
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-medium", "effort": "low"})
    runner = _project(tmp_path, monkeypatch, agent, providers, {})
    _budgets(monkeypatch, acme=1.0)

    def boom(spec, provider, node_id):
        raise RuntimeError("settlement exploded")

    monkeypatch.setattr(runner, "_settle_effort", boom)

    with pytest.raises(RuntimeError, match="settlement exploded"):
        asyncio.run(runner.start("worker", "q"))

    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), (
        f"the claim leaked past the settlement exception: {records}")
    assert runner.startup.availability("acme") is None, (
        "the half-open probe is still held")
    assert not runner.tree.read()["nodes"], (
        "the settlement runs before any node exists")
