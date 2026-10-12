"""ST2-R1..R4: applied filters, durable state, failure and network preservation."""
import concurrent.futures
import os
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import DockerWorld, WAIT_SECONDS, admits, mutations


@pytest.fixture
def world(tmp_path, monkeypatch):
    return DockerWorld(tmp_path, monkeypatch)


def started(world, allowlist=None):
    ex = world.executor(network='allowlist', egress_allowlist=allowlist or ['example.com'])
    assert ex.ensure_proxy().get('ok')
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')
    world.clear_calls()
    return ex


def test_st2_r1_equal_applied_digest_is_durable_and_leaves_proxy_untouched(world):
    ex = started(world)
    fresh = world.executor(network='allowlist', egress_allowlist=['example.com'])
    outcome = fresh.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert not mutations(world.calls(), ex.proxy_container), world.calls()


def test_st2_r1_absent_applied_digest_reconciles_existing_proxy(world):
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    directory = ex.write_proxy_config(world.paths.config / 'proxy' / ex.slug)
    world.put(ex.proxy_container, policy='no', mounts=[
        {'Type': 'bind', 'Source': str(directory / 'filter'), 'Destination': '/etc/tinyproxy/filter', 'RW': False},
        {'Type': 'bind', 'Source': str(directory / 'tinyproxy.conf'), 'Destination': '/etc/tinyproxy/tinyproxy.conf', 'RW': False},
    ], networks=[ex.network, 'bridge'])
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert mutations(world.calls(), ex.proxy_container), world.calls()
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')
    world.clear_calls()
    assert ex.ensure_proxy().get('ok')
    assert not mutations(world.calls(), ex.proxy_container), 'reconciliation must record its success durably'


def test_st2_r1_different_digest_loads_new_filter_for_new_connections(world):
    ex = started(world)
    ex.config['egress_allowlist'] = ['new.example.net']
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert mutations(world.calls(), ex.proxy_container), world.calls()
    applied = world.box(ex.proxy_container)['loaded_filter']
    assert admits(applied, 'new.example.net')
    assert not admits(applied, 'example.com')


def test_st2_r1_failed_reconciliation_refuses_launch_and_next_ensure_retries(world):
    ex = started(world)
    ex.config['egress_allowlist'] = ['new.example.net']
    # Both permitted strategies are failed together; do not require recreation
    # instead of restart, or vice versa.
    state = world.read()
    prefixes = [['run'], ['restart'], ['start']]
    state['failures'] = [{'prefix': p, 'reason': 'proxy reconciliation unavailable', 'target': ex.proxy_container} for p in prefixes]
    world.write(state)
    outcome = ex.ensure_proxy()
    assert not outcome.get('ok'), outcome
    assert 'proxy reconciliation unavailable' in str(outcome)
    world.recover()
    retry = ex.ensure_proxy()
    assert retry.get('ok'), retry
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    world.clear_calls()
    assert ex.ensure_proxy().get('ok')
    assert not mutations(world.calls(), ex.proxy_container)


def test_st2_r1_attachment_failure_never_records_success_and_remains_retryable(world):
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    world.fail(['network', 'connect'], reason='external network attachment refused', target=ex.proxy_container)
    outcome = ex.ensure_proxy()
    assert not outcome.get('ok'), outcome
    assert 'external network attachment refused' in str(outcome)
    world.recover()
    retry = ex.ensure_proxy()
    assert retry.get('ok'), retry
    assert {ex.network, 'bridge'} <= set(world.box(ex.proxy_container)['NetworkSettings']['Networks'])
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')
    world.clear_calls()
    assert ex.ensure_proxy().get('ok')
    assert not mutations(world.calls(), ex.proxy_container)


def test_st2_r1_concurrent_ensures_leave_one_applied_filter_and_both_succeed(world):
    ex = started(world)
    barrier = threading.Barrier(2)
    def ensure():
        fresh = world.executor(network='allowlist', egress_allowlist=['new.example.net'])
        barrier.wait(timeout=WAIT_SECONDS)
        return fresh.ensure_proxy()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ensure) for _ in range(2)]
        outcomes = [f.result(timeout=WAIT_SECONDS) for f in futures]
    assert all(o.get('ok') for o in outcomes), outcomes
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    assert not admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')


def test_st2_r1_ensures_from_independent_processes_are_serialised(world):
    ex = started(world)
    program = '''import json, sys
from pathlib import Path
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths
paths = ProjectPaths(Path(sys.argv[1]))
executor = DockerExecutor({'image': 'img', 'network': 'allowlist', 'mount_cli_from_host': False, 'egress_allowlist': ['new.example.net']}, paths, {}, paths.config)
sys.stdin.read(1)
print(json.dumps(executor.ensure_proxy()))
'''
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, '-c', program, str(world.root)],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for child in children:
            child.stdin.write('x')
            child.stdin.flush()
        results = [child.communicate(timeout=WAIT_SECONDS) for child in children]
        assert all(child.returncode == 0 for child in children), results
        outcomes = [json.loads(stdout) for stdout, stderr in results]
        assert all(o.get('ok') for o in outcomes), outcomes
        assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=WAIT_SECONDS)


def test_st2_r1_workspace_ensure_applies_allowlist_change_before_admitting_launch(world):
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    assert ex.ensure_running().get('ok')
    ex.config['egress_allowlist'] = ['new.example.net']
    outcome = ex.ensure_running()
    assert outcome.get('ok'), outcome
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    assert not admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')


def test_st2_r2_only_egress_changes_and_all_network_attachments_survive(world):
    ex = started(world)
    world.put(ex.container, policy='no', sources=[world.root])
    world.put(ex.auth_container, policy='no', sources=[world.root])
    state = world.read()
    state['containers'][ex.proxy_container]['NetworkSettings']['Networks']['another-project-network'] = {}
    world.write(state)
    workspace_before = world.box(ex.container)
    auth_before = world.box(ex.auth_container)
    ex.config['egress_allowlist'] = ['new.example.net']
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    assert set(world.box(ex.proxy_container)['NetworkSettings']['Networks']) == {ex.network, 'bridge', 'another-project-network'}
    assert world.box(ex.container) == workspace_before
    assert world.box(ex.auth_container) == auth_before
    assert not [a for a in world.calls() if ex.container in a or ex.auth_container in a], world.calls()


@pytest.mark.parametrize('style', ['replace-inode', 'rewrite-in-place'])
def test_st2_r3_filter_refresh_handles_both_file_write_styles(world, style):
    ex = started(world)
    mounted = next(m for m in world.box(ex.proxy_container)['Mounts'] if m['Destination'] == '/etc/tinyproxy/filter')
    filter_path = Path(mounted['Source'])
    original_inode = filter_path.stat().st_ino
    ex.config['egress_allowlist'] = ['new.example.net']
    generated = ex.write_proxy_config(world.base / 'regenerated') / 'filter'
    if style == 'replace-inode':
        replacement = filter_path.with_suffix('.new')
        replacement.write_bytes(generated.read_bytes())
        os.replace(replacement, filter_path)
        assert filter_path.stat().st_ino != original_inode
    else:
        filter_path.write_bytes(generated.read_bytes())
        assert filter_path.stat().st_ino == original_inode
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert admits(world.box(ex.proxy_container)['loaded_filter'], 'new.example.net')
    assert not admits(world.box(ex.proxy_container)['loaded_filter'], 'example.com')


def test_st2_r4_reconciliation_drops_refused_entries_and_never_widens_configuration(world):
    ex = started(world)
    ex.config['egress_allowlist'] = ['new.example.net', 'com', '1.1']
    before = list(ex.config['egress_allowlist'])
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    applied = world.box(ex.proxy_container)['loaded_filter']
    assert admits(applied, 'new.example.net')
    assert not admits(applied, 'arbitrary.com')
    assert not admits(applied, '192.168.1.1')
    assert ex.config['egress_allowlist'] == before
