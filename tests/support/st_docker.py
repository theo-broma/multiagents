"""Black-box Docker daemon double for ST1/ST2/ST9.

The executable on PATH speaks Docker's public inspect/run/update/network
surface. No multiagents subprocess helper or private method is patched.
Its state and argv journal are external observations of that fake daemon.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths
from multiagents.providers import load_providers

WAIT_SECONDS = 5

DAEMON = r'''
import fcntl, json, os, re, sys
from pathlib import Path
a = sys.argv[1:]
base = Path(os.environ['ST_DOCKER_DIR'])
with (base / 'lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with (base / 'argv.jsonl').open('a') as log:
        log.write(json.dumps(a) + '\n')
    state = json.loads((base / 'state.json').read_text())
    boxes = state['containers']
    def save():
        (base / 'state.json').write_text(json.dumps(state))
    def option(flag, default=None):
        return a[a.index(flag) + 1] if flag in a else default
    for failure in state.get('failures', []):
        if a[:len(failure['prefix'])] == failure['prefix'] and (not failure.get('target') or failure['target'] in a):
            print(failure['reason'], file=sys.stderr)
            sys.exit(1)
    def load_filter(box):
        for m in box['Mounts']:
            if m['Destination'] == '/etc/tinyproxy/filter':
                box['loaded_filter'] = Path(m['Source']).read_text()
    if not a:
        sys.exit(1)
    if a[0] == 'info':
        print('28.0.0'); sys.exit(0)
    if a[:2] == ['image', 'inspect']:
        print('[{}]'); sys.exit(0)
    if a[:2] == ['network', 'inspect']:
        print('[{}]'); sys.exit(0)
    if a[:2] == ['network', 'connect']:
        box = boxes[a[-1]]
        box['NetworkSettings']['Networks'][a[-2]] = {}
        save(); sys.exit(0)
    if a[0] == 'inspect':
        fmt = option('-f', option('--format'))
        if not fmt:
            names = a[1:]
            if any(name not in boxes for name in names):
                print('No such container', file=sys.stderr); sys.exit(1)
            print(json.dumps([boxes[name] for name in names])); sys.exit(0)
        name = a[-1]
        if name not in boxes:
            print('No such container: ' + name, file=sys.stderr); sys.exit(1)
        box = boxes[name]
        fmt = option('-f', option('--format'))
        if not fmt:
            print(json.dumps([box])); sys.exit(0)
        if '.State.Status' in fmt:
            print(box['State']['Status'])
        elif 'range .Mounts' in fmt:
            for m in box['Mounts']:
                if '.RW' in fmt:
                    print(m['Destination'] + ':' + str(m['RW']).lower())
                elif '.Source' in fmt and '.Destination' not in fmt:
                    print(m['Source'])
                else:
                    print(m['Destination'])
        elif '.Mounts' in fmt:
            print(json.dumps(box['Mounts']))
        elif '.HostConfig.RestartPolicy.Name' in fmt:
            print(box['HostConfig']['RestartPolicy']['Name'])
        elif '.HostConfig.RestartPolicy' in fmt:
            print(json.dumps(box['HostConfig']['RestartPolicy']))
        elif '.Config.Labels' in fmt:
            keys = re.findall(r'"([^"]+)"', fmt)
            if keys:
                print(box['Config']['Labels'].get(keys[-1], '<no value>'))
            else:
                print(json.dumps(box['Config']['Labels']))
        elif '.NetworkSettings.Networks' in fmt:
            if 'range' in fmt:
                print('\n'.join(box['NetworkSettings']['Networks']))
            else:
                print(json.dumps(box['NetworkSettings']['Networks']))
        else:
            print(json.dumps(box))
        sys.exit(0)
    if a[0] == 'ps':
        fmt = option('--format', '')
        for name, box in boxes.items():
            labels = box['Config']['Labels']
            filters = [a[i+1] for i, v in enumerate(a[:-1]) if v in ('--filter', '-f')]
            if any(f.startswith('label=') and f[6:].split('=')[0] not in labels for f in filters):
                continue
            if 'json' in fmt:
                print(json.dumps({'ID': name, 'Names': name, 'Labels': ','.join(k+'='+v for k,v in labels.items())}))
            else:
                print(name)
        sys.exit(0)
    if a[0] == 'run':
        name = option('--name')
        if name in boxes:
            print('Conflict: container name already in use', file=sys.stderr); sys.exit(1)
        mounts, labels = [], {}
        for i, token in enumerate(a[:-1]):
            if token in ('-v', '--volume'):
                parts = a[i+1].split(':')
                mounts.append({'Type': 'bind', 'Source': parts[0], 'Destination': parts[1], 'RW': 'ro' not in parts[2:]})
            elif token == '--mount':
                items = dict(p.split('=', 1) for p in a[i+1].split(',') if '=' in p)
                mounts.append({'Type': items.get('type', 'bind'), 'Source': items.get('source', items.get('src')), 'Destination': items.get('target', items.get('dst')), 'RW': 'readonly' not in a[i+1]})
            elif token in ('--label', '-l'):
                k, _, v = a[i+1].partition('='); labels[k] = v
        box = {'Id': name, 'Name': '/' + name, 'State': {'Status': 'running'},
               'HostConfig': {'RestartPolicy': {'Name': option('--restart', 'no'), 'MaximumRetryCount': 0}},
               'Config': {'Labels': labels}, 'Mounts': mounts,
               'NetworkSettings': {'Networks': {option('--network', 'bridge'): {}}}}
        load_filter(box)
        boxes[name] = box; save(); print(name); sys.exit(0)
    if a[0] == 'update':
        box = boxes[a[-1]]
        box['HostConfig']['RestartPolicy']['Name'] = option('--restart', box['HostConfig']['RestartPolicy']['Name'])
        save(); print(a[-1]); sys.exit(0)
    if a[0] in ('start', 'restart', 'stop', 'rm'):
        name = a[-1]
        if a[0] == 'rm':
            boxes.pop(name, None)
        elif name in boxes:
            boxes[name]['State']['Status'] = 'exited' if a[0] == 'stop' else 'running'
            if a[0] != 'stop': load_filter(boxes[name])
        save(); print(name); sys.exit(0)
    print('unsupported fake Docker command: ' + repr(a), file=sys.stderr)
    sys.exit(2)
'''


class DockerWorld:
    def __init__(self, base: Path, monkeypatch):
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.daemon = base / 'daemon'
        self.daemon.mkdir()
        self.state_file = self.daemon / 'state.json'
        self.state_file.write_text(json.dumps({'containers': {}, 'failures': []}))
        binary = self.daemon / 'docker'
        binary.write_text(f'#!{sys.executable}\n' + DAEMON)
        binary.chmod(0o755)
        monkeypatch.setenv('ST_DOCKER_DIR', str(self.daemon))
        from multiagents.executor import docker
        monkeypatch.setattr(docker, 'filesystem_type', lambda path: None, raising=False)
        site = str(Path(__file__).parent / 'st_site')
        monkeypatch.setenv('PYTHONPATH', site + os.pathsep + os.environ.get('PYTHONPATH', ''))
        guard = os.environ.get('ST_TEST_DOCKER_GUARD')
        search_path = str(self.daemon) + os.pathsep + os.environ.get('PATH', '')
        if guard:
            search_path = guard + os.pathsep + search_path
        monkeypatch.setenv('PATH', search_path)
        self.root = base / 'project'
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        identity = {**os.environ, 'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'f@example.invalid',
                    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'f@example.invalid'}
        for args in (['init', '-q', '-b', 'main'], ['commit', '-q', '--allow-empty', '-m', 'fixture']):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True, env=identity)

    def executor(self, **config):
        defaults = {'image': 'img', 'network': 'none', 'mount_cli_from_host': False}
        defaults.update(config)
        return DockerExecutor(defaults, self.paths, {}, self.paths.config)

    def auth_executor(self, **config):
        ex = self.executor(auth_proxy=True, network='allowlist', **config)
        ex.providers = load_providers({'claude': {'bin': 'not-installed', 'container_private_home': ['.claude']}})
        vault = ex.vault_state('claude')['claude']
        vault.mkdir(parents=True, exist_ok=True)
        return ex

    def read(self):
        return json.loads(self.state_file.read_text())

    def write(self, state):
        self.state_file.write_text(json.dumps(state))

    def calls(self):
        log = self.daemon / 'argv.jsonl'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def clear_calls(self):
        (self.daemon / 'argv.jsonl').write_text('')

    def put(self, name, *, sources=(), mounts=None, status='running', policy='unless-stopped', labels=None, networks=('bridge',)):
        state = self.read()
        state['containers'][name] = {
            'Id': name, 'Name': '/' + name, 'State': {'Status': status},
            'HostConfig': {'RestartPolicy': {'Name': policy, 'MaximumRetryCount': 0}},
            'Config': {'Labels': labels or {}},
            'Mounts': mounts if mounts is not None else [{'Type': 'bind', 'Source': str(p), 'Destination': str(p), 'RW': True} for p in sources],
            'NetworkSettings': {'Networks': {n: {} for n in networks}},
        }
        self.write(state)

    def fail(self, prefix, reason='fixture Docker failure', target=None):
        state = self.read()
        state['failures'] = [{'prefix': prefix, 'reason': reason, 'target': target}]
        self.write(state)

    def recover(self):
        state = self.read()
        state['failures'] = []
        self.write(state)

    def box(self, name):
        return self.read()['containers'][name]


def restart_policy(argv):
    if '--restart' in argv:
        return argv[argv.index('--restart') + 1]
    return next((s.split('=', 1)[1] for s in argv if s.startswith('--restart=')), 'no')


def admits(filter_text, host):
    return any(re.search(line, host, re.I) for line in filter_text.splitlines() if line.strip())


def mutations(calls, name):
    return [a for a in calls if a and a[0] in ('run', 'rm', 'stop', 'start', 'restart') and name in a]


# These are test-infrastructure public surfaces, settled by ST Decision 9.
from st_container_guard import owner_labels, containers_to_remove, session_reap


def observe_unknown_filesystem(monkeypatch, source):
    from multiagents.executor import docker
    # raising=False allows the pre-implementation branch to fail on the
    # restart-policy assertion instead of failing while installing the seam.
    resolved = Path(source).resolve()
    monkeypatch.setattr(docker, 'filesystem_type',
                        lambda path: None if Path(path) == resolved else 'ext4', raising=False)


def reap_test_containers(world):
    session_reap(str(world.daemon / 'docker'))
