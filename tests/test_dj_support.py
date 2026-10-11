"""Black-box DJ harness; JSON listing envelopes are not prescribed.

Decision 11 fixtures are covered in test_dj_seams.py. Detached-only driver
eligibility and root restart delivery are exercised separately where the
existing driver harness admits public observable checks.

The sole slow green-path test is DJ-R3's mandated 10-second TERM grace. All
other waiting uses bounded polling/events. No feature module is stubbed.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from multiagents.paths import ProjectPaths

BOUND = 3.0
CLI_BOUND = 8.0
ROOT = Path(__file__).resolve().parents[1]


def eventually(read, predicate=bool):
    end = time.monotonic() + BOUND
    while True:
        value = read()
        if predicate(value):
            return value
        assert time.monotonic() < end, f"condition not observed within {BOUND}s: {value!r}"
        time.sleep(0.025)


def records(value):
    if isinstance(value, dict):
        if 'id' in value and 'state' in value:
            return [value]
        return [r for v in value.values() for r in records(v)]
    if isinstance(value, list):
        return [r for v in value for r in records(v)]
    return []


class Jobs:
    def __init__(self, tmp_path):
        self.paths = ProjectPaths(tmp_path / 'project')
        self.paths.ensure()
        self.env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        self.pids = set()
        self.pidfiles = []

    def cli(self, *args, input=None, env=None, timeout=CLI_BOUND):
        return subprocess.run([sys.executable, '-m', 'multiagents.cli',
                               '--path', str(self.paths.root), 'bg', *map(str, args)],
                              cwd=self.paths.root, env=env or self.env, input=input,
                              text=True, capture_output=True, timeout=timeout)

    def listing(self, owner=None):
        result = self.cli('list', '--json', *(['--owner', owner] if owner else []))
        assert result.returncode == 0, f"bg list failed: {result.stderr}"
        found = records(json.loads(result.stdout))
        self.pids.update(r['pid'] for r in found if isinstance(r.get('pid'), int))
        return found

    def run(self, code, *, owner='orchestrator', name='job', key=None):
        args = ['run', '--owner', owner, '--name', name]
        if key is not None:
            args += ['--key', key]
        result = self.cli(*args, '--', sys.executable, '-c', code)
        assert result.returncode == 0, f"bg run failed: {result.stderr}"
        found = [r for r in self.listing(owner) if r['name'] == name]
        assert len(found) == 1, found
        assert str(found[0]['id']) in result.stdout
        return found[0], result

    def held(self, ignore_term=False, **kw):
        pidfile = self.paths.root / f'pid-{len(self.pidfiles)}'
        self.pidfiles.append(pidfile)
        setup = "signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else ""
        job, result = self.run(f"import os,pathlib,signal; {setup}pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); signal.pause()", **kw)
        eventually(pidfile.exists)
        return job, result

    def ended(self, job):
        return eventually(lambda: next(r for r in self.listing() if r['id'] == job['id']),
                          lambda r: r['state'] not in ('starting', 'running'))

    def cleanup(self):
        for path in self.pidfiles:
            if path.exists():
                self.pids.add(int(path.read_text()))
        for pid in self.pids:
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def jobs(tmp_path):
    world = Jobs(tmp_path)
    try:
        yield world
    finally:
        world.cleanup()
