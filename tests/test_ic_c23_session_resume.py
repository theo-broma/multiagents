"""IC-C23: observed session continuity, failure and exclusive ownership."""
import concurrent.futures
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_s3 as s3
import ic_support as ic

@pytest.fixture(params=['reference', a.DOCKER])
def world(request, tmp_path, monkeypatch, st_live_docker):
    yield from a.make_world(request, tmp_path, monkeypatch)


def first(world, mode, **fields):
    world.configure(mode); world.start()
    node = world.launch('session', **fields)
    rep = world.report(node)
    if not fields.get('hold'): s3.wait_done(world, node, a.WAIT)
    return node, rep


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_relaunch_resumes_recorded_session_and_payload(world, mode):
    node, old = first(world, mode)
    assert a.resume(world, node) == 'fresh'
    world.reopen(node)
    new = world.report(node, 2)
    s3.wait_done(world, node, a.WAIT)
    assert new['resumed'] == old['session']
    assert new['prior'] == {'session':old['session'], 'nonce':'payload'}
    assert a.resume(world, node) == 'resumed'


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_steer_resumes_after_stopping_previous_writer(world, mode):
    node, old = first(world, mode, hold=True)
    run = world.runs(node)[-1]
    reply = world.rpc('steer_run', {'run_id':run['run_id'], 'message':'continue the session'})
    assert reply.get('ok') is True, reply
    world.wait_runs(node, 2, a.WAIT)
    new = world.report(node, 2)
    assert new['resumed'] == old['session'] and new['prior']['session'] == old['session']
    assert a.resume(world, node) == 'resumed'


def test_ic_c23_none_records_unsupported(world):
    node, _ = first(world, 'none')
    assert a.resume(world, node) == 'unsupported'


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'reject', 'unconfirmed'])
@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_resume_failure_ends_other_never_continues_fresh(world, mode, failure):
    fields = {'missing':True} if failure == 'missing' else {'corrupt':True} if failure == 'corrupt' else {'reject':True} if failure == 'reject' else {'confirm':False}
    node, _ = first(world, mode, **fields)
    world.reopen(node)
    world.wait_runs(node, 2, a.WAIT)
    view = s3.wait_done(world, node, a.WAIT)
    assert a.resume(world, node) == 'failed', view
    assert any(c.get('class') == 'other' for c in ic.causes(view)), view
    assert any('reason' in o and o['reason'] for o in ic.objects(world.runs(node)[-1])), view


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_unknown_predecessor_holds_without_resume(world, mode):
    node, _ = first(world, mode, hold=True)
    world.plan({'calls':True, 'inspect':'unknown'})
    reply = world.rpc('steer_run', {'run_id':world.runs(node)[0]['run_id'], 'message':'continue'})
    assert reply.get('ok') is True, reply
    world.wait(lambda: (v:=world.get(node)) and (v['state']=='held' or v.get('hold')),
               a.WAIT, 'unconfirmed predecessor hold')
    assert len(world.reports(node)) == 1


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_concurrent_reopens_never_start_two_writers(world, mode):
    node, _ = first(world, mode, resume_hold=True)
    revision = world.get(node)['revision']
    args = {'id':node, 'revision':revision}
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: world.rpc('relaunch_node', args), range(2)))
    assert any(r.get('ok') for r in replies), replies
    world.report(node, 2)
    assert len(world.runs(node)) == 2, world.get(node)


def test_ic_c23_forced_new_session_records_fresh_and_does_not_resume(world):
    node, old = first(world, 'export')
    backend = world.be()
    old_resource = ic.identity_of(ic.run(backend.list_owned(world.slug))[0])
    world.reopen(node, new_session=[node])
    new = world.report(node, 2)
    ic.run(backend.remove(old_resource,False))
    assert new['resumed'] is None and new['session'] != old['session']
    assert new['prior'] is None
    assert a.resume(world, node) == 'fresh'


def test_ic_c23_confirmation_with_a_different_session_id_is_failed(world):
    node, _ = first(world, 'export', wrong_confirmation=True)
    world.reopen(node)
    world.wait_runs(node, 2, a.WAIT)
    view = s3.wait_done(world, node, a.WAIT)
    assert a.resume(world, node) == 'failed', view
    assert any(c.get('class') == 'other' for c in ic.causes(view)), view


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_loop_alias_resumes_on_next_round(tmp_path, monkeypatch, mode):
    import ic_prov as ip
    from nc_fixture.m4_world import commit_entry, finding, verdict_entry
    w = a.loop_world(tmp_path, monkeypatch, mode=mode)
    try:
        w.fxw.queue(commit_entry('a.txt','one\n','r1'), commit_entry('b.txt','two\n','r2'))
        w.fxr.queue(verdict_entry('rejected',[finding('redo')],tag='R1'), verdict_entry('approved',tag='R2'))
        top,wk,rv = ip.start_pair(w)
        w.wait_state(top,'done',timeout=a.LOOP_WAIT)
        first_call, second_call = w.fxr.calls()
        assert second_call['resume'] == first_call['session']
        assert a.loop_reports(w)[1]['prior']['session'] == first_call['session']
        assert w.runs(rv)[-1]['session_resume'] == 'resumed'
    finally:
        w.close()


@pytest.mark.parametrize('change', ['provider','family','account','model'])
def test_ic_c23_incompatible_binding_is_unsupported_and_held(tmp_path, monkeypatch, change):
    import ic_prov as ip
    from nc_fixture.m4_world import commit_entry, finding, verdict_entry
    w = a.loop_world(tmp_path, monkeypatch)
    try:
        w.fxw.queue(commit_entry('a.txt','one\n','r1'), commit_entry('b.txt','two\n','r2'))
        w.fxr.queue(verdict_entry('rejected',[finding('redo')],tag='R1',gate='change'),verdict_entry('approved',tag='R2'))
        top,wk,rv = ip.start_pair(w)
        w.wait_spawn('R1',w.fxr,timeout=a.LOOP_WAIT)
        if change == 'provider':
            other = w.provider('other')
            other.entry.update(w.fxr.entry)
            w.agents['rv']['provider'] = 'other'
            w.agents['rv']['model'] = 'other/m1'
        elif change == 'model':
            w.agents['rv']['model'] = 'fxr/m2'
        elif change == 'family': w.fxr.entry['family'] = 'changed-family'
        else: w.fxr.entry['container_account'] = 'changed-account'
        w.write_config()
        w.gate('change',w.fxr)
        view = w.until(lambda: (v:=w.get(rv)) and v['state']=='held' and v, a.WAIT, what='incompatible session hold')
        text = ic.text_of(view)
        assert 'unsupported' in text and change in text, view
        assert len(a.loop_reports(w)) == 1
    finally:
        w.close()


def test_ic_c23_forced_fresh_archives_old_payload_until_node_disposal(world):
    node, old = first(world,'export')
    backend = world.be()
    old_resource = ic.identity_of(ic.run(backend.list_owned(world.slug))[0])
    world.reopen(node,new_session=[node])
    new = world.report(node,2)
    ic.run(backend.remove(old_resource,False))
    s3.wait_done(world,node,a.WAIT)
    assert a.resume(world,node) == 'fresh'
    old_files = a.payload_files(world.tmp,old['session'])
    assert old_files, 'forced fresh start discarded the old conversation archive'
    assert a.payload_files(world.tmp,new['session'])
    reply = world.rpc('dispose_node',{'id':node,'revision':world.get(node)['revision']})
    assert reply.get('ok') is True, reply
    world.wait(lambda: not a.payload_files(world.tmp,old['session']) and not a.payload_files(world.tmp,new['session']),
               a.WAIT,'both archived and current conversation disposal')


def test_ic_c23_unavailable_bound_provider_never_resumes_or_reroutes(tmp_path, monkeypatch):
    import ic_prov as ip
    from nc_fixture.m4_world import commit_entry, finding, verdict_entry
    w = a.loop_world(tmp_path,monkeypatch)
    try:
        w.fxw.queue(commit_entry('a.txt','one\n','r1'),commit_entry('b.txt','two\n','r2'))
        w.fxr.queue(verdict_entry('rejected',[finding('redo')],tag='R1',gate='disable'),verdict_entry('approved',tag='R2'))
        top,wk,rv = ip.start_pair(w)
        w.wait_spawn('R1',w.fxr,timeout=a.LOOP_WAIT)
        w.fxr.entry['enabled'] = False
        w.write_config(); w.gate('disable',w.fxr)
        view = w.until(lambda: (v:=w.get(rv)) and ('session_unavailable' in ic.text_of(v) or v['state']=='held') and v,
                       a.WAIT,what='unavailable conversation hold')
        assert view['state'] != 'running'
        assert len(a.loop_reports(w)) == 1
    finally:
        w.close()


@pytest.mark.parametrize('change', ['provider','family','account','model'])
def test_ic_c23_forced_fresh_allows_incompatible_binding_change(world, change):
    node, old = first(world,'export')
    if change == 'model':
        pins = {node:{'model':'acme/m2'}}
    elif change == 'provider':
        world.p.providers['replacement'] = dict(world.fake.entry)
        pins = {node:{'model':'replacement/m1'}}
    else:
        world.fake.entry['family' if change == 'family' else 'container_account'] = 'new-binding'
        pins = {}
    world.p.write(); world._reset_server()
    backend = world.be()
    old_resource = ic.identity_of(ic.run(backend.list_owned(world.slug))[0])
    world.reopen(node,new_session=[node],pins=pins)
    new = world.report(node,2)
    ic.run(backend.remove(old_resource,False))
    assert new['resumed'] is None and new['session'] != old['session']
    assert a.resume(world,node) == 'fresh'
    assert a.payload_files(world.tmp,old['session']), 'new binding removed old archive'


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c23_two_alias_reopens_run_one_writer_and_hold_the_other(tmp_path, monkeypatch, mode):
    from nc_fixture.agent import task
    w = a.loop_world(tmp_path,monkeypatch,mode=mode)
    template = '''template: s4a-shared
version: 1
params: {}
root:
  key: top
  kind: group
  children:
    - {key: one, kind: simple, agent: rv, task: first, session: B}
    - {key: two, kind: simple, agent: rv, task: second, session: B}
'''
    try:
        w.fxr.queue({'tag':'initial-one'},{'tag':'initial-two'}, {'tag':'reopened','gate':'writer'})
        w.start_scheduler(); w.register_ok(template)
        top = w.instantiate_ok('s4a-shared',{})
        w.wait_state(top,'done',timeout=a.LOOP_WAIT)
        children = w.children(top)
        assert len(children) == 2
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda node: w.root_op('relaunch_node',node),children))
        assert all(r.get('ok') for r in replies), replies
        w.wait_spawn('reopened',w.fxr,timeout=a.LOOP_WAIT)
        views = w.until(lambda: (vs:=[w.get(n) for n in children]) and
                        sorted(v['state'] for v in vs)==['held','running'] and vs,
                        a.WAIT,what='one live conversation writer and one held reopen')
        assert len(w.fxr.calls()) == 3
        assert 'session' in ic.text_of(next(v for v in views if v['state']=='held')).lower()
    finally:
        w.close()


def test_ic_c23_unconfirmed_live_resume_ends_at_declared_deadline(world):
    # A live provider never confirms; the configured 0.25 s deadline, rather
    # than provider exit, must end it. The test polls the resulting state.
    node, _ = first(world,'export',confirm=False,resume_hold=True)
    world.reopen(node)
    world.wait_runs(node,2,a.WAIT)
    view = s3.wait_done(world,node,a.WAIT)
    assert a.resume(world,node) == 'failed', view
    assert any(c.get('class') == 'other' for c in ic.causes(view)), view
