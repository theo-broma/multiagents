"""C23 review round 3 (ag-1a0fee): steer busy ownership, refresh after a blind
cooldown, provider clocks after an unstamped stop, rollback before a stop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

from multiagents import config, quota_handover as qh, server
from multiagents.runner import Runner
from test_c23_quota_handover import wait_until, world  # noqa: F401
from test_c23_review3_checks import _DRIVER
from test_c23_v3_priorities import demoted, instances, promos, reading, release, tick


def test_qh_r27_refused_steer_keeps_the_handover_reservation(world):
    w = world()
    async def scenario():
        tool = w.tmp / 'tool-beta'
        tool.touch()                    # beta streams its session, then holds
        aid = await demoted(w, tool_gate=str(tool))
        await wait_until(lambda: w.r.tree.get(aid).handover_attempt['state'] == 'completed')
        busy = w.r.__dict__.setdefault('_qh_busy', set())
        original = w.r._qh_budgets
        async def budgets(spec=None):
            # A reconcile recovery claims the run while steer validates.
            busy.add(aid)
            return await original(spec)
        w.r._qh_budgets = budgets
        try:
            result = await server.steer_agent(aid, 'move to other', provider='other')
            assert 'quota handover' in result.get('error', ''), result
            assert aid in busy, 'steer released a reservation it never acquired'
            assert instances(w) == ['alpha', 'beta']
        finally:
            del w.r._qh_budgets
            busy.discard(aid)
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def wedged(w, read_at):
    w.readings['alpha'] = reading('alpha', .8, read_at=read_at)
    w.readings['alpha'].stale_seconds = 0
    w.set_plan('alpha', quota=False)


def test_qh_r30_cooldown_expiry_is_not_a_refresh(world):
    w = world(agent_extra={'priorities': [{'instance': 'alpha'}, {'instance': 'beta'}]})
    stop_stamp = w.clock[0] - 10
    w.readings['alpha'] = reading('alpha', .8, read_at=stop_stamp)
    async def scenario():
        aid = await demoted(w)
        try:
            # The script keeps returning the reading it took before the stop.
            wedged(w, stop_stamp)
            await tick(w, 4 * 3600)
            await tick(w, 60)
            assert instances(w) == ['alpha', 'beta']
            assert not promos(w, 'promote_started')
            wedged(w, stop_stamp + 1)
            await tick(w)
            await wait_until(lambda: instances(w)[-1] == 'alpha')
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r30_provider_clock_behind_an_unstamped_stop_still_refreshes(world):
    w = world(agent_extra={'priorities': [{'instance': 'alpha'}, {'instance': 'beta'}]})
    async def scenario():
        aid = await demoted(w)          # the stop reading carries no read_at
        try:
            behind = w.clock[0] - 3600
            wedged(w, behind)
            await tick(w, 301)
            assert instances(w) == ['alpha', 'beta']
            wedged(w, behind + 5)
            await tick(w, 1)
            await wait_until(lambda: instances(w)[-1] == 'alpha')
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


_STOP_RESTORE = '''    if phase=='stoprestore':
        reached, proceed=asyncio.Event(), asyncio.Event()
        original=run._steer_predecessor_dead
        async def paused(predecessor):
            if not reached.is_set():
                reached.set(); await proceed.wait()
            return await original(predecessor)
        mp.setattr(run,'_steer_predecessor_dead',paused)
        recovering=asyncio.create_task(run.resume_deferred())
        try:
            await asyncio.wait_for(reached.wait(),8)
            assert run.tree.get(aid).provider=='alpha', run.tree.get(aid)
            await run.stop(aid)
        finally:
            proceed.set();await recovering
        assert run.tree.get(aid).status=='cancelled'
        return
    if phase=='stop':
'''


def test_qh_r26_stop_during_recovery_probe_restores_source_first(world):
    w = world(mode='copy')
    ready = w.tmp / 'tool-beta'
    ready.touch()
    w.set_plan('beta', gate=str(w.tmp / 'hold-beta'), tool_gate=str(ready))
    driver, aidfile, gate = (w.tmp / n for n in ('restart.py', 'aid', 'crash'))
    driver.write_text(_DRIVER.replace("    if phase=='stop':\n", _STOP_RESTORE))
    env = {**os.environ, 'PYTHONPATH': str(Path(qh.__file__).resolve().parents[1])}
    children = []
    try:
        for phase in ('start', 'stoprestore'):
            with (w.tmp / (phase + '.log')).open('w') as log:
                child = subprocess.Popen([sys.executable, str(driver), str(w.paths.root),
                    str(aidfile), str(gate), phase, 'launching'], env=env, stdout=log, stderr=log)
                children.append(child)
                child.wait(timeout=15)
                assert child.returncode == 0, (w.tmp / (phase + '.log')).read_text()
        node = Runner(w.paths, config.load(w.paths, seed=False)).tree.get(aidfile.read_text())
        assert node.status == 'cancelled'
        assert node.provider == 'beta' and node.model == 'model-a'
        assert len(node.segments) == 2 and node.segments[-1]['provider'] == 'beta'
        assert node.handover_attempt['state'] == 'failed'
        assert instances(w) == ['alpha', 'beta'], 'an explicit stop must win: no relaunch'
    finally:
        release(w)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


class _Claimed(set):
    """Every run is held by another owner (a merge in progress)."""
    def __contains__(self, item): return True
    def discard(self, item): raise AssertionError('released a claim it never acquired')


def test_qh_r10_quota_stop_does_not_hand_over_a_run_another_owner_holds(world):
    w = world()
    w.r.__dict__['_qh_merging'] = _Claimed()
    async def scenario():
        result = await w.start()
        try:
            assert instances(w) == ['alpha'], 'handover ran while a merge held the branch'
            assert not w.events('handover_started')
            assert result['agent_id'] not in w.r.__dict__.get('_qh_busy', set())
        finally:
            release(w)
    asyncio.run(scenario())
