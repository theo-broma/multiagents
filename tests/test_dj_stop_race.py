"""DJ-R3: a job exiting during stop's identity lookup is already ended."""
from types import SimpleNamespace

import pytest

from multiagents import bg
from multiagents.procs import start_time
from test_dj_seams import clock, live_pid, listed, record, registry
from test_dj_support import jobs


@pytest.mark.parametrize('lookup', ['getpgid', 'getsid'])
@pytest.mark.parametrize('exit_status', [None, 7])
def test_dj_r3_stop_reports_job_ending_during_identity_lookup(
        jobs, live_pid, monkeypatch, capsys, lookup, exit_status):
    clock(jobs)
    monkeypatch.setenv('MULTIAGENTS_STATE_DIR', jobs.env['MULTIAGENTS_STATE_DIR'])
    monkeypatch.setenv('MULTIAGENTS_BG_NOW', jobs.env['MULTIAGENTS_BG_NOW'])
    value = record(jobs, state='running', pid=live_pid.pid,
                   pid_start=start_time(live_pid.pid))

    def exited(pid):
        assert pid == live_pid.pid
        live_pid.kill()
        live_pid.wait(timeout=3)
        if exit_status is not None:
            (registry(jobs) / f"{value['id']}.exit").write_text(f'{exit_status}\n')
        raise ProcessLookupError()

    def signal_group(*args):
        pytest.fail('stop must not signal a job whose identity lookup failed')

    monkeypatch.setattr(bg.os, lookup, exited)
    monkeypatch.setattr(bg.os, 'killpg', signal_group)
    args = SimpleNamespace(bg_command='stop', id=value['id'])
    assert bg.dispatch(jobs.paths, args) == 0
    expected = 'lost' if exit_status is None else 'finished'
    assert capsys.readouterr().out == f"{value['id']}: {expected}\n"
    after = listed(jobs, value['id'])
    assert after['state'] == expected
    assert after['exit_code'] == exit_status
    assert after['ended_at'] is not None
    assert after['acked_at'] is None
