"""Regression tests for the round-8 review of batch M (reviewer ag-cef33c).

Four defects, one test each, under the still-binding RM-R1c:

1. P1: Run construction — and with it `_supervisor()`, which parses the
   limits and can raise on a malformed `doom_loop_repeats` — sat after
   `executor.start()` but outside both cleanup handlers. Everything that
   can fail is now built in the prologue, whose handler releases the
   caller's slot marker and the startup claim: a supervisor failure starts
   nothing and leaves nothing behind.
2. P1: a cleanup hold did not keep the concurrency slot — a failed retry's
   node kept its first attempt's dead pid and `running` status, so
   `_occupies_slot` discounted it. A node under `_cleanup_holds` counts as
   occupying in every admission counter and in `capacity()`.
3. P2: steer() takes the startup claim before `_launch()`, and a prologue
   failure did not release it. Every claim-taker now releases on every
   path: the prologue handler releases the claim beside the slot marker.
4. P2: a reading carrying `stale_seconds` skipped the future `read_at`
   guard. A `read_at` more than 300 s ahead makes the reading unknown
   whatever `stale_seconds` says (RM-R4d).

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import asyncio
import math
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents import budget as bmod  # noqa: E402
from multiagents import procs, runner as runner_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    OLD_SESSION, _budgets, _fakes, _project,
)


# ---------------------------------------------------------------------------
# Finding 1 (P1): everything that can fail is built before executor.start
# ---------------------------------------------------------------------------

def test_a_supervisor_failure_starts_nothing_and_leaves_nothing(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    runner = _project(tmp_path, monkeypatch, agent, providers, {})
    _budgets(monkeypatch, acme=1.0)

    def boom(*a, **kw):
        raise RuntimeError("malformed doom_loop_repeats")

    monkeypatch.setattr(runner, "_supervisor", boom)
    start_calls = {"n": 0}
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def counting_start(self, *args, **kwargs):
        start_calls["n"] += 1
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(executor_cls, "start", counting_start)

    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("error") and "malformed doom_loop_repeats" in result["error"], \
        result

    assert start_calls["n"] == 0, (
        "executor.start ran before the supervisor was built: the process "
        "was started into a launch whose construction then failed outside "
        "the cleanup's ownership")

    assert _calls(probes["acme"]) == [], (
        "the provider was launched before the supervisor was built")
    nodes = runner.tree.read()["nodes"]
    assert nodes, "the node should exist"
    assert all(n.get("status") == "failed" for n in nodes.values()), (
        f"the node was not marked failed: {nodes}")
    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records


# ---------------------------------------------------------------------------
# Finding 2 (P1): a cleanup hold keeps the concurrency slot
# ---------------------------------------------------------------------------

def test_a_cleanup_hold_keeps_the_concurrency_slot(tmp_path, monkeypatch):
    """A failed retry's node keeps its first attempt's dead pid and
    `running` status. While the launch cleanup holds it — death not
    confirmed — it counts as occupying, so the next start is refused."""
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("worker", "acme", "acme-large")
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"limits": {"max_concurrent": 1}})
    _budgets(monkeypatch, acme=1.0)

    node_id = "ag-held"
    runner.tree.add(Node(id=node_id, agent="worker", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="running", pid=999_999_999, pid_start="1"))
    runner.tree.update(node_id, cleanup_hold={
        "since": 1_800_000_000.0, "owner_pid": 1})
    assert not procs.alive(999_999_999, "1")

    with pytest.raises(RuntimeError, match="max_concurrent"):
        asyncio.run(runner.start("worker", "q"))
    assert runner.capacity()["running"] == 1, runner.capacity()

    # Once death is confirmed and the hold is lifted, the slot is free.
    runner.tree.update(node_id, cleanup_hold=None)
    result = asyncio.run(runner.start("worker", "q"))
    assert result.get("agent_id") and not result.get("error"), result


# ---------------------------------------------------------------------------
# Finding 3 (P2): steer's claim is released when the prologue fails
# ---------------------------------------------------------------------------

def test_steer_releases_its_claim_when_the_prologue_fails(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    agent = AgentSpec("advisor", "acme", "acme-large", conversational=True)
    runner = _project(tmp_path, monkeypatch, agent, providers, {})
    _budgets(monkeypatch, acme=1.0)
    worktree = runner.paths.worktree("ag-steered")
    worktree.mkdir(parents=True)
    runner.tree.add(Node(id="ag-steered", agent="advisor", provider="acme",
                         model="acme-large", parent=None, depth=1,
                         status="idle", session_id="sess-steered",
                         worktree=str(worktree), conversation=True, turns=1))

    def boom(*a, **kw):
        raise OSError("prepare_home exploded")

    monkeypatch.setattr(runner_mod, "prepare_home", boom)

    with pytest.raises(OSError, match="prepare_home exploded"):
        asyncio.run(runner.steer("ag-steered", "continue"))

    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), (
        f"steer's startup claim leaked past the prologue failure: {records}")
    assert runner.startup.availability("acme") is None, (
        "the claim leaked; the half-open probe is not claimable")


# ---------------------------------------------------------------------------
# Finding 4 (P2): a future read_at expires the reading, whatever
# stale_seconds says
# ---------------------------------------------------------------------------

def test_a_future_read_at_expires_the_reading_even_with_stale_seconds(
        tmp_path, monkeypatch):
    wall = 1_800_000_000.0
    monkeypatch.setattr(bmod.time, "time", lambda: wall)
    reading = bmod.Budget("acme", known=True, headroom=1.0,
                          stale_seconds=10.0, read_at=wall + 10_000.0)
    assert bmod._reading_age(reading, wall) == math.inf
    assert bmod._apply_reading_age(reading, 3600.0, wall).stale

    # Through the reader: the forged stamp routes the reading as unknown.
    raw = bmod.Budget("acme", known=True, headroom=1.0,
                      stale_seconds=10.0, read_at=wall + 10_000.0)
    monkeypatch.setattr(bmod, "_from_script", lambda *a, **kw: raw)
    bmod.invalidate_cache()
    served = bmod.read_provider("acme", object(), None, tmp_path,
                                max_reading_age=3600.0)
    assert served.stale, served
