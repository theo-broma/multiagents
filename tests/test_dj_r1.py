"""DJ-R1: observable detached launch, transport, retry and termination."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest
from test_dj_support import jobs, eventually, CLI_BOUND


def test_dj_r1_returns_while_command_runs_in_an_independent_session(jobs):
    job, result = jobs.held()
    assert job['state'] == 'running'
    assert str(job['pid']) in result.stdout
    assert os.getsid(job['pid']) != os.getsid(0)
    assert os.getpgid(job['pid']) != os.getpgrp()
    assert job['pid_start'] is not None


@pytest.mark.parametrize('status', [0, 7])
def test_dj_r1_exit_status_stdout_stderr_stdin_and_cwd(jobs, status):
    code = f"import os,sys; print(os.getcwd()); print('OUT'); print('ERR',file=sys.stderr); print('EOF='+repr(sys.stdin.read())); sys.exit({status})"
    job, result = jobs.run(code)
    ended = jobs.ended(job)
    assert ended['state'] == 'finished'
    assert ended['exit_code'] == status
    logs = [p for p in Path(jobs.env['MULTIAGENTS_STATE_DIR']).rglob('*')
            if p.is_file() and str(p) in result.stdout]
    assert logs, 'run must print its log path'
    text = logs[0].read_text()
    assert str(jobs.paths.root) in text
    assert 'OUT' in text and 'ERR' in text and "EOF=''" in text


def test_dj_r1_signal_is_named(jobs):
    job, _ = jobs.run('import os,signal; os.kill(os.getpid(),signal.SIGTERM)')
    assert jobs.ended(job)['exit_code'] == 'signal:TERM'


def test_dj_r1_unexecutable_command_is_a_recorded_failure(jobs):
    result = jobs.cli('run', '--owner', 'orchestrator', '--', '/does/not/exist/dj-command')
    assert result.returncode != 0
    assert 'dj-command' in result.stderr
    found = jobs.listing()
    assert len(found) == 1 and found[0]['state'] == 'failed_to_start'


def test_dj_r1_same_live_key_reuses_job_but_ended_key_can_run_again(jobs):
    job, _ = jobs.held(key='retry')
    second = jobs.cli('run', '--owner', 'orchestrator', '--key', 'retry', '--', 'sh', '-c', 'exit 9')
    assert second.returncode == 0 and job['id'] in second.stdout
    assert len(jobs.listing()) == 1
    os.kill(job['pid'], signal.SIGTERM)
    jobs.ended(job)
    new, _ = jobs.run('pass', name='next', key='retry')
    assert new['id'] != job['id']


def test_dj_r1_root_control_environment_is_removed(jobs):
    jobs.env.update(DJ_ORDINARY='kept', MULTIAGENTS_SAFEPOINT_KEY='secret',
                    MULTIAGENTS_SAFEPOINT_DIR='/not/a/safepoint', MULTIAGENTS_SAFEPOINT_DRIVER='123')
    job, result = jobs.run("import os,json; print(json.dumps(dict(os.environ)))")
    jobs.ended(job)
    log = next(p for p in Path(jobs.env['MULTIAGENTS_STATE_DIR']).rglob('*')
               if p.is_file() and str(p) in result.stdout)
    env = json.loads(log.read_text())
    assert env['DJ_ORDINARY'] == 'kept'
    assert not any(k in env for k in ('MULTIAGENTS_SAFEPOINT_KEY', 'MULTIAGENTS_SAFEPOINT_DIR', 'MULTIAGENTS_SAFEPOINT_DRIVER'))


@pytest.mark.parametrize('sig', [signal.SIGTERM, signal.SIGKILL])
def test_dj_r1_job_survives_callers_whole_process_group(jobs, sig):
    # This caller remains alive after its ordinary foreground bg run returns.
    returned = jobs.paths.root / 'caller-returned'
    caller = subprocess.Popen([sys.executable, '-c',
        f"import pathlib,subprocess,sys,signal; p=subprocess.run(sys.argv[1:]); pathlib.Path({str(returned)!r}).touch(); signal.pause()",
        sys.executable, '-m', 'multiagents.cli', '--path', str(jobs.paths.root),
        'bg', 'run', '--owner', 'orchestrator', '--', 'sleep', '20'],
        cwd=jobs.paths.root, env=jobs.env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Read registry rather than blocking on a missing feature's stdout.
        job = eventually(lambda: jobs.listing(), lambda rows: len(rows) == 1)[0]
        eventually(returned.exists)
        os.killpg(caller.pid, sig)
        caller.wait(timeout=CLI_BOUND)
        assert jobs.listing()[0]['state'] == 'running'
        assert os.getsid(job['pid']) != caller.pid
    finally:
        if caller.poll() is None:
            os.killpg(caller.pid, signal.SIGKILL)
        caller.wait(timeout=CLI_BOUND)
        caller.stdout.close()
        caller.stderr.close()


def test_dj_r1_concurrent_key_retries_start_only_one_command(jobs):
    from concurrent.futures import ThreadPoolExecutor
    def retry(_):
        return jobs.cli('run','--owner','orchestrator','--key','same-live-key','--','sleep','20')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(retry,range(4)))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    records = jobs.listing()
    assert len(records) == 1 and records[0]['state'] == 'running'
    assert all(records[0]['id'] in r.stdout for r in results)
