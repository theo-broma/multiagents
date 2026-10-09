"""SL-R7/R9: shutdown also owns children created by a TERM handler."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from multiagents import procs
from multiagents.scheduler import owner


@pytest.mark.parametrize("mode", ["reap", "sweep", "worker"])
def test_sl_shutdown_reaps_a_descendant_created_after_term(tmp_path, mode):
    ready = tmp_path / "ready"
    child_file = tmp_path / "child.pid"
    lifetime = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = owner.mark_env(lifetime.pid, procs.start_time(lifetime.pid), tmp_path)
    script = '''import os, signal, subprocess, sys, time
from pathlib import Path
def stop(*_):
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
    Path(sys.argv[2]).write_text(str(child.pid))
    os._exit(0)
signal.signal(signal.SIGTERM, stop)
Path(sys.argv[1]).touch()
while True: time.sleep(.1)
'''
    if mode == "worker":
        script = f'''import subprocess, sys, time
from multiagents.scheduler.owner import _watch
with _watch(run=True):
    subprocess.Popen([sys.executable, '-c', {script!r}, *sys.argv[1:]])
    time.sleep(60)
'''
    parent = subprocess.Popen([sys.executable, "-c", script, str(ready), str(child_file)],
                              env=dict(owner.unmarked_env(), **marker))
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "the parent's TERM handler was not ready"
        if mode in {"sweep", "worker"}:
            lifetime.kill()
            lifetime.wait(timeout=2)
            if mode == "sweep":
                owner.sweep_dead_owners()
        else:
            owner.reap(lifetime.pid, tmp_path)
        parent.wait(timeout=10)
        assert child_file.exists(), "the TERM handler did not run"
        child = int(child_file.read_text())
        assert not procs.living(child), "a child forked during shutdown survived"
        assert owner.marked_processes(lifetime.pid, tmp_path) == set()
    finally:
        owner.reap(lifetime.pid, tmp_path)
        for process in (parent, lifetime):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


def test_sl_r7_blocked_worker_startup_reaps_an_adoptable_orphan(tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from multiagents.scheduler.engine import Engine

    ready = tmp_path / "ready"
    child_file = tmp_path / "child.pid"
    lifetime = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = owner.mark_env(lifetime.pid, procs.start_time(lifetime.pid), tmp_path)
    prior_script = '''import subprocess, sys
from pathlib import Path
from multiagents.scheduler.owner import _bind_run
_bind_run('adopted-run')
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
'''
    prior = subprocess.Popen([sys.executable, "-c", prior_script, str(child_file)],
                             env=dict(owner.unmarked_env(), **marker))
    worker = None
    try:
        prior.wait(timeout=5)
        child = int(child_file.read_text())
        assert procs.living(child), "there was no run left to adopt"
        blocked_script = '''import sys, time
from pathlib import Path
from multiagents.scheduler.owner import _watch
with _watch(run=True):
    Path(sys.argv[1]).touch()
    time.sleep(60)
'''
        engine = Engine.__new__(Engine)
        engine.store = SimpleNamespace(directory=tmp_path)
        engine.paths = SimpleNamespace(root=tmp_path)
        engine.stopped = Event()
        engine.children = []
        popen = subprocess.Popen

        def blocked_worker(argv, **kwargs):
            return popen([sys.executable, "-c", blocked_script, str(ready)], **kwargs)

        with monkeypatch.context() as patch:
            for key, value in marker.items():
                patch.setenv(key, value)
            patch.setattr("multiagents.scheduler.engine.subprocess.Popen", blocked_worker)
            engine.spawn({"attempt_id": "adoption", "run_id": "adopted-run"})
        worker = engine.children[0]
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "the replacement worker did not enter startup"
        lifetime.kill()
        lifetime.wait(timeout=2)
        worker.wait(timeout=10)
        assert not procs.living(child), "the adopted run outlived its dead owner"
    finally:
        owner.reap(lifetime.pid, tmp_path)
        for process in (worker, prior, lifetime):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)


@pytest.mark.parametrize("start", ["", "unreadable", "0", "-1"])
def test_sl_r9_unreadable_owner_start_gives_no_sweep_authority(tmp_path, monkeypatch, start):
    lifetime = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = owner.mark_env(lifetime.pid, procs.start_time(lifetime.pid), tmp_path)
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              env=dict(owner.unmarked_env(), **marker))
    stat = procs._stat

    def unreadable(pid):
        fields = stat(pid)
        if pid == lifetime.pid:
            fields = list(fields)
            fields[19] = start
        return fields

    try:
        with monkeypatch.context() as patch:
            patch.setattr(procs, "_stat", unreadable)
            owner.sweep_dead_owners()
        assert target.poll() is None, "an unreadable owner identity authorised a kill"
    finally:
        owner.reap(lifetime.pid, tmp_path)
        for process in (target, lifetime):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
