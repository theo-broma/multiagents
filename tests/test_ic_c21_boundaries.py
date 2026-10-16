"""IC-C21 / K18: endpoint recovery and host-owned orphan directories."""
import json
import sys
import uuid
from pathlib import Path

import pytest
from multiagents.paths import ProjectPaths

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s3 as s3
import ic_s4a as a


@pytest.fixture(params=['reference', a.DOCKER])
def world(request, tmp_path, monkeypatch, st_live_docker):
    yield from a.make_world(request, tmp_path, monkeypatch)


def endpoint_dir(world, endpoint_id):
    return ProjectPaths(world.root).scheduler / 'endpoints' / endpoint_id


def refuses(path):
    with pytest.raises(OSError):
        with a.connect(str(path)):
            pass


def listing(world):
    result = world.cli('scheduler', 'orphans', timeout=world.bound())
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['orphans_error'] is None, value
    return value['orphans']


@pytest.mark.parametrize('point', ['provision_done', 'run_entry', 'exited_seen', 'remove_entry'])
def test_k18_1_nonrunning_attempt_has_no_live_endpoint_after_boundary_recovery(world, point):
    world.configure(rpc=True)
    world.start()
    pid = world.crash_at(point)
    node = world.launch('scoped_rpc', requests=[a.request()])
    world.wait_dead(pid, timeout=world.bound())
    world.plan({'calls': True})
    world.start()  # the dead scheduler cannot answer: recover, then read the record
    attempts = {row['attempt_id'] for row in world.runs(node)}
    assert attempts, 'the crash must leave a recorded attempt to recover'
    assert s3.wait_done(world, node, world.bound())['state'] == 'done'
    attempts.update(row['attempt_id'] for row in world.runs(node))
    for attempt in attempts:
        refuses(endpoint_dir(world, attempt) / 'rpc.sock')
    assert not any(row.get('endpoint_id') in attempts for row in listing(world)), \
        'recorded attempts must not become orphan endpoints'


def test_k18_1_started_attempt_recreates_endpoint_without_reinvocation(world):
    world.configure(rpc=True)
    world.start()
    pid = world.crash_at('run_done')
    node = world.launch('scoped_rpc', requests=[a.request()], hold=True)
    world.wait_dead(pid, timeout=world.bound())
    world.plan({'calls': True})
    world.start()  # the dead scheduler cannot answer: recover, then read the record
    before = world.runs(node)
    assert len(before) == 1
    path = endpoint_dir(world, before[0]['attempt_id']) / 'rpc.sock'
    report = world.report(node)
    assert Path(a.host_endpoint(world, report['endpoint'])) == path
    with a.connect(str(path)) as conn:
        assert a.exchange(conn, a.request()).get('ok') is True
    assert [(r['run_id'], r['attempt_id']) for r in world.runs(node)] == \
        [(r['run_id'], r['attempt_id']) for r in before]
    assert len(world.reports(node)) == 1


def orphan(world):
    endpoint_id = 'att_' + uuid.uuid4().hex
    directory = endpoint_dir(world, endpoint_id)
    directory.mkdir(parents=True)
    return endpoint_id, directory


def test_k18_2_orphan_is_reported_with_null_container_fields_and_never_served(world):
    world.configure(rpc=True)
    endpoint_id, directory = orphan(world)
    world.start()
    rows = listing(world)
    row, = [r for r in rows if r.get('endpoint_id') == endpoint_id]
    assert row['reason'] == 'orphan_endpoint'
    for field in ('container_id', 'name', 'labels', 'created_at'):
        assert row[field] is None, row
    status = world.rpc('scheduler_status')
    assert status.get('ok') is True, status
    assert status['result']['orphans'] == rows
    assert directory.is_dir(), 'discovery must not remove an orphan'
    refuses(directory / 'rpc.sock')
    world.restart(hard=True)
    assert any(r.get('endpoint_id') == endpoint_id for r in listing(world))
    refuses(directory / 'rpc.sock')


def test_k18_2_remove_orphan_endpoint_is_explicit_and_idempotent(world):
    world.configure(rpc=True)
    endpoint_id, directory = orphan(world)
    world.start()
    assert any(r.get('endpoint_id') == endpoint_id for r in listing(world))
    for _ in range(2):
        result = world.cli('scheduler', 'remove-orphan', endpoint_id, timeout=world.bound())
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value.get('ok') is True and value.get('state') == 'absent', value
        assert not directory.exists()
        assert not any(r.get('endpoint_id') == endpoint_id for r in listing(world))
        refuses(directory / 'rpc.sock')


def test_k18_2_remove_orphan_rejects_endpoint_id_prefix_without_deleting(world):
    world.configure(rpc=True)
    endpoint_id, directory = orphan(world)
    world.start()
    result = world.cli('scheduler', 'remove-orphan', endpoint_id[:-1], timeout=world.bound())
    assert result.returncode != 0
    value = json.loads(result.stdout)
    assert value.get('ok') is False and value.get('error') == 'invalid_id', value
    assert value.get('reason'), value
    assert directory.is_dir()
    assert any(r.get('endpoint_id') == endpoint_id for r in listing(world))


def test_k18_2_remove_orphan_refuses_recorded_running_endpoint(world):
    world.configure(rpc=True)
    world.start()
    node = world.launch('scoped_rpc', requests=[a.request()], hold=True)
    report = world.report(node)
    endpoint_id = world.runs(node)[-1]['attempt_id']
    path = a.host_endpoint(world, report['endpoint'])
    result = world.cli('scheduler', 'remove-orphan', endpoint_id, timeout=world.bound())
    assert result.returncode != 0
    value = json.loads(result.stdout)
    assert value.get('ok') is False and value.get('error') == 'not_orphan', value
    assert value.get('reason'), value
    with a.connect(path) as conn:
        assert a.exchange(conn, a.request()).get('ok') is True
    assert not any(r.get('endpoint_id') == endpoint_id for r in listing(world))
