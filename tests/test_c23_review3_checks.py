"""C23 review: script clocks and durable promotion rollback on restart."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from multiagents import config, quota_handover as qh
from multiagents.runner import Runner
from test_c23_quota_handover import wait_until, world  # noqa: F401
from test_c23_v3_priorities import demoted, instances, promos, reading, release, tick


def test_qh_r30_refresh_compares_provider_stamps_in_their_own_clock(world):
    w = world(agent_extra={"priorities": [{"instance": "alpha"}, {"instance": "beta"}]})
    # Scripts can report a remote read_at alongside their own stale_seconds.
    remote_stop = w.clock[0] - 3600
    w.readings['alpha'] = reading('alpha', .8, read_at=remote_stop)
    w.readings['alpha'].stale_seconds = 0
    async def scenario():
        aid = await demoted(w)
        try:
            w.readings['alpha'] = reading('alpha', .8, read_at=remote_stop + 301)
            w.readings['alpha'].stale_seconds = 0
            w.set_plan('alpha', quota=False)
            await tick(w, 301)
            await wait_until(lambda: instances(w)[-1] == 'alpha')
            assert instances(w) == ['alpha', 'beta', 'alpha']
        finally:
            release(w)
            await w.r.stop(aid)
    asyncio.run(scenario())


_DRIVER = '''import asyncio, json, os, sys
from pathlib import Path
from pytest import MonkeyPatch
from multiagents import budget, quota_handover as qh, runner, tree
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
root, aidfile, gate = map(Path,sys.argv[1:4])
phase, seam = sys.argv[4:6]
clock=[1900000000.0 if phase=='start' else 1900000301.0]
readings={n:budget.Budget(n,known=True,headroom=.8,read_at=clock[0]) for n in ('alpha','beta','other','reserve')}
original_launch=Runner._launch
async def launch(self, **kw):
    attempt=self.tree.get(kw['node_id']).handover_attempt or {}
    if attempt.get('promotion') and phase=='start' and seam=='launching':
        gate.write_text(json.dumps(attempt)); os._exit(0)
    return await original_launch(self,**kw)
async def transfer(attempt):
    if attempt.get('promotion') and phase=='start' and seam=='failed_recovery':
        gate.write_text(json.dumps(attempt)); os._exit(0)
    if attempt.get('promotion') and phase=='fail':
        raise OSError('injected recovery transfer failure')
    if phase=='fail' and attempt['to']=='beta':
        raise OSError('source resume temporarily unavailable')
async def main():
    run=Runner(ProjectPaths(root),load(ProjectPaths(root),seed=False))
    if phase=='start':
        aid=(await run.start('worker','Implement the assigned quota handover task'))['agent_id']
        aidfile.write_text(aid)
        for _ in range(800):
            node=run.tree.get(aid)
            if node.provider=='beta' and node.handover_attempt['state']=='completed':break
            await asyncio.sleep(.01)
        else:raise RuntimeError('never demoted')
        clock[0]+=301
        readings['alpha']=budget.Budget('alpha',known=True,headroom=.8,read_at=clock[0])
        await run.resume_deferred()
        await asyncio.sleep(10)
        raise RuntimeError('crash seam not reached')
    aid=aidfile.read_text()
    if phase=='stop':
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
            result=await asyncio.wait_for(run.steer(aid,'stay on the source',provider='beta'),1)
            assert 'quota handover' in result.get('error',''), result
            await run.stop(aid)
            proceed.set();await recovering
            assert run.tree.get(aid).status=='cancelled'
            return
        finally:
            proceed.set();await recovering
            await run.stop(aid)
    if phase=='fail':
        await run.resume_deferred()
        # Exit after a failed source launch: the next process must retry it.
        os._exit(0)
    await run.resume_deferred()
    for _ in range(800):
        node=run.tree.get(aid)
        if node.provider=='beta' and node.promotion is None and node.handover_attempt['state']=='completed':break
        await asyncio.sleep(.01)
    else:raise RuntimeError('source was not resumed: '+repr(run.tree.get(aid)))
    launches=len(list((root.parent/'calls').glob('*.json')))
    for _ in range(3):await run.resume_deferred()
    assert len(list((root.parent/'calls').glob('*.json')))==launches, 'rollback launched twice'
    assert node.rank==1 and len(node.segments)==2
    assert node.promotion_dwell_at>=clock[0]
    # Fresh target data cannot override dwell from the recovered attempt.
    clock[0]+=1
    readings['alpha']=budget.Budget('alpha',known=True,headroom=.8,read_at=clock[0])
    await run.resume_deferred(); await asyncio.sleep(.1)
    assert run.tree.get(aid).provider=='beta'
    await run.stop(aid)
with MonkeyPatch.context() as mp:
    mp.setattr(runner,'now',lambda:clock[0]);mp.setattr(tree,'now',lambda:clock[0])
    mp.setattr(budget,'read_all',lambda *a,**kw:dict(readings))
    mp.setattr(budget,'read_provider',lambda n,*a,**kw:budget.Budget(n,known=True,headroom=0 if n=='alpha' and phase=='start' else .8))
    mp.setattr(qh,'before_transfer',transfer);mp.setattr(Runner,'_launch',launch)
    asyncio.run(main())
'''


@pytest.mark.parametrize('seam,recovery', [('launching', 'restart'),
    ('failed_recovery', 'restart'), ('failed_recovery', 'stop')])
def test_qh_r26_restart_resumes_source_once_after_failed_promotion(world, seam, recovery):
    w = world(mode='copy')
    ready = w.tmp / 'tool-beta'
    ready.touch()
    w.set_plan('beta', gate=str(w.tmp / 'hold-beta'), tool_gate=str(ready))
    driver, aidfile, gate = (w.tmp / n for n in ('restart.py', 'aid', 'crash'))
    driver.write_text(_DRIVER)
    env = {**os.environ, 'PYTHONPATH': str(Path(qh.__file__).resolve().parents[1])}
    children = []
    try:
        phases = ['start', recovery] if seam == 'launching' else ['start', 'fail', recovery]
        for phase in phases:
            with (w.tmp / (phase + '.log')).open('w') as log:
                child = subprocess.Popen([sys.executable, str(driver), str(w.paths.root),
                    str(aidfile), str(gate), phase, seam], env=env, stdout=log, stderr=log)
                children.append(child)
                child.wait(timeout=15)
                assert child.returncode == 0, (w.tmp / (phase + '.log')).read_text()
        node = Runner(w.paths, config.load(w.paths, seed=False)).tree.get(aidfile.read_text())
        assert node.provider == 'beta' and node.promotion is None
        assert instances(w) == (['alpha', 'beta', 'beta'] if recovery == 'restart' else ['alpha', 'beta'])
        assert len(promos(w, 'promote_failed')) == 1
    finally:
        release(w)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
