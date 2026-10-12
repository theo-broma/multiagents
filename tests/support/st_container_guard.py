"""ST1-R4/R5 test-only ownership, crash reaping and Docker argv guard."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

OWNER_LABEL = 'multiagents.test.owner'
WAIT_SECONDS = 5


def process_start(pid):
    """Linux proc field 22; comm may contain spaces and parentheses."""
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def owner_labels(host, pid, start_time):
    return {OWNER_LABEL: f'{host}:{pid}:{start_time}'}


def containers_to_remove(listing):
    """Accept Docker inspect objects; keep any owner we cannot prove dead."""
    remove = []
    for container in listing:
        label = (container.get('Config', {}).get('Labels') or {}).get(OWNER_LABEL)
        if not isinstance(label, str):
            continue
        try:
            host, pid_text, started = label.rsplit(':', 2)
            pid = int(pid_text)
            if host != socket.gethostname() or pid <= 0 or not started.isdecimal():
                continue
            try:
                current = process_start(pid)
            except FileNotFoundError:
                current = None
            if current != started:
                remove.append(container['Id'])
        except (ValueError, KeyError, IndexError, PermissionError, OSError):
            continue
    return remove


def session_reap(binary):
    """A later controller session sweeps only labelled, dead local owners."""
    if not binary:
        return
    try:
        listed = subprocess.run([binary, 'ps', '-aq', '--filter', f'label={OWNER_LABEL}'],
                                capture_output=True, text=True, timeout=WAIT_SECONDS)
        ids = listed.stdout.split() if listed.returncode == 0 else []
        if not ids:
            return
        inspected = subprocess.run([binary, 'inspect', *ids], capture_output=True,
                                   text=True, timeout=WAIT_SECONDS)
        if inspected.returncode:
            return
        for identity in containers_to_remove(json.loads(inspected.stdout)):
            removed = subprocess.run([binary, 'rm', '-f', identity], capture_output=True,
                                     text=True, timeout=WAIT_SECONDS)
            if removed.returncode and 'No such container' not in removed.stderr:
                raise RuntimeError(f'ST1-R4 could not reap {identity}: {removed.stderr}')
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        # An unavailable daemon gives no authority to remove anything.
        return


def restart_policy(argv):
    # Docker's repeated string flags take their last supplied value.
    policy = 'no'
    for index, value in enumerate(argv):
        if value == '--restart':
            policy = argv[index + 1] if index + 1 < len(argv) else '<missing>'
        elif value.startswith('--restart='):
            policy = value.partition('=')[2]
    return policy


def assert_safe_runs(records):
    violations = [r for r in records if r['argv'][:1] == ['run']
                  and restart_policy(r['argv']) != 'no' and not r.get('r1_case')]
    assert not violations, f'ST1-R5 forbidden Docker restart policy: {violations}'


SHIM = r'''
import fcntl, json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
self_dir = Path(__file__).parent.resolve()
search = os.pathsep.join(p for p in os.environ.get('PATH', '').split(os.pathsep)
                         if Path(p or '.').resolve() != self_dir)
target = shutil.which('docker', path=search)
# Live containers are test infrastructure: ownership and no restart are set
# here even when the application under test has not implemented ST1 yet.
if args[:1] == ['run'] and target and real_docker and os.environ.get('ST_TEST_DOCKER_LIVE') == '1' and Path(target).resolve() == Path(real_docker).resolve():
    args = ['run', '--restart', 'no', '--label', owner_label, *args[1:]]
    # Remove application-provided restart flags so Docker sees exactly one.
    clean, index = args[:5], 5
    while index < len(args):
        if args[index] == '--restart':
            index += 2
        elif args[index].startswith('--restart='):
            index += 1
        else:
            clean.append(args[index]); index += 1
    args = clean
test = os.environ.get('PYTEST_CURRENT_TEST', '')
r1 = test.split('::')[-1].startswith('test_st1_r1_')
with Path(journal).open('a') as log:
    fcntl.flock(log, fcntl.LOCK_EX)
    log.write(json.dumps({'argv': args, 'test': test, 'r1_case': r1}) + '\n')
if not target:
    print('docker is not installed', file=sys.stderr)
    sys.exit(127)
os.execv(target, [target, *args])
'''


class DockerGuard:
    def __init__(self, directory, owner, real_docker=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = self.directory / 'argv.jsonl'
        self.journal.touch()
        binary = self.directory / 'docker'
        binary.write_text(f'#!{sys.executable}\njournal = {str(self.journal)!r}\n'
                          f'owner_label = {(OWNER_LABEL + '=' + owner)!r}\nreal_docker = {real_docker!r}\n' + SHIM)
        binary.chmod(0o755)

    def records(self):
        return [json.loads(line) for line in self.journal.read_text().splitlines()]


def pytest_configure(config):
    if hasattr(config, 'workerinput'):
        config.st_docker_owner = config.workerinput['st_docker_owner']
    else:
        config.st_docker_owner = owner_labels(socket.gethostname(), os.getpid(),
                                            process_start(os.getpid()))[OWNER_LABEL]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput['st_docker_owner'] = node.config.st_docker_owner


def pytest_sessionstart(session):
    if not hasattr(session.config, 'workerinput'):
        session_reap(shutil.which('docker'))


@pytest.fixture(scope='session', autouse=True)
def st_docker_guard(tmp_path_factory, request):
    patch = pytest.MonkeyPatch()
    original_path = os.environ.get('PATH', '')
    guard = DockerGuard(tmp_path_factory.mktemp('st-docker-guard'),
                        request.config.st_docker_owner, shutil.which('docker'))
    patch.setenv('PATH', str(guard.directory) + os.pathsep + original_path)
    patch.setenv('ST_TEST_DOCKER_GUARD', str(guard.directory))
    try:
        yield guard
    finally:
        patch.undo()
        assert_safe_runs(guard.records())


@pytest.fixture
def st_live_docker(monkeypatch):
    """Explicit opt-in harness wiring; the guard records the effective argv."""
    monkeypatch.setenv('ST_TEST_DOCKER_LIVE', '1')
