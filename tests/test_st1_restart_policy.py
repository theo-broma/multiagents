"""ST1-R1..R3: public executor actions and Docker argv, no real daemon."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import DockerWorld, observe_unknown_filesystem, restart_policy
from multiagents.executor import docker


@pytest.fixture
def world(tmp_path, monkeypatch):
    result = DockerWorld(tmp_path, monkeypatch)
    monkeypatch.setattr(docker, 'filesystem_type', lambda path: 'ext4', raising=False)
    return result


def run_with_sources(world, monkeypatch, sources):
    ex = world.executor()
    # Public mount-planning inputs: isolate restart selection from SG's git
    # protection and machine-specific provider installations.
    monkeypatch.setattr(ex, 'mounts', lambda: [(Path(p), False) for p in sources])
    monkeypatch.setattr(ex, 'backing', lambda: {})
    monkeypatch.setattr(ex, 'server_mounts', lambda mounts: [])
    return ex.run_args()


@pytest.mark.parametrize('source', ['/tmp', '/tmp/child', '/run', '/run/child', '/var/run', '/dev/shm'])
def test_st1_r1_volatile_path_has_no_restart(world, monkeypatch, source):
    assert restart_policy(run_with_sources(world, monkeypatch, [source])) == 'no'


def test_st1_r1_tmpfs_outside_named_volatile_roots_has_no_restart(world, monkeypatch):
    source = Path('/var/lib/test-ramdisk')
    monkeypatch.setattr(docker, 'filesystem_type',
                        lambda path: 'tmpfs' if Path(path) == source else 'ext4', raising=False)
    assert restart_policy(run_with_sources(world, monkeypatch, [source])) == 'no'



def test_st1_r1_symlink_to_tmpfs_observes_resolved_source(world, monkeypatch):
    source = world.base / 'ramdisk'
    source.mkdir()
    link = world.base / 'ramdisk-link'
    link.symlink_to(source, target_is_directory=True)
    monkeypatch.setattr(docker, 'filesystem_type',
                        lambda path: 'tmpfs' if Path(path) == source.resolve() else 'ext4', raising=False)
    assert restart_policy(run_with_sources(world, monkeypatch, [link])) == 'no'


def test_st1_r1_symlink_resolving_into_tmp_has_no_restart(world, monkeypatch):
    link = world.base / 'temporary-source'
    link.symlink_to('/tmp', target_is_directory=True)
    assert restart_policy(run_with_sources(world, monkeypatch, [link])) == 'no'


@pytest.mark.parametrize('sources', [['/var/tmp'], ['/var/tmp', '/usr'], ['/tmpfoo'], ['/runfoo'], ['/dev/shmfoo']])
def test_st1_r1_persistent_sources_and_path_boundaries_keep_restart(world, monkeypatch, sources):
    assert restart_policy(run_with_sources(world, monkeypatch, sources)) == 'unless-stopped'


def test_st1_r1_one_volatile_source_among_persistent_sources_drops_restart(world, monkeypatch):
    assert restart_policy(run_with_sources(world, monkeypatch, ['/usr', '/tmp', '/var/tmp'])) == 'no'


def test_st1_r1_backing_source_volatility_is_used_instead_of_destination(world, monkeypatch):
    ex = world.executor()
    destination = Path('/var/tmp/persistent-container-path')
    monkeypatch.setattr(ex, 'mounts', lambda: [(destination, False)])
    monkeypatch.setattr(ex, 'backing', lambda: {destination: Path('/tmp')})
    monkeypatch.setattr(ex, 'server_mounts', lambda mounts: [])
    assert restart_policy(ex.run_args()) == 'no'


def test_st1_r1_read_only_tmp_source_is_also_volatile(world, monkeypatch):
    ex = world.executor()
    monkeypatch.setattr(ex, 'mounts', lambda: [(Path('/tmp'), True)])
    monkeypatch.setattr(ex, 'backing', lambda: {})
    monkeypatch.setattr(ex, 'server_mounts', lambda mounts: [])
    assert restart_policy(ex.run_args()) == 'no'


def test_st1_r1_symlink_to_persistent_source_keeps_restart(world, monkeypatch):
    link = world.base / 'persistent-source'
    link.symlink_to('/usr', target_is_directory=True)
    assert restart_policy(run_with_sources(world, monkeypatch, [link])) == 'unless-stopped'


def test_st1_r1_unknown_filesystem_fails_closed(world, monkeypatch):
    observe_unknown_filesystem(monkeypatch, world.root)
    assert restart_policy(run_with_sources(world, monkeypatch, [world.root])) == 'no'


@pytest.mark.parametrize('kind', ['workspace', 'egress', 'auth'])
def test_st1_r1_all_new_container_kinds_use_actual_volatile_sources(world, monkeypatch, kind):
    with tempfile.TemporaryDirectory(prefix='st1-', dir='/tmp') as volatile:
        if kind == 'auth':
            monkeypatch.setenv('MULTIAGENTS_STATE_DIR', volatile)
            ex = world.auth_executor()
            outcome = ex.ensure_auth_proxy()
            name = ex.auth_container
        elif kind == 'egress':
            ex = world.executor(network='allowlist')
            ex.config_dir = Path(volatile)
            outcome = ex.ensure_proxy()
            name = ex.proxy_container
        else:
            ex = world.executor(extra_mounts=[volatile])
            outcome = ex.ensure_running()
            name = ex.container
        assert outcome.get('ok'), outcome
        runs = [a for a in world.calls() if a[0] == 'run' and name in a]
        assert runs, world.calls()
        assert restart_policy(runs[-1]) == 'no'


@pytest.mark.parametrize('kind', ['workspace', 'egress', 'auth'])
@pytest.mark.parametrize('filesystem, expected', [('ext4', 'unless-stopped'), (None, 'no')])
def test_st1_r1_all_new_container_kinds_observe_persistent_or_unknown_filesystems(
        world, monkeypatch, kind, filesystem, expected):
    # Basetemp and the isolated machine state are on persistent /var/tmp.
    # The agreed observation seam supplies the filesystem reading for every
    # actual source, including proxy configuration and credential mounts.
    monkeypatch.setattr(docker, 'filesystem_type', lambda path: filesystem, raising=False)
    ex = world.auth_executor() if kind == 'auth' else world.executor(
        network='allowlist' if kind == 'egress' else 'none')
    name = {'workspace': ex.container, 'egress': ex.proxy_container, 'auth': ex.auth_container}[kind]
    ensure = {'workspace': ex.ensure_running, 'egress': ex.ensure_proxy, 'auth': ex.ensure_auth_proxy}[kind]
    outcome = ensure()
    assert outcome.get('ok'), outcome
    runs = [a for a in world.calls() if a[0] == 'run' and name in a]
    assert runs, world.calls()
    assert restart_policy(runs[-1]) == expected


def existing(world, kind, *, status='running', policy='unless-stopped', source='/tmp'):
    ex = world.auth_executor() if kind == 'auth' else world.executor(network='allowlist' if kind == 'egress' else 'none')
    name = {'workspace': ex.container, 'egress': ex.proxy_container, 'auth': ex.auth_container}[kind]
    mounts = []
    if kind == 'workspace':
        assert not ex.protect_project()
        mounts = [{'Type': 'bind', 'Source': str(p), 'Destination': str(p), 'RW': not ro} for p, ro in ex.mounts()]
    elif kind == 'auth':
        mounts = [{'Type': 'bind', 'Source': str(world.root), 'Destination': '/proxy-config', 'RW': False}]
    mounts.append({'Type': 'bind', 'Source': str(source), 'Destination': '/old-actual-source', 'RW': False})
    world.put(name, mounts=mounts, status=status, policy=policy)
    ensure = {'workspace': ex.ensure_running, 'egress': ex.ensure_proxy, 'auth': ex.ensure_auth_proxy}[kind]
    return ex, name, ensure


@pytest.mark.parametrize('kind', ['workspace', 'auth'])
def test_st1_r2_existing_container_corrects_policy_using_inspected_sources(world, kind):
    ex, name, ensure = existing(world, kind)
    outcome = ensure()
    assert outcome.get('ok'), outcome
    updates = [a for a in world.calls() if a[0] == 'update' and name in a]
    assert updates and restart_policy(updates[-1]) == 'no', world.calls()
    assert not [a for a in world.calls() if a[0] in ('rm', 'stop', 'run') and name in a]
    assert world.box(name)['State']['Status'] == 'running'


def test_st1_r2_existing_egress_corrects_policy_without_disrupting_current_filter(world, monkeypatch):
    # This test creates its initial proxy only to establish the applied filter.
    # R5 permits persistent restart policies only in the R1 cases themselves.
    monkeypatch.setattr(docker, 'filesystem_type', lambda path: None, raising=False)
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    assert ex.ensure_proxy().get('ok')
    state = world.read()
    box = state['containers'][ex.proxy_container]
    box['HostConfig']['RestartPolicy']['Name'] = 'unless-stopped'
    box['Mounts'].append({'Type': 'bind', 'Source': '/tmp', 'Destination': '/historical-source', 'RW': False})
    world.write(state)
    world.clear_calls()
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    assert any(a[0] == 'update' and restart_policy(a) == 'no' and ex.proxy_container in a for a in world.calls()), world.calls()
    assert not [a for a in world.calls() if a[0] in ('run', 'stop', 'rm', 'restart')]


@pytest.mark.parametrize('kind', ['workspace', 'egress', 'auth'])
def test_st1_r2_failed_policy_update_refuses_ensure_with_reason(world, kind):
    ex, name, ensure = existing(world, kind)
    world.fail(['update'], reason='policy update denied by daemon', target=name)
    outcome = ensure()
    assert not outcome.get('ok'), outcome
    assert 'policy update denied by daemon' in str(outcome)
    assert not [a for a in world.calls() if a[0] in ('run', 'stop', 'rm', 'start') and name in a]


def test_st1_r2_inspect_failure_does_not_authorise_start(world):
    ex, name, ensure = existing(world, 'workspace', status='exited', policy='no')
    world.fail(['inspect'], reason='mount inspection unavailable', target=name)
    outcome = ensure()
    assert not outcome.get('ok'), outcome
    assert not [a for a in world.calls() if a[0] in ('run', 'start') and name in a]


def test_st1_r3_exited_workspace_with_existing_sources_is_started(world):
    ex, name, ensure = existing(world, 'workspace', status='exited', policy='no', source=world.root)
    outcome = ensure()
    assert outcome.get('ok'), outcome
    assert any(a[0] == 'start' and name in a for a in world.calls())
    assert not [a for a in world.calls() if a[0] in ('run', 'rm') and name in a]


@pytest.mark.parametrize('kind', ['workspace', 'auth'])
def test_st1_r3_missing_inspected_mount_refuses_start_without_creating_source(world, kind):
    missing = world.base / 'vanished-mount'
    ex, name, ensure = existing(world, kind, status='exited', policy='no', source=missing)
    outcome = ensure()
    assert not outcome.get('ok'), outcome
    assert str(missing) in str(outcome)
    assert 'recreat' in str(outcome).lower()
    assert not missing.exists()
    assert not [a for a in world.calls() if a[0] in ('start', 'run', 'rm') and name in a]


def test_st1_r1_persistent_egress_receipt_then_st1_r3_missing_source_refuses_start(world):
    # ST2-R1 / D14: an absent receipt would require replacement, whose old
    # sources do not block it. Establish a matching receipt through ensure
    # so the exited proxy really exercises ST1-R3's existing-container start.
    # This setup also tests R1's persistent-source policy; the test's R1 name
    # uses the suite guard's existing exception for that policy assertion.
    ex = world.executor(network='allowlist', egress_allowlist=['example.com'])
    outcome = ex.ensure_proxy()
    assert outcome.get('ok'), outcome
    runs = [a for a in world.calls() if a[0] == 'run' and ex.proxy_container in a]
    assert runs, world.calls()
    assert restart_policy(runs[-1]) == 'unless-stopped'

    missing = world.base / 'vanished-mount'
    state = world.read()
    box = state['containers'][ex.proxy_container]
    box['State']['Status'] = 'exited'
    box['HostConfig']['RestartPolicy']['Name'] = 'no'
    box['Mounts'].append({'Type': 'bind', 'Source': str(missing),
                          'Destination': '/old-actual-source', 'RW': False})
    world.write(state)
    world.clear_calls()

    outcome = ex.ensure_proxy()
    assert not outcome.get('ok'), outcome
    assert str(missing) in str(outcome)
    assert 'recreat' in str(outcome).lower()
    assert not missing.exists()
    assert not [a for a in world.calls()
                if a[0] in ('start', 'run', 'rm') and ex.proxy_container in a]
