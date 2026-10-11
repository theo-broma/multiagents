"""DJ-R1..R4: public on-disk fixtures and clock from Decision 11."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest
from multiagents.procs import start_time
from test_dj_support import jobs, eventually, CLI_BOUND

NOW = 2_000_000_000
WEEK = 7 * 24 * 3600


def registry(jobs):
    root = Path(jobs.env['MULTIAGENTS_STATE_DIR']) / 'bg' / jobs.paths.slug
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def record(jobs, ident='0123456789ab', **changes):
    value = dict(version=1, id=ident, key=None, name='contract-job',
                 owner='orchestrator', argv=['true'], cwd=str(jobs.paths.root),
                 origin_session=None, pid=None, pid_start=None, started_at=NOW-100,
                 state='starting', exit_code=None, ended_at=None,
                 emitted_at=None, emitted_session=None, acked_at=None)
    value.update(changes)
    path = registry(jobs) / f'{ident}.json'
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return value


def listed(jobs, ident):
    return next(r for r in jobs.listing() if r['id'] == ident)


def clock(jobs, now=NOW):
    jobs.env['MULTIAGENTS_BG_NOW'] = str(now)


@pytest.mark.parametrize('age,expected', [(59,'starting'), (60,'starting'), (61,'lost')])
def test_dj_r1_interrupted_starting_age_boundary(jobs, age, expected):
    clock(jobs)
    value = record(jobs, started_at=NOW-age)
    row = listed(jobs, value['id'])
    assert row['state'] == expected
    assert row['ended_at'] == (NOW if expected == 'lost' else None)


def test_dj_r1_starting_key_retry_launches_nothing(jobs):
    clock(jobs)
    value = record(jobs, key='retry-start', started_at=NOW)
    marker = jobs.paths.root / 'should-not-run'
    result = jobs.cli('run','--owner','orchestrator','--key','retry-start','--',
                      sys.executable,'-c',f'from pathlib import Path; Path({str(marker)!r}).touch()')
    assert result.returncode == 0 and value['id'] in result.stdout
    assert len(jobs.listing()) == 1 and not marker.exists()


@pytest.mark.parametrize('code,exit_text', [('pass','0\n'), ('raise SystemExit(7)','7\n'),
    ('import os,signal; os.kill(os.getpid(),signal.SIGTERM)','signal:TERM\n')])
def test_dj_r1_exit_file_exact_contents(jobs, code, exit_text):
    job, _ = jobs.run(code)
    jobs.ended(job)
    assert (registry(jobs)/f"{job['id']}.exit").read_text() == exit_text


@pytest.mark.parametrize('kind', ['missing', 'permission'])
def test_dj_r1_exec_failure_reason_and_failed_record(jobs, kind):
    command = jobs.paths.root / 'unexecutable'
    if kind == 'permission':
        command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o600)
    result = jobs.cli('run','--owner','orchestrator','--',command)
    assert result.returncode != 0 and str(command) in result.stderr
    row, = jobs.listing()
    assert row['state'] == 'failed_to_start'
    assert row['ended_at'] is not None and row['pid'] is None


@pytest.fixture
def live_pid():
    child = subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'],
                             start_new_session=True)
    try:
        yield child
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=CLI_BOUND)


def test_dj_r2_r3_reused_pid_is_lost_and_stop_does_not_signal_it(jobs, live_pid):
    clock(jobs)
    value = record(jobs, state='running', pid=live_pid.pid,
                   pid_start=str(int(start_time(live_pid.pid))+1))
    assert listed(jobs,value['id'])['state'] == 'lost'
    assert listed(jobs,value['id'])['ended_at'] == NOW
    result = jobs.cli('stop',value['id'])
    assert result.returncode == 0
    assert live_pid.poll() is None
    assert listed(jobs,value['id'])['state'] == 'lost'


@pytest.mark.parametrize('state,exit_status,expected', [
    ('stopped','7\n','stopped'), ('running','7\n','finished'),
    ('failed_to_start',None,'failed_to_start'), ('finished',None,'running')])
def test_dj_r2_state_precedence_over_recorded_state(jobs, live_pid, state, exit_status, expected):
    clock(jobs)
    value = record(jobs,state=state,pid=live_pid.pid,pid_start=start_time(live_pid.pid))
    if exit_status: (registry(jobs)/f"{value['id']}.exit").write_text(exit_status)
    assert listed(jobs,value['id'])['state'] == expected


@pytest.mark.parametrize('payload', ['{broken', '{}', '{"version":2}', '[]'])
def test_dj_r2_invalid_records_are_listed_never_signalled_or_pruned(jobs, payload):
    clock(jobs)
    path = registry(jobs)/'0123456789ab.json'; path.write_text(payload)
    assert listed(jobs,'0123456789ab')['state'] == 'invalid'
    jobs.cli('stop','0123456789ab')
    jobs.cli('ack','0123456789ab')
    result = jobs.cli('prune')
    assert result.returncode == 0
    assert path.read_text() == payload


@pytest.mark.parametrize('trigger', ['prune', 'list', 'ack', 'stop', 'wait', 'run', 'watch'])
def test_dj_r2_explicit_and_implicit_pruning_spares_unacked_and_boundaries(jobs, trigger):
    clock(jobs)
    for ident, age, ack in [('111111111111',WEEK+1,True),
                            ('222222222222',WEEK,True),
                            ('333333333333',WEEK+1,False)]:
        record(jobs,ident,state='finished',ended_at=NOW-age,
               acked_at=NOW-age if ack else None)
        (registry(jobs)/f'{ident}.exit').write_text('0\n')
        (registry(jobs)/f'{ident}.log').write_text('log\n')
    args = {'list':['--json'], 'ack':['444444444444'], 'stop':['444444444444'],
            'wait':['444444444444','--timeout','0'],
            'run':['--owner','orchestrator','--','true'],
            'watch':['--owner','orchestrator','--timeout','1']}.get(trigger,[])
    result = jobs.cli(trigger,*args,input=json.dumps({'session_id':'unbound'}) if trigger=='watch' else None)
    if trigger == 'prune':
        assert result.returncode == 0 and '111111111111' in result.stdout
    for suffix in ('json','exit','log'):
        assert not (registry(jobs)/f'111111111111.{suffix}').exists()
        assert (registry(jobs)/f'222222222222.{suffix}').exists()
        assert (registry(jobs)/f'333333333333.{suffix}').exists()


@pytest.mark.parametrize('suffix', ['json','exit','log'])
def test_dj_r2_hostile_symlinks_never_read_through_or_damage_targets(jobs, suffix):
    clock(jobs)
    outside = jobs.paths.root/'outside'
    outside.write_text('SENSITIVE-CONTENT\n')
    value = record(jobs,state='finished',ended_at=NOW-WEEK-1,acked_at=NOW-WEEK-1)
    root = registry(jobs)
    (root/f"{value['id']}.exit").write_text('0\n')
    path = root/f"{value['id']}.{suffix}"
    if path.exists(): path.unlink()
    path.symlink_to(outside)
    for command in [('list','--json'),('wait',value['id'],'--timeout','0'),('prune',)]:
        result = jobs.cli(*command)
        if command[0] in ('list','prune'):
            assert result.returncode == 0, result.stderr
        assert 'SENSITIVE-CONTENT' not in result.stdout+result.stderr
    assert outside.read_text() == 'SENSITIVE-CONTENT\n'
    if suffix == 'json': assert path.is_symlink()


def binding(jobs, pid, session='contract-session', pid_start=None):
    root = registry(jobs)/'bindings'; root.mkdir(mode=0o700,exist_ok=True)
    (root/f'{session}.json').write_text(json.dumps(dict(version=1, role='orchestrator',
        session_id=session,pid=pid,pid_start=pid_start or start_time(pid))))
    return session


def watch(jobs, session, timeout=1):
    return jobs.cli('watch','--owner','orchestrator','--timeout',timeout,
                    input=json.dumps({'session_id':session}))


@pytest.mark.parametrize('elapsed,code', [(899,0),(900,0),(901,2)])
def test_dj_r4_reemission_window_boundary(jobs, live_pid, elapsed, code):
    clock(jobs)
    session = binding(jobs,live_pid.pid)
    value = record(jobs,state='finished',ended_at=NOW-1000,
                   emitted_at=NOW-elapsed,emitted_session=session)
    (registry(jobs)/f"{value['id']}.exit").write_text('0\n')
    result = watch(jobs,session)
    assert result.returncode == code, result.stderr
    row = listed(jobs,value['id'])
    assert row['acked_at'] is None
    assert row['emitted_at'] == (NOW if code == 2 else NOW-elapsed)
    assert (value['id'] in result.stderr) == (code == 2)


@pytest.mark.parametrize('start_matches', [True,False])
def test_dj_r4_stale_watcher_lock_is_taken_over(jobs, live_pid, start_matches):
    clock(jobs)
    session = binding(jobs,live_pid.pid)
    value = record(jobs,state='finished',ended_at=NOW)
    (registry(jobs)/f"{value['id']}.exit").write_text('0\n')
    dead = subprocess.Popen([sys.executable,'-c','pass']); dead.wait(timeout=CLI_BOUND)
    lock_pid = dead.pid if start_matches else live_pid.pid
    (registry(jobs)/'watch-orchestrator.lock').write_text(json.dumps({
        'pid':lock_pid,'pid_start':'not-the-live-start'}))
    result = watch(jobs,session)
    assert result.returncode == 2 and value['id'] in result.stderr


def test_dj_r4_dead_cli_wins_over_simultaneously_due_end(jobs):
    clock(jobs)
    dead = subprocess.Popen([sys.executable,'-c','pass']); dead.wait(timeout=CLI_BOUND)
    session = binding(jobs,dead.pid,pid_start='dead')
    value = record(jobs,state='finished',ended_at=NOW)
    (registry(jobs)/f"{value['id']}.exit").write_text('0\n')
    before = (registry(jobs)/f"{value['id']}.json").read_bytes()
    result = watch(jobs,session)
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert (registry(jobs)/f"{value['id']}.json").read_bytes() == before


def test_dj_r4_short_override_deadline_heartbeats_without_acknowledging(jobs, live_pid):
    clock(jobs)
    session = binding(jobs,live_pid.pid)
    value = record(jobs,state='running',pid=live_pid.pid,pid_start=start_time(live_pid.pid),started_at=NOW-123)
    result = watch(jobs,session,timeout=2)
    assert result.returncode == 2 and value['id'] in result.stderr
    assert value['name'] in result.stderr
    assert 'heartbeat' in result.stderr.lower()
    row = listed(jobs,value['id'])
    assert row['acked_at'] is None and row['emitted_at'] is None


@pytest.mark.parametrize('end_age,ack_age,removed', [
    (WEEK+1,WEEK-1,False), (WEEK-1,WEEK+1,False), (WEEK+1,WEEK+1,True)])
def test_dj_r2_pruning_requires_both_end_and_ack_to_be_old(jobs, end_age, ack_age, removed):
    clock(jobs)
    value = record(jobs,state='finished',ended_at=NOW-end_age,acked_at=NOW-ack_age)
    root = registry(jobs)
    (root/f"{value['id']}.exit").write_text('0\n')
    result = jobs.cli('prune')
    assert result.returncode == 0, result.stderr
    assert (root/f"{value['id']}.json").exists() != removed


def test_dj_r2_future_version_with_live_pid_is_never_signalled(jobs, live_pid):
    clock(jobs)
    value = record(jobs,version=2,state='running',pid=live_pid.pid,
                   pid_start=start_time(live_pid.pid))
    path = registry(jobs)/f"{value['id']}.json"
    before = path.read_bytes()
    assert listed(jobs,value['id'])['state'] == 'invalid'
    jobs.cli('stop',value['id'])
    assert live_pid.poll() is None and path.read_bytes() == before
