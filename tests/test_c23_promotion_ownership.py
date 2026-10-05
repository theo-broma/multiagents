"""Promotion checks must not adopt or mutate runs they cannot supervise."""
from __future__ import annotations

import asyncio

import pytest

from multiagents.tree import Node
from test_c23_quota_handover import world  # noqa: F401
from test_c23_v3_priorities import promos


@pytest.mark.parametrize('agent,supervised,claimed', [
    ('worker', False, False),
    ('worker', True, False),
    ('missing-agent', True, True),
])
def test_qh_r23_promotion_check_requires_ownership_and_configured_agent(
        world, monkeypatch, agent, supervised, claimed):
    w = world()
    node = Node(id='external-run', agent=agent, provider='beta', model='model-a',
                parent=None, depth=1)
    w.r.tree.add(node)
    w.r.tree.set_status(node.id, 'running')
    if supervised:
        monkeypatch.setitem(w.r.runs, node.id, object())
    if claimed:
        monkeypatch.setitem(w.r._locks, node.id, object())

    before = w.r.tree.read()
    asyncio.run(w.r._qh_promote_check())
    assert w.r.tree.read() == before
    assert not promos(w)


def test_qh_r27_promotion_rechecks_ownership_after_budget_refresh(world, monkeypatch):
    w = world()
    node = Node(id='released-run', agent='worker', provider='beta', model='model-a',
                parent=None, depth=1)
    w.r.tree.add(node)
    w.r.tree.set_status(node.id, 'running')
    monkeypatch.setitem(w.r.runs, node.id, object())
    monkeypatch.setitem(w.r._locks, node.id, object())

    async def released_during_refresh():
        w.r._locks.pop(node.id)
        return w.readings

    monkeypatch.setattr(w.r, '_qh_budgets', released_during_refresh)
    before = w.r.tree.read()
    asyncio.run(w.r._qh_promote_check())
    assert w.r.tree.read() == before


@pytest.mark.parametrize('marker', ['_steering_nodes', '_qh_merging'])
def test_qh_r26_dead_owner_promotion_is_recovered_despite_marker(world, monkeypatch, marker):
    w = world()
    promotion = {'owner_pid': 0, 'owner_start': '', 'phase': 'waiting',
                 'from': 'beta', 'to': 'alpha', 'from_rank': 1, 'to_rank': 0}
    node = Node(id='recovering-run', agent='worker', provider='beta', model='model-a',
                parent=None, depth=1, promotion=promotion)
    w.r.tree.add(node)
    w.r.tree.set_status(node.id, 'running')
    monkeypatch.setitem(w.r.runs, node.id, object())
    monkeypatch.setitem(w.r._locks, node.id, object())
    monkeypatch.setitem(w.r.__dict__, marker, {node.id})
    recovered = []

    async def recover(node_id, attempt, settings):
        recovered.append((node_id, attempt))

    monkeypatch.setattr(w.r, '_qh_promote', recover)

    async def scenario():
        await w.r._qh_promote_check()
        await asyncio.sleep(0)

    before = w.r.tree.read()
    asyncio.run(scenario())
    assert recovered == [(node.id, promotion)]
    assert w.r.tree.read() == before
    assert not promos(w)


@pytest.mark.parametrize('boundary', ['budget', 'usable', 'unusable'])
@pytest.mark.parametrize('change', ['steering', 'merging', 'agent_removed', 'ownership',
                                    'pinned', 'awaiting_user', 'busy', 'handover'])
def test_qh_r27_eligibility_changes_during_await_prevent_new_promotion(
        world, monkeypatch, boundary, change):
    w = world(options={'promote_min_dwell_seconds': 0},
              agent_extra={'priorities': ['alpha', 'reserve', 'beta']})
    node = Node(id='changing-run', agent='worker', provider='beta', model='model-a',
                parent=None, depth=1)
    w.r.tree.add(node)
    w.r.tree.set_status(node.id, 'running')
    monkeypatch.setitem(w.r.runs, node.id, object())
    monkeypatch.setitem(w.r._locks, node.id, object())
    started = []

    def change_eligibility():
        if change in ('steering', 'merging', 'busy'):
            marker = {'steering': '_steering_nodes', 'merging': '_qh_merging',
                      'busy': '_qh_busy'}[change]
            monkeypatch.setitem(w.r.__dict__, marker, {node.id})
        elif change == 'agent_removed':
            monkeypatch.delitem(w.r.config.agents, node.agent)
        elif change == 'ownership':
            monkeypatch.delitem(w.r._locks, node.id)
        elif change == 'pinned':
            w.r.tree.update(node.id, pinned=True)
        elif change == 'awaiting_user':
            w.r.tree.set_status(node.id, 'awaiting_user')
        elif change == 'handover':
            w.r.tree.update(node.id, handover_attempt={'state': 'prepared'})

    async def budgets():
        if boundary == 'budget':
            change_eligibility()
        return w.readings

    async def usable(*args, **kwargs):
        if boundary in ('usable', 'unusable'):
            change_eligibility()
        return boundary != 'unusable'

    async def promote(*args):
        started.append(args)

    monkeypatch.setattr(w.r, '_qh_budgets', budgets)
    monkeypatch.setattr(w.r, '_qh_usable', usable)
    monkeypatch.setattr(w.r, '_qh_promote', promote)

    async def scenario():
        await w.r._qh_promote_check()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not started
    current = w.r.tree.get(node.id)
    assert current.promotion is None
    assert current.provider == 'beta'
    assert current.status == ('awaiting_user' if change == 'awaiting_user' else 'running')
    assert not promos(w)
