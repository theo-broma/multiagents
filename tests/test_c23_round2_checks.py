"""C23 promotion ownership, failed-attempt dwell, and crash rollback checks."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from multiagents import config, quota_handover as qh, server
from multiagents.runner import Runner
import test_c23_quota_handover as base
from test_c23_quota_handover import wait_until, world  # noqa: F401
from test_c23_v3_priorities import demoted, instances, promos, reading, regain, release, tick


def refresh(w):
    w.clock[0] += 1
    regain(w)


def test_qh_r30_refresh_must_be_strictly_newer_than_unstamped_stop(world):
    w = world()
    async def scenario():
        aid = await demoted(w)
        try:
            stopped_at = w.clock[0]
            w.readings['alpha'] = reading('alpha', .8, read_at=stopped_at)
            w.set_plan('alpha', quota=False)
            await tick(w, 300)
            assert instances(w) == ['alpha', 'beta']
            assert not promos(w, 'promote_started')
            refresh(w)
            await tick(w)
            await wait_until(lambda: instances(w)[-1] == 'alpha')
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r27_explicit_pin_supersedes_safe_point_wait(world):
    w = world()
    tool = w.tmp / 'tool-beta'
    async def scenario():
        aid = await demoted(w, tool_gate=str(tool))
        try:
            refresh(w)
            await tick(w, 300)
            assert promos(w, 'promote_started')
            result = await server.steer_agent(aid, 'stay on beta', provider='beta')
            assert result.get('steered'), result
            tool.touch()
            await wait_until(lambda: len(w.calls()) == 3)
            check = server.check_agent(aid)
            assert check['pinned'] and check['current_provider'] == 'beta'
            assert not w.r.tree.get(aid).promotion
            assert not promos(w, 'promote_completed')
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r26_source_quota_stop_supersedes_safe_point_wait(world, monkeypatch):
    w = world(options={"promote_min_dwell_seconds": 0}, agent_extra={'priorities': [{'instance': 'alpha'}, {'instance': 'beta'}]})
    # A provider can quota-stop before supplying a result for an active tool.
    script = w.tmp / 'fixture.py'
    script.write_text(base.CLI.replace(
        "    print(json.dumps({'type':'tool_result','session_id':sid}),flush=True)",
        "    if not plan.get('quota'): print(json.dumps({'type':'tool_result','session_id':sid}),flush=True)"))
    tool = w.tmp / 'tool-beta'
    async def scenario():
        aid = await demoted(w, quota=True, tool_gate=str(tool))
        reached, proceed = asyncio.Event(), asyncio.Event()
        original = w.r._qh_budgets
        async def budgets(spec=None):
            if spec and spec.provider == 'beta' and w.r.tree.get(aid).quota_stops.get('beta'):
                reached.set()
                await proceed.wait()
            return await original(spec)
        monkeypatch.setattr(w.r, '_qh_budgets', budgets)
        try:
            refresh(w)
            await tick(w, 300)
            assert promos(w, 'promote_started')
            assert w.r.runs[aid].active_tools
            tool.touch()
            (w.tmp / 'hold-beta').touch()
            await asyncio.wait_for(reached.wait(), 8)
            await tick(w)
            assert len(promos(w, 'promote_started')) == 1
            proceed.set()
            await wait_until(lambda: instances(w)[-1] == 'alpha')
            await wait_until(lambda: w.r.tree.get(aid).handover_attempt['state'] == 'completed')
            assert instances(w) == ['alpha', 'beta', 'alpha']
            assert w.r.tree.get(aid).promotion is None
            assert not promos(w, 'promote_completed')
            assert promos(w, 'promote_failed')
        finally:
            proceed.set()
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


def test_qh_r25_failed_promotion_refresh_does_not_bypass_dwell(world):
    w = world()
    ready = w.tmp / 'tool-beta'
    ready.touch()
    async def scenario():
        aid = await demoted(w, tool_gate=str(ready))
        try:
            refresh(w)
            w.set_plan('alpha', reject=True)
            await tick(w, 300)
            await wait_until(lambda: len(w.calls()) == 4)
            await wait_until(lambda: w.r.tree.get(aid).handover_attempt['state'] == 'completed')
            assert instances(w) == ['alpha', 'beta', 'alpha', 'beta']
            assert len(w.r.tree.get(aid).segments) == 2
            refresh(w)
            w.set_plan('alpha', reject=False)
            await tick(w, 298)
            assert len(promos(w, 'promote_started')) == 1
            assert instances(w) == ['alpha', 'beta', 'alpha', 'beta']
            await tick(w, 1)
            await wait_until(lambda: len(w.calls()) == 5)
            assert instances(w)[-1] == 'alpha'
            assert len(promos(w, 'promote_started')) == 2
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


_RESTART = '''import asyncio, json, os, sys
from pathlib import Path
from pytest import MonkeyPatch
from multiagents import budget, quota_handover as qh, runner, tree
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
root, aidfile, mode, gate = map(Path, sys.argv[1:])
clock=[1900000000.0 if str(mode)=='start' else 1900000301.0]
readings={n:budget.Budget(n,known=True,headroom=.8,read_at=clock[0]) for n in ('alpha','beta','other','reserve')}
async def transfer(attempt):
    if attempt.get('promotion'):
        if str(mode)=='start':
            gate.write_text(json.dumps(attempt))
            os._exit(0)
        raise OSError('injected recovery transfer failure')
async def main():
    run=Runner(ProjectPaths(root),load(ProjectPaths(root),seed=False))
    if str(mode)=='start':
        aid=(await run.start('worker','Implement the assigned quota handover task'))['agent_id']
        aidfile.write_text(aid)
        for _ in range(800):
            if run.tree.get(aid).provider=='beta' and run.tree.get(aid).handover_attempt['state']=='completed': break
            await asyncio.sleep(.01)
        else: raise RuntimeError('source never demoted')
        clock[0]+=301
        readings['alpha']=budget.Budget('alpha',known=True,headroom=.8,read_at=clock[0])
        await run.resume_deferred()
        await asyncio.sleep(10)
        raise RuntimeError('promotion crash point not reached')
    else:
        aid=aidfile.read_text()
        await run.resume_deferred()
        for _ in range(800):
            node=run.tree.get(aid)
            if node.promotion is None and node.provider=='beta' and node.handover_attempt['state']=='completed':
                await run.stop(aid)
                return
            await asyncio.sleep(.01)
        raise RuntimeError('recovered promotion remained pending')
with MonkeyPatch.context() as mp:
    mp.setattr(runner,'now',lambda:clock[0]); mp.setattr(tree,'now',lambda:clock[0])
    mp.setattr(budget,'read_all',lambda *a,**kw:dict(readings))
    mp.setattr(budget,'read_provider',lambda n,*a,**kw:budget.Budget(n,known=True,headroom=0 if n=='alpha' and str(mode)=='start' else .8))
    mp.setattr(qh,'before_transfer',transfer)
    asyncio.run(main())
'''


def test_qh_r26_crash_recovery_rollback_clears_pending_promotion(world):
    w = world(mode='copy')
    ready = w.tmp / 'tool-beta'
    ready.touch()
    w.set_plan('beta', gate=str(w.tmp / 'hold-beta'), tool_gate=str(ready))
    driver, aidfile, gate = (w.tmp / n for n in ('restart.py', 'aid', 'promotion-crash'))
    driver.write_text(_RESTART)
    env = {**os.environ, 'PYTHONPATH': str(Path(qh.__file__).resolve().parents[1])}
    argv = [sys.executable, str(driver), str(w.paths.root), str(aidfile)]
    children = []
    try:
        for mode in ('start', 'restart'):
            with (w.tmp / (mode + '.log')).open('w') as log:
                child = subprocess.Popen([*argv, mode, str(gate)], env=env, stdout=log, stderr=log)
                children.append(child)
                child.wait(timeout=15)
                assert child.returncode == 0, (w.tmp / (mode + '.log')).read_text()
            if mode == 'start':
                attempt = json.loads(gate.read_text())
                assert attempt['state'] == 'prepared' and attempt['promotion']
        node = Runner(w.paths, config.load(w.paths, seed=False)).tree.get(aidfile.read_text())
        assert node.promotion is None
        assert node.provider == 'beta'
        assert instances(w) == ['alpha', 'beta', 'beta']
        assert len(promos(w, 'promote_failed')) == 1
    finally:
        release(w)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
