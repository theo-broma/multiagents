"""IC-C21: revocation and reconnect through the granted Unix endpoint.
Crash boundaries and orphan location await the contract's explicit seams.
"""
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s4a as a
import ic_s3 as s3

@pytest.fixture(params=['reference', a.DOCKER])
def world(request, tmp_path, monkeypatch, st_live_docker):
    yield from a.make_world(request, tmp_path, monkeypatch)


def endpoint(world):
    world.configure(rpc=True); world.start()
    # Keep the invocation alive after reporting its endpoint.
    task = __import__('ic_probe').instruction('scoped_rpc', requests=[a.request()])
    # Probe holding is implemented by the slice 4A provider, not a host-side sleep.
    node = world.create(task=task.replace('"requests":', '"hold": true, "requests":'))['id']
    report = world.report(node)
    assert report['replies'][0].get('ok') is True, report
    return node, a.host_endpoint(world, report['endpoint'])


@pytest.mark.parametrize('ending', ['end','cancel','replacement'])
def test_ic_c21_authority_revoked_for_new_and_existing_connections(world, ending):
    node, path = endpoint(world)
    with a.connect(path) as existing:
        assert a.exchange(existing, a.request())['ok'] is True
        if ending == 'cancel':
            assert world.cancel_raw(node)['ok'] is True
        elif ending == 'replacement':
            run = world.runs(node)[-1]
            reply = world.rpc('steer_run', {'run_id':run['run_id'], 'message':'finish'})
            assert reply.get('ok') is True, reply
            world.wait_runs(node, 2, a.WAIT)
        else:
            # stop_run terminates the provider normally through the public lifecycle.
            reply = world.rpc('stop_run', {'run_id':world.runs(node)[-1]['run_id']})
            assert reply.get('ok') is True, reply
        if ending != 'replacement': s3.wait_done(world, node, a.WAIT)
        with pytest.raises((OSError, ConnectionError)):
            a.exchange(existing, a.request('create_node', {'kind':'simple','agent':'worker','task':'must not run'}))
        with pytest.raises(OSError):
            with a.connect(path): pass
        assert not world.get(node).get('children'), 'a post-revocation mutation had an effect'


def test_ic_c21_scheduler_restart_reconnects_same_authority_without_new_attempt(world):
    node, path = endpoint(world)
    before = world.runs(node)
    world.restart(hard=True)
    with a.connect(path) as conn:
        assert a.exchange(conn, a.request())['ok'] is True
    assert [(r['run_id'],r['attempt_id']) for r in world.runs(node)] == [(r['run_id'],r['attempt_id']) for r in before]
    assert len(world.reports(node)) == 1, 'scheduler restart reinvoked the container workload'


def test_ic_c21_unconfirmed_authority_is_held_without_listener(world):
    node, path = endpoint(world)
    world.plan({'calls':True, 'inspect':'unknown'})
    world.restart(hard=True)
    world.wait(lambda: world.get(node)['state'] == 'held', a.WAIT, 'unconfirmed authority hold')
    with pytest.raises(OSError):
        with a.connect(path): pass


def test_ic_c21_mutation_committed_before_revocation_keeps_its_effect(world):
    node,path = endpoint(world)
    with a.connect(path) as conn:
        revision = world.plan_revision()
        created = a.exchange(conn,a.request('create_node',{'kind':'simple','agent':'worker',
            'task':'committed-before-revocation','plan_revision':revision}))
        assert created.get('ok') is True, created
        child = created['result']['id']
        assert world.cancel_raw(node).get('ok') is True
        s3.wait_done(world,node,a.WAIT)
        assert world.get(child)['task'] == 'committed-before-revocation'
