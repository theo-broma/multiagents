"""Black-box contract for C23, context/specs/c23-quota-handover.md.

Clock seam: existing runner.now/tree.now and budget.read_all/read_provider are
injected. Reserve deadlines must use that clock and be reconsidered by public
resume_deferred/wait_for_any calls; no real 120-second wait is necessary.
Session files use the existing provider transcript.dir/transcript.glob surface.
The copy adapter must transfer only the declared conversation. No transfer
function or private implementation shape is prescribed.
Config-load rejection uses the existing loader convention, ValueError; the
C23 contract does not specify an exception type.
No production stub, private method assertion, or real provider is used.
Verified by QH-R1: enabled/off/default-on quota scenarios.
Verified by QH-R2: reserve config boundaries, unknown name, unset reservation.
Verified by QH-R3: shipped modes and none/undeclared/shared/copy scenarios.
Verified by QH-R4: agent opt-out scenario.
Verified by QH-R5: tiers, model eligibility, disabled/unknown quota, list order.
Verified by QH-R6: new-run reserved routing and floor boundaries.
Verified by QH-R7: explicit MCP steer, same-instance steer, named refusals.
Verified by QH-R8: transcript resume, isolated/idempotent/divergent/atomic copy,
    rejected resume. Mid-transfer I/O failure still needs an injection seam.
Verified by QH-R9: continuation prompt, commits, dirty work, stable agent id.
Verified by QH-R10: durable pre-launch attempts, no ping-pong, and process
    restart before target resume verification. Before-transfer crash injection
    still needs a deterministic seam.
Verified by QH-R11: reset boundary, live turn, failure, cross-family home state.
Verified by QH-R12: reserved run leaves for a freed sibling.
Verified by QH-R13: reserved routing above/below the floor.
Verified by QH-R14: idle eligibility and an actual running competing agent.
Verified by QH-R15: FIFO candidates and a gated live floor run.
Verified by QH-R16: wakeup, allow, veto, pre-deadline/deadline dispatch.
Verified by QH-R17: floor quota stop and boundary return after reset.
Verified by QH-R18: event schemas checked whenever scenario events are read.
Verified by QH-R19: check/collect/tree provider and segment output.
Verified by QH-R20: provider spend attribution and floor budget output;
    the local fixture has no vault, so known vault-account coverage is pending.
Verified by QH-R21: subagent launch scenario; scheduler launcher pending.
Verified by QH-R22: off-switch/no-candidate scenarios and existing suite.
QH-R21: subagent start coverage; scheduler exclusion needs the M2 launcher.
The missing before-transfer crash/mid-copy I/O-failure seam is recorded in the report.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import c3_harness as h
from multiagents import budget, config, runner as runner_mod, server, tree
from multiagents.budget import Budget
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner

MARKER = 'C23 fixture quota exhausted'
TASK = 'Implement the assigned quota handover task'

# Real subprocess. Shared stores keep transcripts across account changes;
# separate stores refuse resumes until the runner installs the conversation.
CLI = r'''import json, sys, uuid, subprocess, time
from pathlib import Path
args = sys.argv[1:]
def flag(k): return args[args.index(k)+1] if k in args else None
name, account = flag('--instance'), flag('--account')
store, state, probe = map(Path, [flag('--store'), flag('--state'), flag('--probe')])
store.mkdir(parents=True, exist_ok=True)
plan = json.loads(state.read_text())
sid = flag('--resume') or plan.get('session_id') or str(uuid.uuid4())
path = store / (sid + '.jsonl')
prompt = flag('--prompt') or sys.stdin.read()
old = path.read_text() if path.exists() else ''
record = dict(instance=name, account=account, session_id=sid, resume=flag('--resume'),
              cwd=str(Path.cwd()), model=flag('--model'), effort=flag('--effort'),
              branch=subprocess.run(['git','branch','--show-current'],capture_output=True,text=True,check=True).stdout.strip(),
              prompt=prompt, transcript_before=old,
              dirty=(Path.cwd()/'c23-dirty.txt').read_text() if (Path.cwd()/'c23-dirty.txt').exists() else None)
event_path=Path(flag('--events'))
record['events_before_launch']=[json.loads(x) for x in event_path.read_text().splitlines()] if event_path.exists() else []
probe.mkdir(parents=True,exist_ok=True)
record_path=probe/(str(time.time_ns())+'-'+uuid.uuid4().hex+'.json')
part=record_path.with_suffix('.part')
part.write_text(json.dumps(record))
part.replace(record_path)
if flag('--resume') and (not path.exists() or plan.get('reject')):
    print('Unknown session id', file=sys.stderr); sys.exit(1)
if plan.get('pre_append_gate'):
    end=time.monotonic()+12
    while not Path(plan['pre_append_gate']).exists() and time.monotonic()<end: time.sleep(.01)
with path.open('a') as f: f.write(json.dumps({'instance':name,'prompt':prompt})+'\n')
if plan.get('dirty'):
    Path('c23-dirty.txt').write_text('uncommitted from first segment')
if plan.get('commit'):
    Path('c23-commit.txt').write_text('committed first segment')
    subprocess.run(['git','add','c23-commit.txt'],check=True)
    subprocess.run(['git','commit','-m','C23 first segment commit'],check=True,capture_output=True)
if plan.get('gate'):
    end = time.monotonic()+12
    while not Path(plan['gate']).exists() and time.monotonic()<end: time.sleep(.01)
limited = plan.get('quota', False)
print(json.dumps({'type':'text','session_id':sid,
                  'text':'C23 fixture quota exhausted' if limited else 'C23 turn complete'}),flush=True)
print(json.dumps({'type':'usage','session_id':sid,'usage':{'input_tokens':10,'output_tokens':2,'cost_usd':0.1}}),flush=True)
sys.exit(1 if limited else 0)
'''


class World:
    def __init__(self, tmp_path, monkeypatch, *, enabled=True, reserved=None,
                 mode='shared', models=None, agent_extra=None, options=None):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.clock = [1900000000.0]
        monkeypatch.setattr(runner_mod, 'now', lambda: self.clock[0])
        monkeypatch.setattr(tree, 'now', lambda: self.clock[0])
        self.readings = {n: Budget(n, known=True, headroom=0.8,
            windows={'short':{'percent':20,'span_minutes':300,'resets_at':
                datetime.fromtimestamp(self.clock[0]+(i+1)*3600, timezone.utc).isoformat()}})
            for i,n in enumerate(('alpha','beta','reserve','other'))}
        monkeypatch.setattr(budget, 'read_all', lambda *a, **kw: dict(self.readings))
        # At launch alpha is usable. The fake CLI's quota marker represents
        # the account exhausting during the turn; the uncached corroborating
        # reading at finalization is exhausted.
        monkeypatch.setattr(budget, 'read_provider', lambda name, *a, **kw:
                            Budget(name, known=True, headroom=0.0) if self.plan(name).get('quota') else self.readings[name])
        self.states, self.stores = {}, {}
        self.session_id = str(uuid.uuid4())
        script = tmp_path/'fixture.py'
        script.write_text(CLI)
        providers = {}
        self.probe = tmp_path/'calls'
        shared = tmp_path/'shared'
        for name in self.readings:
            state = tmp_path/(name+'.json')
            state.write_text(json.dumps({'quota': name == 'alpha', **({'session_id':self.session_id} if name=='alpha' else {})}))
            self.states[name] = state
            store = shared if mode == 'shared' and name != 'other' else tmp_path/('store-'+name)
            store.mkdir(exist_ok=True)
            self.stores[name] = store
            providers[name] = {
                'bin':sys.executable, 'family':'different' if name == 'other' else 'fixture',
                'handover_mode':{'local':mode, 'docker':mode},
                'spawn':{'args':[str(script),'--instance',name,'--account','acct-'+name,
                                  '--store',str(store),'--state',str(state),'--probe',str(self.probe),
                                  '--events',str(tmp_path/'project'/'.multiagents'/'events.jsonl'),
                                  '--model','{model}','--prompt','{prompt}'],
                         'resume':['--resume','{session_id}'],
                         'optional':{'effort':['--effort','{effort}']}},
                'stream':{'format':'ndjson','session_id_paths':['session_id'], 'rules':[
                    {'match':{'type':'text'},'as':'text','fields':{'text':'text'}},
                    {'match':{'type':'usage'},'as':'step','fields':{'tokens':'usage'}}]},
                'transcript':{'dir':str(store),'glob':'*.jsonl',
                              'limit_markers':[{'match':MARKER,'detail':'fixture quota','resets':True}]}}
        h.as_root(monkeypatch)
        root = h.make_git_repo(tmp_path/'project')
        self.paths = ProjectPaths(root)
        self.paths.ensure()
        for p in providers.values():
            args=p['spawn']['args']
            args[args.index('--events')+1]=str(self.paths.events_file)
        qh = {'enabled':enabled, **(options or {})}
        if reserved is not None: qh['reserved_instance'] = reserved
        self.project = {'team':'','executor':{'kind':'local'},'quota_handover':qh,
                        'budget':{'reserve':False}, 'limits':{'retry_silent_failure_under_seconds':0,'commit_fix_attempts':0}}
        self.agent = {'provider':'alpha','model':'model-a','effort':'high',
                      'models': models if models is not None else {'beta':'model-a','reserve':'model-a','other':'model-o'},
                      **(agent_extra or {})}
        self.providers = providers
        self.save()
        self.r = Runner(self.paths, config.load(self.paths, seed=False))
        monkeypatch.setattr(server, 'runner', lambda: self.r)

    def save(self):
        for filename, value in [('project',self.project),('providers',{'providers':self.providers}),
                                ('agents',{'agents':{'worker':self.agent}})]:
            (self.paths.config/(filename+'.yaml')).write_text(yaml.safe_dump(value, sort_keys=False))

    def reload(self):
        self.save()
        self.r = Runner(self.paths, config.load(self.paths, seed=False))

    def plan(self, name): return json.loads(self.states[name].read_text())
    def set_plan(self,name,**kw): self.states[name].write_text(json.dumps({**self.plan(name),**kw}))
    def calls(self):
        return [json.loads(p.read_text()) for p in sorted(self.probe.glob('*.json'))]
    def events(self,kind=None):
        rows = [json.loads(x) for x in self.paths.events_file.read_text().splitlines()] if self.paths.events_file.exists() else []
        handover_kinds={'handover_started','handover_completed','handover_failed',
            'return_home','return_home_failed','reserve_request','reserve_allowed','reserve_vetoed'}
        for event in rows:
            if event.get('kind') in handover_kinds:
                assert {'kind','agent_id','session_id','from','to','tier','segment','attempt','reason','at'}<=event.keys(),event
        return [e for e in rows if kind is None or e.get('kind') == kind]
    def unusable(self,*names):
        for n in names: self.readings[n] = Budget(n,known=True,headroom=0)
    def reset(self,name):
        self.clock[0] += 3600
        self.readings[name] = Budget(name,known=True,headroom=0.8)
        self.set_plan(name,quota=False)

    async def settle(self,agent_id):
        # Poll the public tool result, never Run.done or a private finalizer.
        end = asyncio.get_running_loop().time()+12
        while asyncio.get_running_loop().time()<end:
            if self.r.check(agent_id)['status'] not in ('running','pending'):
                await asyncio.sleep(.05)
                if self.r.check(agent_id)['status'] not in ('running','pending'): return self.r.check(agent_id)
            await asyncio.sleep(.01)
        pytest.fail('fixture run did not finish within 12 seconds')

    async def start(self, **kw):
        result = await self.r.start('worker',TASK,**kw)
        if result.get('agent_id'): await self.settle(result['agent_id'])
        return result

    async def steer(self,agent_id,provider=None):
        if provider is not None:
            assert 'provider' in inspect.signature(server.steer_agent).parameters, 'QH-R7: MCP steer_agent lacks provider='
            result = await server.steer_agent(agent_id,'Continue assigned work',provider=provider)
        else: result = await self.r.steer(agent_id,'Continue assigned work')
        await self.settle(agent_id)
        return result


@pytest.fixture
def world(tmp_path,monkeypatch):
    return lambda **kw: World(tmp_path,monkeypatch,**kw)


def switched(w, target):
    calls = w.calls()
    assert [c['instance'] for c in calls][:2] == ['alpha',target], calls
    assert w.events('handover_started'), 'switch attempt event missing'
    assert w.events('handover_completed'), 'completed switch event missing'
    return calls[:2]


@pytest.mark.parametrize('enabled',[False,True])
def test_qh_r1_qh_r22_off_switch_equivalence(world,enabled):
    w = world(enabled=enabled)
    result=asyncio.run(w.start())
    assert [c['instance'] for c in w.calls()] == (['alpha','beta'] if enabled else ['alpha'])
    if not enabled:
        assert w.r.check(result['agent_id'])['status'] == 'limited'
        assert not w.events('handover_started')


@pytest.mark.parametrize('value',[0,1,-.1,1.1,float('nan'),float('inf')])
def test_qh_r2_reserve_fraction_rejects_boundary_and_nonfinite(world,value):
    w = world(options={'reserve_fraction':.5})
    w.project['quota_handover']['reserve_fraction'] = value
    w.save()
    with pytest.raises(ValueError): config.load(w.paths,seed=False)


def test_qh_r2_unknown_reserved_instance_refused_at_load(world):
    w = world()
    w.project['quota_handover']['reserved_instance'] = 'does-not-exist'
    w.save()
    with pytest.raises(ValueError,match='does-not-exist'): config.load(w.paths,seed=False)


def test_qh_r2_unset_reservation_does_not_protect_any_instance(world):
    w = world(models={'reserve':'model-a'})
    w.unusable('beta','other')
    asyncio.run(w.start())
    switched(w,'reserve')
    assert not w.events('reserve_request')


@pytest.mark.parametrize('mode',['none',None])
def test_qh_r3_undeclared_or_none_mode_does_not_resume_sibling(world,mode):
    w = world(mode='none',models={'beta':'model-a','other':'model-o'})
    if mode is None:
        for p in w.providers.values(): p.pop('handover_mode')
        w.reload()
    asyncio.run(w.start())
    assert all(not c['resume'] for c in w.calls()[1:]), w.calls()
    assert len(w.calls()) >= 2, 'tier 3 continuation missing'


def test_qh_r4_agent_opt_out_keeps_original_instance(world):
    w = world(agent_extra={'handover':False})
    asyncio.run(w.start())
    assert [c['instance'] for c in w.calls()] == ['alpha']
    assert not w.events('handover_started')


@pytest.mark.parametrize('target,blocked,reserved',[('beta',(), 'reserve'),('reserve',('beta','other'),'reserve'),('other',('beta','reserve'),None)])
def test_qh_r5_each_successor_tier_and_better_tier_wins(world,target,blocked,reserved):
    w = world(reserved=reserved)
    w.unusable(*blocked)
    asyncio.run(w.start())
    switched(w,target)


def test_qh_r5_sibling_must_serve_exact_model(world):
    w = world(models={'beta':'different-model','other':'model-o'})
    w.unusable('reserve')
    asyncio.run(w.start())
    calls=w.calls()
    assert len(calls)>=2, 'no continuation after rejecting the wrong-model sibling resume'
    assert all(not c['resume'] or c['model']==calls[0]['model'] for c in calls[1:]),calls


@pytest.mark.parametrize('headroom',[.249,.25,.251])
def test_qh_r6_qh_r13_new_run_reserve_floor_boundary(world,headroom):
    w = world(reserved='reserve',models={'reserve':'model-a'})
    w.unusable('alpha','beta','other')
    w.readings['reserve'] = Budget('reserve',known=True,headroom=headroom)
    asyncio.run(w.start())
    assert bool(w.calls()) == (headroom>.25), w.calls()
    if headroom<=.25: assert w.events('reserve_request'), 'floor needs orchestrator veto window'


def test_qh_r6_qh_r13_new_run_skips_reserved_while_any_alternative_usable(world):
    w = world(reserved='reserve')
    w.set_plan('alpha',quota=False)
    # Without reservation the existing soonest-reset strategy would prefer
    # reserve, so this scenario actually distinguishes the new protection.
    w.readings['reserve']=Budget('reserve',known=True,headroom=.8,windows={
        'short':{'percent':20,'span_minutes':300,'resets_at':
            datetime.fromtimestamp(w.clock[0]+1000,timezone.utc).isoformat()}})
    asyncio.run(w.start())
    assert w.calls()[0]['instance'] != 'reserve'


@pytest.mark.parametrize('target',['beta','other','alpha'])
def test_qh_r7_explicit_steer_preserves_or_continues_as_specified(world,target):
    w = world()
    w.set_plan('alpha',quota=False)
    async def scenario():
        start = await w.start()
        await w.steer(start['agent_id'],target)
    asyncio.run(scenario())
    a,b = w.calls()
    assert b['instance']==target
    assert bool(b['resume']) == (target!='other')
    assert (a['session_id']==b['session_id']) == (target!='other')


@pytest.mark.parametrize('target,condition',[('foreign','list'),('beta','usable'),('reserve','floor')])
def test_qh_r7_explicit_refusal_names_failed_condition(world,target,condition):
    w = world(reserved='reserve')
    if target=='foreign':
        w.providers['foreign']={**w.providers['other'],'family':'unlisted-family'}
        w.reload()
    w.set_plan('alpha',quota=False)
    async def scenario():
        start = await w.start()
        if target=='beta': w.unusable('beta')
        if target=='reserve': w.readings['reserve']=Budget('reserve',known=True,headroom=.2)
        assert 'provider' in inspect.signature(server.steer_agent).parameters, 'QH-R7 missing provider parameter'
        result = await server.steer_agent(start['agent_id'],'continue',provider=target)
        assert result.get('error'), result
        alternatives={'list':('list','allowed','roster','configured','permit'), 'usable':('quota','headroom','usable','cooldown','budget','exhaust'),
                      'floor':('floor','reserve')}
        assert any(x in json.dumps(result).lower() for x in alternatives[condition]),result
    asyncio.run(scenario())
    assert len(w.calls())==1


@pytest.mark.parametrize('mode',['shared','copy'])
def test_qh_r8_session_resume_carries_transcript_cwd_model_effort(world,mode):
    w = world(mode=mode)
    asyncio.run(w.start())
    a,b = switched(w,'beta')
    assert b['resume']==a['session_id']==b['session_id']
    assert TASK in b['transcript_before']
    assert a['cwd']==b['cwd'] and a['branch']==b['branch'] and a['model']==b['model'] and a['effort']==b['effort']
    original=json.dumps({'instance':'alpha','prompt':a['prompt']})+'\n'
    source=(w.stores['alpha']/(a['session_id']+'.jsonl')).read_text()
    assert source.startswith(original)
    if mode=='copy': assert source==original, 'copy transfer changed source state'


def test_qh_r8_copy_does_not_copy_credentials_or_other_sessions(world):
    w=world(mode='copy')
    for filename in ('credentials.json','unrelated.jsonl'):
        (w.stores['alpha']/filename).write_text('source-secret')
        (w.stores['beta']/filename).write_text('target-original')
    asyncio.run(w.start())
    switched(w,'beta')
    for filename in ('credentials.json','unrelated.jsonl'):
        assert (w.stores['beta']/filename).read_text()=='target-original'
        assert (w.stores['alpha']/filename).read_text()=='source-secret'
    assert all('source-secret' not in p.read_text() for p in w.stores['beta'].rglob('*') if p.is_file())


def test_qh_r8_rejected_resume_tries_next_tier_without_fresh_old_id(world):
    w=world(models={'beta':'model-a','other':'model-o'})
    w.set_plan('beta',reject=True)
    asyncio.run(w.start())
    calls=w.calls()
    assert [c['instance'] for c in calls]==['alpha','beta','other'],calls
    assert calls[1]['resume']==calls[0]['session_id']
    assert not calls[2]['resume'] and calls[2]['session_id']!=calls[0]['session_id']
    assert w.events('handover_failed')


def test_qh_r9_cross_family_continuation_preserves_dirty_work_and_commit_context(world):
    w=world(models={'other':'model-o'})
    w.unusable('beta','reserve')
    w.set_plan('alpha',dirty=True,commit=True)
    result=asyncio.run(w.start())
    a,b=switched(w,'other')
    assert b['cwd']==a['cwd'] and b['branch']==a['branch'] and b['dirty']=='uncommitted from first segment'
    assert b['session_id']!=a['session_id'] and not b['resume']
    prompt=b['prompt']
    assert TASK in prompt and 'uncommitted' in prompt.lower()
    assert 'quota' in prompt[prompt.index(TASK)+len(TASK):].lower(), 'continuation note must explain the quota cut'
    assert f".multiagents/runs/{result['agent_id']}" in prompt
    import subprocess
    commit=subprocess.run(['git','-C',b['cwd'],'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    assert 'C23 first segment commit' in prompt or commit[:7] in prompt
    assert w.r.check(result['agent_id']).get('current_provider')=='other'


def test_qh_r10_successive_quota_stops_never_ping_pong_before_reset(world):
    w=world(models={'beta':'model-a','other':'model-o'})
    w.set_plan('beta',quota=True)
    asyncio.run(w.start())
    assert [c['instance'] for c in w.calls()]==['alpha','beta','other'],w.calls()
    attempts=w.events('handover_started')
    keys=[(e['agent_id'],e['segment'],e['attempt']) for e in attempts]
    assert len(keys)==len(set(keys))==2


@pytest.mark.parametrize('reset',[False,True])
def test_qh_r11_return_home_only_at_resume_boundary_after_reset(world,reset):
    w=world()
    async def scenario():
        result=await w.start()
        switched(w,'beta')
        if reset: w.reset('alpha')
        assert len(w.calls())==2, 'quota reset must not interrupt a live turn'
        await w.steer(result['agent_id'])
    asyncio.run(scenario())
    assert w.calls()[-1]['instance']==('alpha' if reset else 'beta')
    if reset: assert w.events('return_home')


def test_qh_r11_return_home_failure_keeps_current_and_emits_event(world):
    w=world()
    async def scenario():
        result=await w.start()
        switched(w,'beta')
        w.reset('alpha'); w.set_plan('alpha',reject=True)
        await w.steer(result['agent_id'])
    asyncio.run(scenario())
    assert w.calls()[-1]['instance']=='beta'
    assert w.events('return_home_failed')


def test_qh_r12_reserved_instance_leaves_for_freed_better_tier(world):
    w=world(reserved='reserve',models={'beta':'model-a','reserve':'model-a'})
    w.unusable('beta','other')
    async def scenario():
        result=await w.start()
        switched(w,'reserve')
        w.reset('beta')
        await w.steer(result['agent_id'])
    asyncio.run(scenario())
    assert w.calls()[-1]['instance']=='beta'


async def reserve_tool(name,*args):
    fn=getattr(server,name,None)
    assert callable(fn), f'QH-R16: missing MCP {name}'
    result=fn(*args)
    return await result if inspect.isawaitable(result) else result


def floor_world(world):
    w=world(reserved='reserve',models={'reserve':'model-a'})
    w.unusable('alpha','beta','other')
    w.readings['reserve']=Budget('reserve',known=True,headroom=.2)
    return w


@pytest.mark.parametrize('answer',['allow','veto','timeout'])
def test_qh_r14_qh_r16_allow_veto_and_timeout(world,answer):
    w=floor_world(world)
    async def scenario():
        await w.start()
        requests=w.events('reserve_request')
        assert requests, 'idle floor candidate must request reserve'
        assert not w.calls(), 'no floor dispatch before answer or deadline'
        wake=await server.wait_for_agents(None,0)
        assert wake.get('status')=='awaiting_orchestrator',wake
        request=requests[-1]
        request_id=request.get('request_id')
        assert request_id, 'reserve request must expose an answerable request id'
        if answer=='allow': await reserve_tool('allow_reserve',request_id)
        elif answer=='veto': await reserve_tool('veto_reserve',request_id,'protect orchestrator quota')
        else:
            w.clock[0]+=119
            await w.r.resume_deferred()
            assert not w.calls()
            w.clock[0]+=1
        await w.r.resume_deferred()
        await asyncio.sleep(.1)
        if answer=='veto':
            assert not w.calls()
            assert w.events('reserve_vetoed')
            await w.r.resume_deferred()
            assert len(w.events('reserve_request'))==1
            w.reset('alpha')
            await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            assert w.calls()[0]['instance']=='alpha', 'quota reset must release veto deferral'
        else:
            assert w.calls() and w.calls()[0]['instance']=='reserve'
            assert w.events('reserve_allowed')
    asyncio.run(scenario())


def test_qh_r15_floor_candidates_queued_fifo_one_at_a_time(world):
    w=floor_world(world)
    async def scenario():
        first=await w.r.start('worker','first FIFO task')
        second=await w.r.start('worker','second FIFO task')
        requests=w.events('reserve_request')
        assert requests and 'first FIFO task' in json.dumps(requests[0])
        assert not w.calls()
        await reserve_tool('allow_reserve',requests[0]['request_id'])
        await w.r.resume_deferred()
        await asyncio.sleep(.1)
        assert w.calls() and 'first FIFO task' in w.calls()[0]['prompt']
        # The second request cannot spend quota until its own approval.
        assert all('second FIFO task' not in c['prompt'] for c in w.calls())
    asyncio.run(scenario())


def test_qh_r17_floor_quota_stop_defers_without_switching_again(world):
    w=floor_world(world)
    w.set_plan('reserve',quota=True)
    async def scenario():
        await w.r.start('worker',TASK)
        requests=w.events('reserve_request')
        assert requests
        await reserve_tool('allow_reserve',requests[0]['request_id'])
        await w.r.resume_deferred()
        await asyncio.sleep(.2)
        assert [c['instance'] for c in w.calls()]==['reserve']
        await w.r.resume_deferred()
        assert len(w.calls())==1
    asyncio.run(scenario())


def test_qh_r18_switch_events_have_contract_fields(world):
    w=world()
    asyncio.run(w.start())
    required={'kind','agent_id','session_id','from','to','tier','segment','attempt','reason','at'}
    for kind in ('handover_started','handover_completed'):
        rows=w.events(kind)
        assert rows, f'missing {kind}'
        for e in rows:
            assert required<=e.keys(),e
            assert e['from']=='alpha' and e['to']=='beta' and e['tier']==1


def test_qh_r19_public_check_collect_and_tree_show_segments(world):
    w=world()
    result=asyncio.run(w.start())
    aid=result['agent_id']
    for output in (server.check_agent(aid),server.collect_agent(aid)):
        assert output.get('home_provider')=='alpha' and output.get('current_provider')=='beta',output
        segments=output.get('segments')
        assert segments and [s['provider'] for s in segments]==['alpha','beta']
        for s in segments:
            assert {'provider','account','model','session_id','started_at','ended_at','end_reason'}<=s.keys()
        assert segments[0]['end_reason'] and segments[0]['ended_at'] is not None
    shown=json.dumps(server.agent_tree())
    assert 'home_provider' in shown and 'current_provider' in shown and 'segments' in shown


def test_qh_r20_accounting_keeps_earlier_segment_attribution_and_reports_floor(world):
    w=world(reserved='reserve')
    result=asyncio.run(w.start())
    switched(w,'beta')
    shown=server.budget_status()
    serialized=json.dumps(shown)
    assert 'reserve' in serialized and ('0.25' in serialized or '25%' in serialized),shown
    segments=w.r.collect(result['agent_id']).get('segments')
    assert segments and [s['provider'] for s in segments]==['alpha','beta']
    # Provider/account attribution is observable via the budget tool, not a
    # particular ledger or an internal aggregate structure.
    assert 'alpha' in serialized and 'beta' in serialized
    # The fake local provider has no vault; unknown account is legitimate.
    # Segment usage attribution must still retain both providers.
    def amounts(value, name):
        if isinstance(value,dict):
            own=[value] if value.get('provider')==name else []
            own += [value[name]] if name in value and isinstance(value[name],dict) else []
            return own + [row for child in value.values() for row in amounts(child,name)]
        if isinstance(value,list): return [row for child in value for row in amounts(child,name)]
        return []
    for name in ('alpha','beta'):
        rows=amounts(shown,name)
        def has_cost(value):
            if isinstance(value,dict):
                return float(value.get('cost_usd',0) or 0)>0 or any(has_cost(x) for x in value.values())
            if isinstance(value,list): return any(has_cost(x) for x in value)
            return False
        assert any(has_cost(row) for row in rows),(name,shown)


def test_qh_r21_subagent_runs_are_covered(world):
    w=world()
    # Parent is an actual completed start_agent node; identity is supplied
    # through the existing harness's process environment boundary.
    w.set_plan('alpha',quota=False)
    async def scenario():
        parent=await w.start()
        w.set_plan('alpha',quota=True)
        h.as_subagent(w.mp,agent_id=parent['agent_id'],depth=1,can_spawn=True)
        before=len(w.calls())
        child=await w.start()
        assert child['agent_id']!=parent['agent_id']
        assert [c['instance'] for c in w.calls()[before:]]==['alpha','beta']
    asyncio.run(scenario())
    assert [c['instance'] for c in w.calls()][-2:]==['alpha','beta']


def test_qh_r8_divergent_target_is_preserved_and_source_stays_resumable(world):
    w=world(mode='copy',models={'beta':'model-a','other':'model-o'})
    w.unusable('reserve')
    source=w.stores['alpha']/(w.session_id+'.jsonl')
    target=w.stores['beta']/(w.session_id+'.jsonl')
    source.write_text('{"history":"source conversation"}\n')
    target.write_text('{"history":"different conversation"}\n')
    asyncio.run(w.start())
    assert target.read_text()=='{"history":"different conversation"}\n'
    assert source.read_text().startswith('{"history":"source conversation"}\n')
    assert w.events('handover_failed'), 'divergent transfer must be reported'
    assert w.calls()[-1]['instance']=='other', 'failed sibling transfer must try next tier'
    assert not w.calls()[-1]['resume']


def test_qh_r8_identical_target_allows_idempotent_session_transfer(world):
    w=world(mode='copy')
    initial='{"history":"same conversation"}\n'
    (w.stores['alpha']/(w.session_id+'.jsonl')).write_text(initial)
    # Source adds the alpha turn before handing over. Use a gated first turn
    # to capture exactly the source transcript before permitting its quota stop.
    gate=w.tmp/'release-alpha'
    w.set_plan('alpha',gate=str(gate))
    async def scenario():
        result=await w.r.start('worker',TASK)
        try:
            await wait_until(lambda: bool(w.calls()))
            source=w.stores['alpha']/(w.session_id+'.jsonl')
            await wait_until(lambda: len(source.read_text().splitlines())==2)
            (w.stores['beta']/(w.session_id+'.jsonl')).write_bytes(source.read_bytes())
        finally: gate.touch()
        await w.settle(result['agent_id'])
    asyncio.run(scenario())
    switched(w,'beta')
    assert not w.events('handover_failed')


async def wait_until(predicate):
    end=asyncio.get_running_loop().time()+8
    while asyncio.get_running_loop().time()<end:
        if predicate(): return
        await asyncio.sleep(.01)
    pytest.fail('fixture observation not reached')


def test_qh_r8_copy_concurrent_reader_never_observes_partial_installation(world):
    import threading
    w=world(mode='copy')
    prefix=json.dumps({'history':'x'*2_000_000})+'\n'
    (w.stores['alpha']/(w.session_id+'.jsonl')).write_text(prefix)
    target=w.stores['beta']/(w.session_id+'.jsonl')
    stop=threading.Event()
    bad=[]
    seen=threading.Event()
    gate=w.tmp/'release-copy-target'
    w.set_plan('beta',pre_append_gate=str(gate))
    def read_target():
        while not stop.is_set():
            try: data=target.read_text()
            except FileNotFoundError: continue
            seen.set()
            if not data.startswith(prefix):
                bad.append('partial conversation prefix'); return
            try:
                for line in data.splitlines(): json.loads(line)
            except ValueError:
                bad.append('incomplete session JSON'); return
    reader=threading.Thread(target=read_target)
    reader.start()
    async def scenario():
        result=await w.r.start('worker',TASK)
        try:
            await wait_until(lambda: len(w.calls())>=2 or w.r.check(result['agent_id'])['status'] not in ('running','pending'))
            assert len(w.calls())>=2,'copy handover missing'
            await wait_until(seen.is_set)
        finally:
            stop.set(); reader.join(timeout=5); gate.touch()
            await w.settle(result['agent_id'])
    try: asyncio.run(scenario())
    finally:
        stop.set(); reader.join(timeout=5); gate.touch()
    switched(w,'beta')
    assert not bad,bad


def test_qh_r5_disabled_sibling_is_not_candidate(world):
    w=world(models={'beta':'model-a','other':'model-o'})
    w.providers['beta']['enabled']=False
    w.unusable('reserve')
    w.reload()
    asyncio.run(w.start())
    switched(w,'other')


def test_qh_r5_unknown_quota_counts_as_usable(world):
    w=world()
    w.readings['beta']=Budget('beta',known=False,headroom=None)
    asyncio.run(w.start())
    switched(w,'beta')


def test_qh_r5_tier3_obeys_agent_list_order(world):
    w=world(mode='none',models={'other':'model-o','beta':'model-a'})
    w.unusable('reserve')
    asyncio.run(w.start())
    a,b=switched(w,'other')
    assert b['model']=='model-o' and not b['resume']


def test_qh_r14_running_agent_prevents_floor_request_and_dispatch(world):
    w=floor_world(world)
    # A separate roster entry owns a genuinely running fake CLI; the floor
    # candidate's own list still contains only the unusable alpha and reserve.
    agents=yaml.safe_load((w.paths.config/'agents.yaml').read_text())
    agents['agents']['busy']={'provider':'other','model':'model-o','models':{}}
    (w.paths.config/'agents.yaml').write_text(yaml.safe_dump(agents))
    w.readings['other']=Budget('other',known=True,headroom=.8)
    gate=w.tmp/'release-busy'
    w.set_plan('other',gate=str(gate))
    w.r=Runner(w.paths,config.load(w.paths,seed=False))
    async def scenario():
        busy=await w.r.start('busy','already running work')
        try:
            await wait_until(lambda: bool(w.calls()))
            assert w.r.check(busy['agent_id'])['status']=='running'
            candidate=await w.r.start('worker','floor candidate')
            if candidate.get('agent_id') and w.r.check(candidate['agent_id'])['status'] in ('running','pending'):
                await w.settle(candidate['agent_id'])
            assert not w.events('reserve_request'), 'orchestrator is not idle'
            assert all(c['instance']!='reserve' for c in w.calls())
        finally:
            gate.touch()
            await w.settle(busy['agent_id'])
    asyncio.run(scenario())


def test_qh_r15_second_floor_run_cannot_overlap_first(world):
    w=floor_world(world)
    gate=w.tmp/'release-floor'
    w.set_plan('reserve',gate=str(gate))
    async def scenario():
        await w.r.start('worker','first task under floor')
        requests=w.events('reserve_request')
        assert requests
        try:
            await reserve_tool('allow_reserve',requests[0]['request_id'])
            await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            await w.r.start('worker','second task under floor')
            await w.r.resume_deferred()
            assert len(w.calls())==1, 'two runs dispatched on floor simultaneously'
            for request in w.events('reserve_request')[1:]:
                await reserve_tool('allow_reserve',request['request_id'])
            await w.r.resume_deferred()
            assert len(w.calls())==1
        finally: gate.touch()
    asyncio.run(scenario())


def test_qh_r17_floor_leaves_at_boundary_when_home_reset(world):
    w=floor_world(world)
    async def scenario():
        initial=await w.r.start('worker',TASK)
        requests=w.events('reserve_request')
        assert requests
        await reserve_tool('allow_reserve',requests[0]['request_id'])
        await w.r.resume_deferred()
        await wait_until(lambda: bool(w.calls()))
        aid=w.events('reserve_request')[0]['agent_id']
        await w.settle(aid)
        w.reset('alpha')
        await w.steer(aid)
        assert w.calls()[-1]['instance']=='alpha'
    asyncio.run(scenario())


def test_qh_r11_quota_reset_does_not_interrupt_live_foreign_turn(world):
    w=world()
    gate=w.tmp/'release-beta'
    w.set_plan('beta',gate=str(gate))
    async def scenario():
        result=await w.r.start('worker',TASK)
        try:
            await wait_until(lambda: len(w.calls())>=2 or w.r.check(result['agent_id'])['status'] not in ('running','pending'))
            switched(w,'beta')
            assert w.calls()[-1]['instance']=='beta'
            w.reset('alpha')
            await w.r.resume_deferred()
            assert len(w.calls())==2
            assert w.r.check(result['agent_id'])['status']=='running'
        finally:
            gate.touch()
            await w.settle(result['agent_id'])
    asyncio.run(scenario())


def test_qh_r22_no_candidates_preserves_old_quota_stop(world):
    w=world(models={})
    w.unusable('beta','reserve','other')
    result=asyncio.run(w.start())
    assert [c['instance'] for c in w.calls()]==['alpha']
    assert w.r.check(result['agent_id'])['status']=='limited'
    assert not w.events('handover_completed')


def test_qh_r3_shipped_context_modes_are_data(world):
    w=world()
    loaded=config.load(w.paths,seed=False)
    expected={'claude':{'docker':'shared','local':'none'},
              'codex':{'docker':'copy','local':'copy'},
              'agy':{'docker':'none','local':'none'}}
    for name, modes in expected.items():
        actual=loaded.providers[name].get('handover_mode',{})
        assert actual==modes,(name,actual)


def test_qh_r10_attempt_is_durable_before_target_launch(world):
    w=world()
    asyncio.run(w.start())
    a,b=switched(w,'beta')
    attempts=[e for e in b['events_before_launch'] if e.get('kind')=='handover_started']
    assert attempts,'handover attempt must be persisted before target CLI runs'
    assert all({'agent_id','segment','attempt'}<=e.keys() for e in attempts)


def test_qh_r1_enabled_defaults_true_when_omitted(world):
    w=world()
    w.project['quota_handover'].pop('enabled')
    w.reload()
    asyncio.run(w.start())
    switched(w,'beta')


@pytest.mark.parametrize('home_mode,changed,expected',[('copy',False,'alpha'),('copy',True,'other'),('none',False,'other')])
def test_qh_r11_cross_family_return_requires_resumable_unchanged_home(world,home_mode,changed,expected):
    w=world(mode=home_mode,models={'other':'model-o'})
    w.unusable('beta','reserve')
    async def scenario():
        result=await w.start()
        a,b=switched(w,'other')
        if changed:
            source=w.stores['alpha']/(a['session_id']+'.jsonl')
            source.write_text(source.read_text()+'{"external":"changed home transcript"}\n')
        w.reset('alpha')
        await w.steer(result['agent_id'])
        final=w.calls()[-1]
        assert final['instance']==expected
        if expected=='alpha': assert final['resume']==a['session_id']
        else: assert final['resume']==b['session_id']
    asyncio.run(scenario())


def test_qh_r10_restart_during_unverified_target_resume_does_not_launch_twice(world):
    """Kill only our server process, while target CLI has not confirmed resume.

    Restart uses normal Runner construction + public resume_deferred, not an
    implementation-specific attempt ledger or recovery function.
    """
    import os
    import subprocess
    import time
    w=world()
    gate=w.tmp/'release-restarted-target'
    w.set_plan('beta',pre_append_gate=str(gate))
    aid_file=w.tmp/'server-agent-id'
    driver=w.tmp/'server-process.py'
    driver.write_text('''import asyncio, sys
from pathlib import Path
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
async def main():
    paths=ProjectPaths(Path(sys.argv[1]))
    run=Runner(paths,load(paths,seed=False))
    aidfile=Path(sys.argv[2])
    if sys.argv[3]=='start':
        answer=await run.start('worker', 'Implement the assigned quota handover task')
        aidfile.write_text(answer['agent_id'])
    else:
        await run.resume_deferred()
    aid=aidfile.read_text()
    end=asyncio.get_running_loop().time()+12
    while asyncio.get_running_loop().time()<end:
        if run.check(aid)['status'] not in ('pending','running'):
            await asyncio.sleep(.1)
            if run.check(aid)['status'] not in ('pending','running'): return
        await asyncio.sleep(.01)
from pytest import MonkeyPatch
from multiagents import budget
with MonkeyPatch.context() as mp:
    mp.setattr(budget,'read_all',lambda *a,**kw: {
        n:budget.Budget(n,known=True,headroom=.8)
        for n in ('alpha','beta','reserve','other')})
    mp.setattr(budget,'read_provider',lambda n,*a,**kw:
        budget.Budget(n,known=True,headroom=0 if n=='alpha' else .8))
    asyncio.run(main())
''')
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    argv=[sys.executable,str(driver),str(w.paths.root),str(aid_file)]
    first_log=w.tmp/'first-server.log'
    second_log=w.tmp/'second-server.log'
    processes=[]
    with first_log.open('w') as log:
        first=subprocess.Popen([*argv,'start'],env=env,stdout=log,stderr=log)
        processes.append(first)
        try:
            end=time.monotonic()+8
            while time.monotonic()<end and len(w.calls())<2 and first.poll() is None:
                time.sleep(.01)
            assert w.calls() and w.calls()[0]['instance']=='alpha',first_log.read_text()
            assert len(w.calls())>=2, 'missing handover: target was never launched'
            assert w.calls()[1]['resume']==w.calls()[0]['session_id']
            assert w.events('handover_started'), 'attempt must precede target verification'
            first.kill()
            first.wait(timeout=5)
            with second_log.open('w') as log2:
                second=subprocess.Popen([*argv,'restart'],env=env,stdout=log2,stderr=log2)
                processes.append(second)
                gate.touch()
                second.wait(timeout=15)
                assert second.returncode==0,second_log.read_text()
            assert [c['instance'] for c in w.calls()]==['alpha','beta'],w.calls()
            latest=Runner(w.paths,config.load(w.paths,seed=False)).check(aid_file.read_text())
            assert latest.get('current_provider')=='beta',latest
            assert latest['status'] in ('done','idle','merged'),latest
        finally:
            gate.touch()
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
