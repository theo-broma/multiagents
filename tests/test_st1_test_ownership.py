"""ST1-R4/R5: test infrastructure, observed through listings and subprocesses."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / 'support'))
from st_docker import DockerWorld, WAIT_SECONDS, owner_labels, reap_test_containers
from st_container_guard import OWNER_LABEL, containers_to_remove, process_start
import st_container_guard

NESTED_WAIT_SECONDS = 10


def listing(name, labels):
    return {'Id': name, 'Config': {'Labels': labels}}


@pytest.mark.parametrize('owner', ['dead', 'live', 'reused-pid', 'other-host', 'unlabelled'])
def test_st1_r4_session_reaper_only_removes_provably_dead_local_owners(tmp_path, monkeypatch, owner):
    world = DockerWorld(tmp_path, monkeypatch)
    host, pid = socket.gethostname(), os.getpid()
    started = process_start(pid)
    should_reap = owner in ('dead', 'reused-pid')
    if owner == 'dead':
        with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE) as child:
            pid, started = child.pid, process_start(child.pid)
            child.stdin.close()
            child.wait(timeout=WAIT_SECONDS)
    elif owner == 'reused-pid':
        started = str(int(started) - 1)
    elif owner == 'other-host':
        host += '-another-host'
    labels = {} if owner == 'unlabelled' else owner_labels(host, pid, started)
    name = 'multiagents-st1-owned-' + owner
    world.put(name, labels=labels, policy='no')
    assert containers_to_remove([world.box(name)]) == ([name] if should_reap else [])
    reap_test_containers(world)
    assert (name not in world.read()['containers']) == should_reap
    # Repetition must not turn a preserved owner into permission to remove it.
    reap_test_containers(world)
    assert (name not in world.read()['containers']) == should_reap


@pytest.mark.parametrize('label', [None, '', 'garbage', 'host:1', 'host:1:2:3',
                                   f'{socket.gethostname()}:0:1',
                                   f'{socket.gethostname()}:-1:1',
                                   f'{socket.gethostname()}:abc:1',
                                   f'{socket.gethostname()}:1:abc'])
def test_st1_r4_malformed_ownership_never_authorises_removal(label):
    assert containers_to_remove([listing('keep', {OWNER_LABEL: label})]) == []


def test_st1_r4_empty_listing_is_safe():
    assert containers_to_remove([]) == []


def test_st1_r4_unobservable_process_identity_never_authorises_removal(monkeypatch):
    def unreadable(pid):
        raise PermissionError('process identity cannot be observed')
    monkeypatch.setattr(st_container_guard, 'process_start', unreadable)
    labels = owner_labels(socket.gethostname(), os.getpid(), '1')
    assert containers_to_remove([listing('keep', labels)]) == []


@pytest.mark.parametrize('failed_command', [['ps'], ['inspect']])
def test_st1_r4_failed_daemon_observation_never_authorises_removal(tmp_path, monkeypatch, failed_command):
    world = DockerWorld(tmp_path, monkeypatch)
    labels = owner_labels(socket.gethostname(), os.getpid(), str(int(process_start(os.getpid())) - 1))
    world.put('keep', labels=labels, policy='no')
    world.fail(failed_command, reason='owner observation unavailable')
    reap_test_containers(world)
    assert 'keep' in world.read()['containers']
    assert not [a for a in world.calls() if a[:1] == ['rm']]


def nested_suite(tmp_path, world, st_docker_guard, source, *, real_double=False, workers=0,
                 isolate=False, live_opt_in=False):
    """Use the actual hooks and autouse fixture in a separate pytest controller."""
    project = tmp_path / 'nested-suite'
    project.mkdir()
    support = Path(__file__).parent.resolve() / 'support'
    (project / 'conftest.py').write_text(
        f'import sys\nsys.path.insert(0, {str(support)!r})\n'
        'from st_container_guard import (pytest_configure, pytest_configure_node, '
        'pytest_sessionstart, st_docker_guard, st_live_docker)\n'
        + ('from st_docker_isolation import st_docker_daemon_is_opt_in, st_unavailable_docker\n'
           if isolate else ''))
    (project / 'test_nested.py').write_text(source)
    env = dict(os.environ)
    # Do not feed this intentionally failing child into the parent's guard.
    entries = [p for p in env['PATH'].split(os.pathsep)
               if p not in (str(st_docker_guard.directory), str(world.daemon))]
    env['PATH'] = os.pathsep.join(([str(world.daemon)] if real_double else []) + entries)
    env.pop('ST_TEST_DOCKER_GUARD', None)
    env.pop('ST_TEST_DOCKER_LIVE', None)
    env.pop('SG_TEST_DOCKER', None)
    if live_opt_in:
        env['SG_TEST_DOCKER'] = '1'
    if real_double:
        env['ST_TEST_DOCKER_LIVE'] = '1'
    else:
        env.pop('SG_TEST_DOCKER', None)
    args = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
            '--basetemp', str(tmp_path / 'nested-temp')]
    if workers:
        args += ['-n', str(workers)]
    with subprocess.Popen(args, cwd=project, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        started = process_start(child.pid)
        try:
            stdout, stderr = child.communicate(timeout=NESTED_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=WAIT_SECONDS)
            raise
    return child.returncode, stdout + stderr, child.pid, started


@pytest.mark.parametrize('opt_in, live_fixture', [(False, False), (False, True),
                                                (True, False), (True, True)])
def test_st1_r4_real_docker_requires_opt_in_and_live_fixture(
        tmp_path, monkeypatch, st_docker_guard, opt_in, live_fixture):
    """A recording host-Docker substitute must see no ordinary-test calls.

    Exercise a direct binary call and a child Python process, not an executor
    mock. Only the explicit live case may reach the substitute daemon.
    """
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    expected = 0 if opt_in and live_fixture else 127
    fixture = 'st_live_docker' if live_fixture else ''
    source = f'''import subprocess, sys

def test_docker_access({fixture}):
    assert subprocess.run(['docker', 'info'], capture_output=True).returncode == {expected}
    child = "import subprocess, sys; sys.exit(subprocess.run(['docker', 'info'], capture_output=True).returncode)"
    assert subprocess.run([sys.executable, '-c', child], capture_output=True).returncode == {expected}
'''
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard, source,
        real_double=True, isolate=True, live_opt_in=opt_in)
    assert result == 0, output
    # Session-start owner reaping is independent of test opt-in. Empty ps
    # listings grant no authority to remove anything; test calls are separate.
    calls = [a for a in world.calls() if a[:1] != ['ps']]
    assert calls == ([['info'], ['info']] if expected == 0 else [])


def test_st1_r5_ordinary_fallback_preserves_strict_guard(tmp_path, monkeypatch, st_docker_guard):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    source = '''import subprocess

def test_forbidden():
    subprocess.run(['docker', 'run', '--restart=always', 'img'], capture_output=True)
'''
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard, source,
                                      real_double=True, isolate=True)
    assert result == 1, output
    assert 'ST1-R5 forbidden Docker restart policy' in output, output
    assert 'test_nested.py::test_forbidden (call)' in output, output
    assert not [a for a in world.calls() if a[:1] == ['run']]


def test_st1_r4_ps_context_and_core_inspection_tests_never_reach_host_docker(
        tmp_path, monkeypatch, st_docker_guard):
    """Run the original assertions with an available recording host binary.

    SG_TEST_DOCKER is absent in the child. The PS daemon seam and the suite's
    fallback must prevent every test call from reaching the host substitute.
    """
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    tests = str(Path(__file__).parent.resolve())
    source = f'''import sys
sys.path.insert(0, {tests!r})
from test_ps_review_fixes import (
    unavailable_docker,
    test_1_a_matching_context_recovers,
    test_1_a_block_from_the_container_context_survives_a_passing_host_check,
)
from test_core import (
    test_docker_refuses_to_mount_the_socket,
    test_a_container_that_cannot_take_the_new_mount_says_so,
)
'''
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard, source,
                                      real_double=True, isolate=True)
    assert result == 0, output
    assert '4 passed' in output, output
    assert not [a for a in world.calls() if a[:1] != ['ps']]


def subprocess_test(world, name, options):
    return f'''import os, subprocess

def {name}(st_docker_guard, monkeypatch):
    monkeypatch.setenv('PATH', str(st_docker_guard.directory) + os.pathsep + {str(world.daemon)!r} + os.pathsep + os.environ['PATH'])
    subprocess.run({['docker', 'run', '-d', '--name', 'multiagents-st1-guard', *options, 'img']!r}, check=True)
'''


@pytest.mark.parametrize('options', [[], ['--restart', 'no'], ['--restart=no'], ['--restart=always', '--restart', 'no']])
def test_st1_r5_guard_forwards_safe_subprocess_runs(tmp_path, monkeypatch, st_docker_guard, options):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard,
                                       subprocess_test(world, 'test_safe', options))
    assert result == 0, output
    assert 'multiagents-st1-guard' in world.read()['containers'], 'shim must forward'


@pytest.mark.parametrize('options', [['--restart', 'unless-stopped'], ['--restart=always'],
                                     ['--restart', 'on-failure:3'], ['--restart', 'no', '--restart=always']])
def test_st1_r5_suite_guard_rejects_non_no_restart_from_subprocess(tmp_path, monkeypatch, st_docker_guard, options):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard,
                                       subprocess_test(world, 'test_forbidden', options))
    assert result == 1, output
    assert 'ST1-R5 forbidden Docker restart policy' in output, output
    assert 'test_nested.py::test_forbidden (call)' in output, output
    assert 'multiagents-st1-guard' in world.read()['containers'], 'record and forward, then fail session'


def test_st1_r5_guard_allows_only_r1_cases_under_test(tmp_path, monkeypatch, st_docker_guard):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard,
        subprocess_test(world, 'test_st1_r1_persistent', ['--restart', 'unless-stopped']))
    assert result == 0, output


def test_st1_r4_later_pytest_session_reaps_dead_labels_only(tmp_path, monkeypatch, st_docker_guard):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    live = owner_labels(socket.gethostname(), os.getpid(), process_start(os.getpid()))
    reused = owner_labels(socket.gethostname(), os.getpid(), str(int(process_start(os.getpid())) - 1))
    other = owner_labels(socket.gethostname() + '-elsewhere', os.getpid(), '1')
    for name, labels in [('reap', reused), ('live', live), ('other', other), ('unlabelled', {})]:
        world.put(name, labels=labels, policy='no')
    result, output, _, _ = nested_suite(tmp_path, world, st_docker_guard,
                                       'def test_session():\n    assert True\n', real_double=True)
    assert result == 0, output
    assert set(world.read()['containers']) == {'live', 'other', 'unlabelled'}


def test_st1_r4_live_forwarding_labels_xdist_controller_and_sets_no_restart(tmp_path, monkeypatch, st_docker_guard):
    world = DockerWorld(tmp_path / 'world', monkeypatch)
    source = '''import os, subprocess
import pytest

@pytest.mark.parametrize('number', range(2))
def test_live(number):
    subprocess.run(['docker', 'run', '-d', '--restart=unless-stopped', '--name', 'live-' + str(number), 'img'], check=True)
'''
    result, output, pid, started = nested_suite(tmp_path, world, st_docker_guard,
                                               source, real_double=True, workers=2)
    assert result == 0, output
    boxes = world.read()['containers']
    assert set(boxes) == {'live-0', 'live-1'}
    expected = owner_labels(socket.gethostname(), pid, started)
    for box in boxes.values():
        assert box['HostConfig']['RestartPolicy']['Name'] == 'no'
        assert expected.items() <= box['Config']['Labels'].items()
