"""IC-C20: the private endpoint uses host authority and existing wire semantics."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_s3 as s3

@pytest.fixture(params=['reference', a.DOCKER])
def world(request, tmp_path, monkeypatch, st_live_docker):
    yield from a.make_world(request, tmp_path, monkeypatch)


def run(world, requests, **fields):
    world.configure(rpc=True); world.start()
    node = world.launch('scoped_rpc', requests=requests, **fields)
    view = s3.wait_done(world, node, a.WAIT)
    assert view['state'] == 'done', view
    return node, world.report(node)


def test_ic_c20_rpc_only_provider_runs_and_round_trips(world):
    _, report = run(world, [a.request()])
    assert report['replies'][0]['ok'] is True, report
    assert report['endpoint'], report


@pytest.mark.parametrize('field,value', [('token','forged'), ('subject','another-run'),
    ('root',True), ('permissions',['delegate']), ('principal',{'root':True}), ('node_id','nd-forged')])
def test_ic_c20_caller_identity_is_forbidden(world, field, value):
    _, report = run(world, [a.request(**{field:value})])
    assert report['replies'][0].get('error',{}).get('error') == 'forbidden', report


def test_ic_c20_out_of_scope_node_is_forbidden_and_unchanged(world):
    world.configure(rpc=True); world.start()
    victim = world.launch('session')
    before = s3.wait_done(world, victim, a.WAIT)
    node = world.launch('scoped_rpc', requests=[a.request('get_node', {'id':victim}),
        a.request('cancel_node', {'id':victim, 'revision':before['revision']})])
    s3.wait_done(world, node, a.WAIT)
    assert all(r.get('error',{}).get('error') == 'forbidden' for r in world.report(node)['replies'])
    assert world.get(victim)['state'] == before['state']


def test_ic_c20_mutation_retry_deduplicates_and_changed_payload_is_refused(world):
    # create_node is the public delegation operation, scoped below the caller.
    req = a.request('create_node', {'kind':'simple', 'agent':'worker', 'task':'delegated', 'plan_revision':'CURRENT'})
    _, report = run(world, [req, req, dict(req, args=dict(req['args'],task='changed'))])
    first, again, changed = report['replies']
    assert first.get('ok') is True, report
    assert again == first, report
    assert changed.get('ok') is False and changed.get('error',{}).get('error') == 'request_id_reused', report


@pytest.mark.skipif(not __import__('ic_docker').OPT_IN, reason='guest containment: SG_TEST_DOCKER=1')
def test_ic_c20_guest_mount_is_only_readonly_endpoint(world):
    if world.backend_name != 'docker': pytest.skip('reference backend makes no containment claim')
    _, report = run(world, [a.request()],check_readonly=True)
    assert report['endpoint'] == '/run/multiagents/rpc/rpc.sock'
    assert report['entries'] == ['rpc.sock']
    assert report['writable']['ok'] is False
    assert not any('TOKEN' in key for key in report['rpc_env'])


@pytest.mark.parametrize('op', ['steer_run','stop_run'])
def test_ic_c20_other_subject_run_cannot_be_mutated(world, op):
    world.configure(rpc=True); world.start()
    victim = world.launch('session',hold=True)
    world.report(victim)
    victim_run = world.runs(victim)[-1]
    args = {'run_id':victim_run['run_id']}
    if op == 'steer_run': args['message'] = 'unauthorised'
    node = world.launch('scoped_rpc',requests=[a.request(op,args)])
    s3.wait_done(world,node,a.WAIT)
    reply = world.report(node)['replies'][0]
    assert reply.get('error',{}).get('error') == 'forbidden', reply
    assert [r['run_id'] for r in world.runs(victim)] == [victim_run['run_id']]
    assert world.get(victim)['state'] == 'running'


@pytest.mark.skipif(not __import__('ic_docker').OPT_IN, reason='SG_TEST_DOCKER=1 required')
def test_ic_c20_guest_cannot_reach_root_socket_or_scheduler_store(world):
    if world.backend_name != 'docker': pytest.skip('containment requires Docker')
    world.configure(rpc=True); world.start()
    paths = [str(world.sock), str(world.rpc_dir), str(world.root / '.multiagents' / 'scheduler')]
    node = world.launch('scoped_rpc',requests=[a.request()],hidden_paths=paths)
    rep = world.report(node)
    assert rep['replies'][0].get('ok') is True, rep
    assert rep['hidden'] == dict.fromkeys(paths,False), rep


@pytest.mark.parametrize('op,args', [('list_templates',{}),('scheduler_status',{}),('wait_for_nodes',{'timeout':0})])
def test_ic_c20_existing_read_operations_work_through_private_endpoint(world, op, args):
    _, rep = run(world,[a.request(op,args)])
    assert rep['replies'][0].get('ok') is True, rep
