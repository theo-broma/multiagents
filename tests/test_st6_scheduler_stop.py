"""ST6-R2: scheduler stop reports process identity, not record-file lifetime.

The CLI is real. A real scheduler is used for the slow engine.stop case;
other cases use processes with explicitly controlled signal behaviour and
the documented process identity record. All long time is virtual.
"""
from __future__ import annotations

import json
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.world import World  # noqa: E402

from multiagents import cli, procs, scheduler

READY_BOUND = 8
RETURN_BOUND = 2

PARKED_DAEMON = r'''
import json, os, signal, sys
from pathlib import Path
from multiagents.procs import start_time
record, mode = Path(sys.argv[1]), sys.argv[2]
def terminate(*args):
    if mode == "exit-retaining-record":
        raise SystemExit(0)
    if mode == "remove-record-but-live":
        record.unlink()
        print("record-removed", flush=True)
if mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, terminate)
record.write_text(json.dumps({"pid": os.getpid(), "pid_start": start_time(os.getpid())}))
print("ready", flush=True)
while True:
    signal.pause()
'''

SLOW_SCHEDULER = r'''
import sys
from unittest.mock import patch
from multiagents import scheduler
from multiagents.scheduler.engine import Engine
original = Engine.stop
def slow_stop(engine):
    print("engine-stopping", flush=True)
    # The test clock releases stdin after 12 seconds. This dependency seam
    # models a slow engine shutdown while the real daemon owns its record,
    # signal handlers, RPC lifecycle, and final cleanup.
    sys.stdin.buffer.read(1)
    original(engine)
with patch.object(Engine, "stop", slow_stop):
    scheduler.start(__import__("pathlib").Path(sys.argv[1]), foreground=True)
'''


def line(proc):
    readable, _, _ = select.select([proc.stdout], [], [], RETURN_BOUND)
    assert readable, "fixture daemon did not reach its signal/shutdown boundary"
    return proc.stdout.readline().decode().strip()


@contextmanager
def daemon(world, mode):
    record = world.paths.scheduler / "process.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    if mode == "slow-engine":
        command = [sys.executable, "-c", SLOW_SCHEDULER, str(world.root)]
    else:
        command = [sys.executable, "-c", PARKED_DAEMON, str(record), mode]
    proc = subprocess.Popen(command, cwd=world.root, env=world.daemon_env(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        if mode == "slow-engine":
            world.until(lambda: world.sock_answers(), timeout=READY_BOUND,
                        what="the real scheduler to answer")
        else:
            assert line(proc) == "ready"
        yield proc, record
    finally:
        if mode == "slow-engine" and proc.poll() is None:
            try:
                proc.stdin.write(b"x")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=RETURN_BOUND)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


class ShutdownClock:
    def __init__(self, proc, mode):
        self.proc, self.mode = proc, mode
        self.value = 0.0
        self.shutdown_seen = False
        self.released = False

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        if self.mode == "slow-engine":
            if not self.shutdown_seen:
                assert line(self.proc) == "engine-stopping"
                self.shutdown_seen = True
            self.value += seconds
            if self.value >= 12 and not self.released:
                self.proc.stdin.write(b"x")
                self.proc.stdin.flush()
                self.released = True
                self.proc.wait(timeout=RETURN_BOUND)
        elif self.mode == "exit-retaining-record":
            self.proc.wait(timeout=RETURN_BOUND)
            self.value += 1000
        elif self.mode == "remove-record-but-live":
            if not self.shutdown_seen:
                assert line(self.proc) == "record-removed"
                self.shutdown_seen = True
            self.value += 1000
        else:
            self.value += 1000

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.write_config()
    yield w
    w.close()


def stop(world, monkeypatch, capsys, proc, mode):
    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "time", ShutdownClock(proc, mode))
        result = cli.main(["--path", str(world.root), "scheduler", "stop"])
    assert result == 0
    return json.loads(capsys.readouterr().out)


def test_st6_r2_a_real_daemon_with_12_second_engine_shutdown_reports_stopped(world, monkeypatch, capsys):
    with daemon(world, "slow-engine") as (proc, record):
        result = stop(world, monkeypatch, capsys, proc, "slow-engine")
        assert result.get("stopped") is True, result
        assert proc.poll() == 0, "stopped:true was returned before the daemon exited"


def test_st6_r2_a_daemon_ignoring_sigterm_reports_false_with_its_pid(world, monkeypatch, capsys):
    with daemon(world, "ignore") as (proc, record):
        result = stop(world, monkeypatch, capsys, proc, "ignore")
        assert result.get("stopped") is False, result
        assert result.get("pid") == proc.pid, result
        assert proc.poll() is None, "the fixture that ignores SIGTERM died unexpectedly"


def test_st6_r2_removing_the_process_record_does_not_mean_the_process_exited(world, monkeypatch, capsys):
    with daemon(world, "remove-record-but-live") as (proc, record):
        result = stop(world, monkeypatch, capsys, proc, "remove-record-but-live")
        assert result.get("stopped") is False, result
        assert result.get("pid") == proc.pid, result
        assert proc.poll() is None


def test_st6_r2_an_exited_daemon_is_stopped_even_with_a_retained_record(world, monkeypatch, capsys):
    with daemon(world, "exit-retaining-record") as (proc, record):
        result = stop(world, monkeypatch, capsys, proc, "exit-retaining-record")
        assert result.get("stopped") is True, result
        assert proc.poll() == 0


def test_st6_r2_a_reused_pid_with_a_different_start_time_is_already_stopped(world, monkeypatch, capsys):
    with daemon(world, "ignore") as (proc, record):
        current = procs.start_time(proc.pid)
        assert current, "this identity test requires Linux /proc start times"
        record.write_text(json.dumps({"pid": proc.pid, "pid_start": str(int(current) - 1)}))
        result = stop(world, monkeypatch, capsys, proc, "ignore")
        assert result.get("stopped") is True, result
        assert proc.poll() is None, "a different process with the old pid was signalled"


def test_st6_r2_pid_reuse_during_shutdown_counts_as_exit_of_the_recorded_process(world, monkeypatch, capsys):
    with daemon(world, "ignore") as (proc, record):
        original_start_time = procs.start_time
        initial = original_start_time(proc.pid)
        assert initial, "this identity test requires Linux /proc start times"
        reading = [initial]

        class ReusedPidClock(ShutdownClock):
            def sleep(self, seconds):
                # Inject a kernel identity reading, rather than depending on
                # Linux actually recycling this exact pid during a test.
                reading[0] = str(int(initial) + 1)
                super().sleep(seconds)

        with monkeypatch.context() as patch:
            patch.setattr(procs, "start_time", lambda pid: (
                reading[0] if pid == proc.pid else original_start_time(pid)))
            patch.setattr(scheduler, "time", ReusedPidClock(proc, "ignore"))
            code = cli.main(["--path", str(world.root), "scheduler", "stop"])
        assert code == 0
        result = json.loads(capsys.readouterr().out)
        assert result.get("stopped") is True, result
        assert proc.poll() is None, "the replacement process must remain alive"
