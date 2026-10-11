"""DJ-R3: wait exit codes, acknowledgement, role filtering and stop isolation."""
import pytest
from test_dj_support import jobs


@pytest.mark.parametrize('status,expected', [(0,0),(3,1)])
def test_dj_r3_wait_reports_and_acknowledges_finished_job(jobs, status, expected):
    job, _ = jobs.run(f'import sys; print("TAIL"); sys.exit({status})')
    result = jobs.cli('wait', job['id'], '--timeout', '2')
    assert result.returncode == expected, result.stderr
    assert job['id'] in result.stdout + result.stderr
    assert 'TAIL' in result.stdout + result.stderr
    record = jobs.listing()[0]
    assert record['acked_at'] is not None
    assert not record.get('unacked')


def test_dj_r3_wait_timeout_does_not_acknowledge(jobs):
    job, _ = jobs.held()
    result = jobs.cli('wait', job['id'], '--timeout', '0')
    assert result.returncode == 124
    assert jobs.listing()[0]['acked_at'] is None


def test_dj_r3_list_filters_owners_and_ack_clears_unacked(jobs):
    a, _ = jobs.run('pass', name='a')
    b, _ = jobs.run('pass', owner='initializer', name='b')
    jobs.ended(a); jobs.ended(b)
    assert {r['id'] for r in jobs.listing('orchestrator')} == {a['id']}
    assert {r['id'] for r in jobs.listing('initializer')} == {b['id']}
    assert all(r.get('unacked') for r in jobs.listing())
    result = jobs.cli('ack', a['id'], b['id'])
    assert result.returncode == 0
    assert all(r['acked_at'] is not None and not r.get('unacked') for r in jobs.listing())


def test_dj_r3_stop_is_idempotent_and_leaves_other_job_running(jobs):
    a, _ = jobs.held(name='a')
    b, _ = jobs.held(name='b')
    assert jobs.cli('stop', a['id']).returncode == 0
    stopped = jobs.ended(a)
    assert stopped['state'] == 'stopped'
    assert jobs.cli('stop', a['id']).returncode == 0
    repeated = next(r for r in jobs.listing() if r['id'] == a['id'])
    assert repeated['ended_at'] == stopped['ended_at']
    assert next(r for r in jobs.listing() if r['id'] == b['id'])['state'] == 'running'
    assert jobs.cli('wait', a['id'], '--timeout','0').returncode == 1


def test_dj_r3_unknown_stop_is_exit_one(jobs):
    result = jobs.cli('stop', 'unknown-job')
    assert result.returncode == 1, result.stderr


def test_dj_r3_stop_of_finished_job_preserves_state_and_exit(jobs):
    job, _ = jobs.run('raise SystemExit(7)')
    before = jobs.ended(job)
    assert jobs.cli('stop', job['id']).returncode == 0
    after = jobs.listing()[0]
    assert (after['state'], after['exit_code'], after['ended_at']) == (before['state'],7,before['ended_at'])

# One intentionally slow case: the public contract mandates a real 10-second
# TERM grace and offers no clock injection. All other waits use short bounds.
STOP_GRACE_BOUND = 14.0


@pytest.mark.slow
def test_dj_r3_stop_escalates_to_kill_for_a_command_ignoring_term(jobs):
    job, _ = jobs.held(ignore_term=True)
    result = jobs.cli('stop',job['id'],timeout=STOP_GRACE_BOUND)
    assert result.returncode == 0, result.stderr
    assert jobs.ended(job)['state'] == 'stopped'
    # A zombie has ended; a live task must no longer be executing.
    from pathlib import Path
    stat = Path(f"/proc/{job['pid']}/stat")
    if stat.exists():
        assert stat.read_text().rsplit(')',1)[1].split()[0] == 'Z'
