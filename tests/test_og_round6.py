"""OG round 6 (review ag-12bc6a): a recorded provider name becomes a route once,
where it becomes one.

A promotion or a handover attempt keeps the names it was written under. When
one of them is read back as a launch target — the rollback to `from`, the
recovery of a waiting promotion or of a prepared attempt to `to` — it is the
route that name became, never the pre-rename name, which reaches either no
provider block at all or a block that is not a route. A promotion refusal
written under the old name is the route's refusal.

The rename here is a test-only declaration on the C23 fixture providers, so
nothing depends on a shipped provider. Placeholders only.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from test_c23_quota_handover import World  # noqa: E402

from multiagents.renames import Renames  # noqa: E402
from multiagents.tree import Node  # noqa: E402

OLD = "placeholder-alpha-old"
NEW = "alpha"
SESSION = "placeholder-session"


def _rename(w):
    w.mp.setattr(w.r.tree, "renames", Renames(aliases={OLD: NEW}, unroutable=frozenset()))


def _world(tmp_path, monkeypatch, old_block=False):
    """`old_block`: the old name still has a block of its own, as a CLI base
    left behind by a rename does; it is not a route."""
    w = World(tmp_path, monkeypatch)
    if old_block:
        w.providers[OLD] = {**w.providers[NEW], "routable": False}
        w.reload()
    _rename(w)
    return w


def _launches(w):
    """Record what `_qh_switch` launches, without spawning anything."""
    launched = []

    async def launch(**kwargs):
        launched.append(kwargs)
        return SimpleNamespace(stop_requested=False)

    w.mp.setattr(w.r, "_launch", launch)
    return launched


def _spec(w, name=NEW):
    return w.r._usable_spec(w.r.config.agent("worker"), name)


def _segment(w, provider):
    return {"provider": provider, "account": None, "model": "model-a", "session_id": SESSION,
            "started_at": w.clock[0] - 3600, "ended_at": None, "end_reason": "", "usage": {}}


def _node(w, provider, **fields):
    worktree = w.tmp / "worktree"
    worktree.mkdir(exist_ok=True)
    node = Node(id="ag-0ld6a1", agent="worker", provider=provider, model="model-a",
                parent=None, depth=0, task="placeholder task", status="running",
                session_id=SESSION, worktree=str(worktree),
                segments=[_segment(w, provider)], **fields)
    w.r.tree.add(node)
    return w.r.tree.get(node.id)


def _promotion(w, source, target, **fields):
    return {"from": source, "to": target, "session_id": SESSION, "from_rank": 1, "to_rank": 0,
            "segment": 2, "attempt": 1, "reason": "moved to a higher priority instance",
            "read_at": w.clock[0] - 60, "triggered_at": w.clock[0] - 60, "phase": "waiting",
            "owner_pid": 0, "owner_start": "", **fields}


# (a) rollback of a run adopted under the old name ---------------------------

@pytest.mark.parametrize("old_block", [False, True], ids=["no-block", "cli-base-block"])
def test_og_r6a_a_rollback_restores_an_old_name_run_on_its_route(tmp_path, monkeypatch, old_block):
    w = _world(tmp_path, monkeypatch, old_block=old_block)
    launched = _launches(w)
    node = _node(w, OLD)                     # adopted after the upgrade, never relaunched
    promotion = _promotion(w, OLD, "beta")
    w.r.tree.update(node.id, promotion=promotion)

    restored = asyncio.run(w.r._qh_promotion_failed(node.id, promotion, _spec(w), None,
                                                    "target launch refused"))

    assert restored is True
    [launch] = launched
    assert launch["provider"].name == NEW
    assert launch["spec"].provider == NEW
    assert launch["session_id"] == SESSION   # the stopped source session, resumed
    after = w.r.tree.get(node.id)
    assert len(after.segments) == 1          # a same-route rollback is not a new segment
    assert after.handover_attempt["new_segment"] is False
    assert after.handover_attempt["to"] == NEW
    assert after.handover_attempt["promotion_rollback"] == promotion
    assert after.provider == NEW


# (b) recovery of pre-upgrade state whose `to` is the old name ---------------

def test_og_r6b_a_waiting_promotion_to_the_old_name_launches_on_the_route(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    launched = _launches(w)
    node = _node(w, "beta")
    promotion = _promotion(w, "beta", OLD)
    w.r.tree.update(node.id, promotion=promotion)
    monkeypatch.setattr(w.r, "authoritative", lambda node, action: node)

    async def stopped(*a, **kw):
        return {}

    monkeypatch.setattr(w.r, "stop", stopped)

    asyncio.run(w.r._qh_promote(node.id, promotion, w.r._qh_settings()))

    assert [e for e in w.events("promote_failed")] == []
    [launch] = launched
    assert launch["provider"].name == NEW
    assert launch["spec"].provider == NEW
    after = w.r.tree.get(node.id)
    assert after.provider == NEW
    assert after.handover_attempt["to"] == NEW
    assert [s["provider"] for s in after.segments] == ["beta", NEW]


@pytest.mark.parametrize("old_block", [False, True], ids=["no-block", "cli-base-block"])
def test_og_r6b_a_prepared_attempt_to_the_old_name_recovers_on_the_route(tmp_path, monkeypatch,
                                                                       old_block):
    w = _world(tmp_path, monkeypatch, old_block=old_block)
    launched = _launches(w)
    node = _node(w, "beta")
    source = _spec(w, "beta")
    source_spec = {**asdict(source), "set_fields": sorted(source.set_fields or ())}
    attempt = {"from": "beta", "to": OLD, "tier": 1, "segment": 2, "new_segment": True,
               "attempt": 1, "session_id": SESSION, "resume": True, "state": "prepared",
               "reason": "quota", "source_spec": source_spec, "source_pid": None,
               "source_limits": None, "source_launched_at": None, "source_prompt_file": None,
               "message": "Continue the assigned work.", "owner_pid": 0, "owner_start": "",
               "tried": [OLD]}
    w.r.tree.update(node.id, handover_attempt=attempt)

    asyncio.run(w.r._qh_reconcile())

    [launch] = launched
    assert launch["provider"].name == NEW
    assert launch["spec"].provider == NEW
    after = w.r.tree.get(node.id)
    assert after.provider == NEW
    assert after.handover_attempt["state"] == "launched"
    assert [s["provider"] for s in after.segments] == ["beta", NEW]


# (c) a refusal recorded under the old name ----------------------------------

def _eligible(w, node):
    w.r.runs[node.id] = SimpleNamespace(active_tools=set(), awaiting=False, adopted=False)
    w.r._locks[node.id] = object()


def _promote_check(w, refusals):
    stamp = w.clock[0] - 30
    w.readings[NEW] = replace(w.readings[NEW], read_at=stamp)
    node = _node(w, "beta", promotion_refusals={k: stamp + v for k, v in refusals.items()})
    _eligible(w, node)
    started = []

    async def promote(node_id, promotion, settings):
        started.append(promotion)

    w.mp.setattr(w.r, "_qh_promote", promote)
    try:
        asyncio.run(w.r._qh_promote_check())
    finally:
        w.r.runs.pop(node.id, None)
        w.r._locks.pop(node.id, None)
    return [p["to"] for p in started]


@pytest.mark.parametrize("refusals, promoted", [
    ({}, [NEW]),                             # control: the fixture promotes
    ({OLD: -1}, [NEW]),                      # a refusal at an older reading
    ({OLD: 0}, []),                          # the same reading, written before the upgrade
    ({NEW: 0}, []),
])
def test_og_r6c_a_pre_upgrade_refusal_blocks_promotion_at_the_same_reading(
        tmp_path, monkeypatch, refusals, promoted):
    w = _world(tmp_path, monkeypatch)
    assert _promote_check(w, refusals) == promoted


def test_og_r6c_a_refusal_is_recorded_once_under_the_route(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    node = _node(w, "beta", promotion_refusals={OLD: w.clock[0] - 600, "other": 1.0})
    promotion = _promotion(w, "beta", OLD)
    w.r.tree.update(node.id, promotion=promotion)
    assert w.r._qh_fail_promotion(node.id, promotion, "target launch refused")
    assert w.r.tree.get(node.id).promotion_refusals == {NEW: promotion["read_at"], "other": 1.0}
    # The same refusal reported again is not a second event.
    assert w.r._qh_fail_promotion(node.id, dict(promotion, to=NEW), "target launch refused")
    assert len(w.events("promote_failed")) == 1


# A veto's quota fingerprint recorded under the old name ---------------------

def test_og_r6_a_pre_upgrade_veto_clears_on_its_routes_reset(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reserved="reserve")
    _rename(w)
    with w.r.tree.transaction() as data:
        data.setdefault("quota_reserve", []).append(
            {"id": "reserve-placeholder", "agent": "worker", "task": "placeholder task",
             "node_id": "", "kwargs": {}, "state": "vetoed", "queued_at": w.clock[0] - 600,
             "deferred_by": "", "readings": {OLD: [0.1, {}]}})
    started = []

    async def start(agent, task, **kwargs):
        started.append(agent)
        return {"agent_id": "ag-0ld6b2"}

    monkeypatch.setattr(w.r, "start", start)
    asyncio.run(w.r._qh_drain_reserve())   # the route's headroom went from 0.1 to 0.8
    assert started == ["worker"]
    [request] = w.r.tree.read()["quota_reserve"]
    assert request["state"] == "finished"
