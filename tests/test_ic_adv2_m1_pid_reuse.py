"""Adversary: M1 pid reuse causes dead process to be misreported as Running.

IC-C4 / IC-C5: "Its stop signals only processes it started and recorded; it never
signals an unrelated host process."
A backend inspecting a resource must report Exited or Unknown when the original
process exits. If the PID is recycled by an unrelated process on the host,
ReferenceBackend._alive must verify that pid_start matches record["pid_start"].

Without the pid_start check (mutant M1), ReferenceBackend._invocation_state reports
Running() for an unrelated process that happened to get the same PID, and
stop() blocks waiting for the unrelated process or returns Unknown.
"""
from __future__ import annotations

import subprocess
import sys

from multiagents.executor import lifecycle as lc


def test_ic_adv2_pid_reuse_with_different_start_time_is_not_reported_running(tmp_path):
    # Start an unrelated dummy process on the host
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        real_start_time = lc._start_time(victim.pid)
        assert real_start_time, "could not read start time of victim process"

        # Construct a record with victim.pid but a mismatched start time (simulating PID reuse)
        fake_record = {
            "key": "inv-reused",
            "directory": "inv-test",
            "kind": "agent",
            "pid": victim.pid,
            "pid_start": "0",  # Different start time! Original process started at time 0 and died
        }

        # _alive MUST return False because pid_start does not match.
        # Under mutant M1 (pid_start check omitted), _alive returns True!
        assert not lc._alive(fake_record), (
            "M1 defect: _alive(record) returned True for a reused PID with mismatched pid_start!"
        )
    finally:
        victim.kill()
        victim.wait()
