"""IC-C22: session declarations govern payload projection and export."""
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


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c22_session_survives_scheduler_restart_and_attempt_completion(world, mode):
    world.configure(mode); world.start()
    node = world.launch('session', nonce='survives-discard')
    old = world.report(node)
    s3.wait_done(world, node, a.WAIT)
    world.restart(hard=True)
    world.reopen(node)
    rep = world.report(node, 2)
    assert rep['prior'] == {'session':old['session'], 'nonce':'survives-discard'}
    assert a.resume(world, node) == 'resumed'


def test_ic_c22_forbidden_export_is_quarantined_and_next_resume_fails(world):
    world.configure('export')
    world.fake.entry['session_store'] = {'dir':'sessions', 'glob':'*', 'forbidden_profile':'sessions/*.secret'}
    world.p.write(); world._reset_server(); world.start()
    node = world.launch('session', forbidden=True)
    s3.wait_done(world, node, a.WAIT)
    view = world.get(node)
    assert 'quarantined' in ic.text_of(view) and 'credential.secret' in ic.text_of(view), view
    world.reopen(node)
    world.wait_runs(node, 2, a.WAIT)
    s3.wait_done(world, node, a.WAIT)
    assert a.resume(world, node) == 'failed'


def test_ic_c22_explicit_none_overrides_legacy_session_store(world):
    world.configure('none')
    world.fake.entry['session_store'] = {'dir':'sessions', 'glob':'*.json'}
    world.p.write(); world._reset_server(); world.start()
    node = world.launch('session')
    s3.wait_done(world, node, a.WAIT)
    world.reopen(node)
    rep = world.report(node, 2)
    assert rep['resumed'] is None
    assert a.resume(world, node) == 'unsupported'


def test_ic_c22_legacy_session_store_without_session_block_exports(world):
    world.configure('export')
    del world.fake.entry['session']
    world.fake.entry['session_store'] = {'dir':'sessions', 'glob':'*.json'}
    world.p.write(); world._reset_server(); world.start()
    node = world.launch('session')
    old = world.report(node)
    s3.wait_done(world, node, a.WAIT)
    world.reopen(node)
    new = world.report(node, 2)
    assert new['resumed'] == old['session'] and new['prior']['session'] == old['session']


@pytest.mark.parametrize('session', [None, {'mode':'home','path':''}, {'mode':'export','path':''}])
def test_ic_c22_null_block_or_empty_payload_path_is_invalid(session):
    from multiagents.providers import load_providers
    with pytest.raises(ValueError) as error:
        load_providers({'session-probe':{'bin':'python3','session':session}})
    assert 'session-probe' in str(error.value)
    assert ('path' if isinstance(session,dict) else 'session') in str(error.value)


def test_ic_c22_session_declaration_inherits_through_extends(world):
    world.configure('export')
    declaration = world.fake.entry.pop('session')
    world.p.providers['session-base'] = {'session':declaration}
    world.fake.entry['extends'] = 'session-base'
    world.p.write(); world._reset_server(); world.start()
    node = world.launch('session')
    old = world.report(node)
    s3.wait_done(world, node, a.WAIT)
    world.reopen(node)
    new = world.report(node, 2)
    assert new['resumed'] == old['session'] and new['prior']['session'] == old['session']
    assert a.resume(world,node) == 'resumed'


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c22_attempt_discard_keeps_conversation_payload(world, mode):
    world.configure(mode); world.start()
    node = world.launch('session')
    old = world.report(node)
    s3.wait_done(world,node,a.WAIT)
    backend = world.be()
    owned = ic.run(backend.list_owned(world.slug))
    assert len(owned) == 1, owned
    ic.run(backend.remove(ic.identity_of(owned[0]), False))
    world.reopen(node)
    new = world.report(node,2)
    assert new['resumed'] == old['session'] and new['prior']['session'] == old['session']


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c22_node_disposal_removes_its_conversation_payload(world, mode):
    world.configure(mode); world.start()
    node = world.launch('session')
    old = world.report(node)
    s3.wait_done(world,node,a.WAIT)
    assert a.payload_files(world.tmp,old['session']), 'no persisted conversation payload'
    view = world.get(node)
    reply = world.rpc('dispose_node',{'id':node,'revision':view['revision']})
    assert reply.get('ok') is True, reply
    world.wait(lambda: not a.payload_files(world.tmp,old['session']), a.WAIT, 'conversation disposal')


def test_ic_c22_forbidden_payload_is_refused_before_next_launch(world):
    if world.backend_name == 'docker': pytest.skip('host mutation test uses reference HOME paths')
    world.configure('home')
    world.fake.entry['session_store'] = {'dir':'sessions','glob':'*','forbidden_profile':'sessions/*.secret'}
    world.p.write(); world._reset_server(); world.start()
    node = world.launch('session')
    old = world.report(node)
    s3.wait_done(world,node,a.WAIT)
    files = a.payload_files(world.tmp,old['session'])
    assert files
    for path in files: (path.parent/'credential.secret').write_text('must never be projected')
    world.reopen(node)
    world.wait_runs(node,2,a.WAIT)
    view = s3.wait_done(world,node,a.WAIT)
    assert len(world.reports(node)) == 1, 'provider launched with forbidden payload'
    assert 'forbidden' in ic.text_of(view).lower(), view


@pytest.mark.parametrize('mode', ['home','export'])
def test_ic_c22_alias_disposal_removes_conversation_payload(tmp_path, monkeypatch, mode):
    import ic_prov as ip
    from nc_fixture.m4_world import commit_entry, verdict_entry
    w = a.loop_world(tmp_path,monkeypatch,mode=mode)
    try:
        w.fxw.queue(commit_entry('a.txt','one\n','r1'))
        w.fxr.queue(verdict_entry('approved',tag='R1'))
        top,wk,rv = ip.start_pair(w)
        w.wait_state(top,'done',timeout=a.WAIT)
        session = w.fxr.calls()[0]['session']
        assert a.payload_files(w.tmp,session), 'alias has no persistent payload'
        reply = w.root_op('dispose_node',top)
        assert reply.get('ok') is True, reply
        w.until(lambda: not a.payload_files(w.tmp,session), a.WAIT, what='alias payload disposal')
    finally:
        w.close()
