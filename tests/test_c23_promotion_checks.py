"""Additional promotion invariants, with observable sessions before steering."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from multiagents import quota_handover as qh, server
from multiagents.providers import Event
from test_c23_quota_handover import TASK, wait_until, world  # noqa: F401
from test_c23_v3_priorities import demoted, regain, tick, instances, promos, release


def test_qh_r27_observed_session_pin_and_immediate_auto(world):
    w = world(options={'promote_min_dwell_seconds': 100000})
    w.set_plan('alpha', quota=False, tool_gate=str(w.tmp / 'tool-alpha'))
    w.set_plan('beta', tool_gate=str(w.tmp / 'tool-beta'))

    async def scenario():
        aid = (await w.r.start('worker', TASK))['agent_id']
        try:
            await wait_until(lambda: bool(w.r.tree.get(aid).session_id))
            assert (await server.steer_agent(aid, 'use beta', provider='beta'))['steered']
            await wait_until(lambda: w.r.tree.get(aid).handover_attempt['state'] == 'completed')
            assert server.check_agent(aid)['pinned']
            regain(w)
            await tick(w, 300)
            assert instances(w) == ['alpha', 'beta'] and not promos(w)
            result = await server.steer_agent(aid, 'use priorities', provider='auto')
            assert result['steered'], result
            await wait_until(lambda: instances(w)[-1] == 'alpha')
            assert not server.check_agent(aid)['pinned']
        finally:
            release(w)
            (w.tmp / 'tool-alpha').touch()
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r26_stop_wins_during_promotion_transfer(world, monkeypatch):
    w = world(mode='copy')

    async def scenario():
        aid = await demoted(w)
        reached, proceed = asyncio.Event(), asyncio.Event()
        async def pause(attempt):
            if attempt.get('promotion'):
                reached.set()
                await proceed.wait()
        monkeypatch.setattr(qh, 'before_transfer', pause)
        try:
            regain(w)
            await tick(w, 300)
            await asyncio.wait_for(reached.wait(), 8)
            await w.r.stop(aid)
            proceed.set()
            await asyncio.sleep(.1)
            assert instances(w) == ['alpha', 'beta']
            assert w.r.check(aid)['status'] == 'cancelled'
            assert not promos(w, 'promote_completed')
        finally:
            proceed.set()
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r26_parallel_tool_results_preserve_the_remaining_call():
    run = SimpleNamespace(active_tools=set())
    policy = qh.QuotaHandover()
    for identity in ('first', 'second'):
        policy._qh_observe(run, Event('tool', raw={'tool_id': identity}, state='running'))
    policy._qh_observe(run, Event('step', raw={'tool_id': 'first'}, state='completed'))
    assert run.active_tools == {'second'}
    policy._qh_observe(run, Event('step', raw={'tool_id': 'second'}, state='completed'))
    assert not run.active_tools


def test_qh_r26_claude_nested_results_preserve_the_remaining_call():
    run = SimpleNamespace(active_tools=set())
    policy = qh.QuotaHandover()
    policy._qh_observe(run, Event('tool', raw={'message': {'content': [
        {'type': 'tool_use', 'id': 'a'}, {'type': 'tool_use', 'id': 'b'}]}}))
    policy._qh_observe(run, Event('step', raw={'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'a'}]}}))
    assert run.active_tools == {'b'}
    policy._qh_observe(run, Event('step', raw={'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': 'b'}]}}))
    assert not run.active_tools


def test_qh_r27_merge_in_progress_excludes_promotion(world, monkeypatch):
    import threading
    w = world()
    entered, finish = threading.Event(), threading.Event()
    original = w.r.authoritative
    def authorize(node, operation):
        if operation == 'merge_agent':
            entered.set()
            finish.wait(8)
            return None
        return original(node, operation)
    monkeypatch.setattr(w.r, 'authoritative', authorize)

    async def scenario():
        aid = await demoted(w, tool_gate=str(w.tmp / 'tool-beta'))
        merging = asyncio.create_task(asyncio.to_thread(w.r.merge_agent, aid))
        try:
            await wait_until(entered.is_set)
            regain(w)
            await asyncio.wait_for(tick(w, 300), 2)
            assert not promos(w)
            finish.set()
            await merging
            await tick(w)
            (w.tmp / 'tool-beta').touch()
            await wait_until(lambda: instances(w)[-1] == 'alpha')
        finally:
            finish.set()
            await merging
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r30_bare_family_expands_to_its_instances(world):
    w = world(agent_extra={'priorities': ['fixture']})
    w.unusable('alpha', 'reserve')
    w.set_plan('beta', quota=False)
    result = asyncio.run(w.start())
    assert not result.get('deferred')
    assert instances(w) == ['beta']


def test_qh_r30_bare_instance_keeps_only_that_instance(world):
    w = world(agent_extra={'priorities': ['alpha']})
    w.unusable('alpha')
    result = asyncio.run(w.start())
    assert result['deferred']
    assert not w.calls()


def test_qh_r26_plain_message_event_does_not_break_tool_tracking():
    run = SimpleNamespace(active_tools={'tool'})
    qh.QuotaHandover()._qh_observe(run, Event('raw', raw={'message': 'waiting'}))
    assert run.active_tools == {'tool'}
