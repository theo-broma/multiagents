"""DJ-R4 invalid bindings must never emit or acknowledge an end."""
import json
from test_dj_support import jobs


def test_dj_r4_unbound_session_exits_silently_without_touching_jobs(jobs):
    job, _ = jobs.run('pass')
    before = jobs.ended(job)
    result = jobs.cli('watch', '--owner','orchestrator', input=json.dumps({'session_id':'unbound'}))
    assert result.returncode == 0, result.stderr
    assert result.stdout == '' and result.stderr == ''
    after = jobs.listing()[0]
    for field in ('acked_at','emitted_at','emitted_session','ended_at'):
        assert after[field] == before[field]


def test_dj_r4_no_binding_and_no_work_exits_zero(jobs):
    result = jobs.cli('watch','--owner','initializer',input=json.dumps({'session_id':'unbound'}))
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr

# A live binding is created through the public root CLI, never a fabricated file.
import os
from pathlib import Path
import signal
import subprocess
from test_dj_r5 import root_launch
from test_dj_support import eventually, CLI_BOUND, ROOT


def hook_command(capture):
    import shlex
    argv = capture['argv']
    assert '--settings' in argv, 'root launch has not installed the DJ Stop hook'
    settings = Path(argv[argv.index('--settings')+1])
    hooks = [h for entry in json.loads(settings.read_text())['hooks']['Stop']
             for h in entry.get('hooks',[])]
    assert len(hooks) == 1
    return shlex.split(hooks[0]['command'])


def emit(capture, cwd):
    return subprocess.run(hook_command(capture),
        input=json.dumps({'session_id':capture['session']}),cwd=cwd,
        capture_output=True,text=True,timeout=CLI_BOUND,
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src')))


def spawn_watch(capture,cwd):
    child = subprocess.Popen(hook_command(capture),cwd=cwd,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src')))
    child.stdin.write(json.dumps({'session_id':capture['session']}))
    child.stdin.close(); child.stdin = None
    return child


def test_dj_r4_due_end_is_immediate_emission_not_ack_and_same_session_is_silent(jobs, root_launch):
    paths, launch, _ = root_launch
    job, _ = jobs.run('print("DONE")',name='ended-before-watch')
    jobs.ended(job)
    capture, _, _ = launch('orchestrator',hold=True)
    report = emit(capture,paths.root)
    assert report.returncode == 2 and job['id'] in report.stderr
    assert not report.stdout
    record = jobs.listing()[0]
    assert record['emitted_at'] is not None
    assert record['emitted_session'] == capture['session']
    assert record['acked_at'] is None and record.get('unacked')
    second = emit(capture,paths.root)
    assert second.returncode == 0 and not second.stderr


def test_dj_r4_new_session_adopts_old_unacknowledged_end(jobs,root_launch):
    paths, launch, _ = root_launch
    job, _ = jobs.run('pass')
    jobs.ended(job)
    first, process, release = launch('orchestrator',hold=True)
    assert emit(first,paths.root).returncode == 2
    release.touch(); process.communicate(timeout=CLI_BOUND)
    second, _, _ = launch('orchestrator',fresh=True,hold=True)
    assert second['session'] != first['session']
    report = emit(second,paths.root)
    assert report.returncode == 2 and job['id'] in report.stderr
    assert jobs.listing()[0]['acked_at'] is None


def test_dj_r4_report_is_capped_sanitised_and_tail_limited(jobs,root_launch):
    paths, launch, _ = root_launch
    job, _ = jobs.run("import sys; print('OLD-LINE',flush=True); [sys.stdout.buffer.write((str(n)+' '+ 'x'*500+'\\x00\\x1b\\x07\\n').encode()) for n in range(30)]", name='binary-log')
    jobs.ended(job)
    capture, _, _ = launch('orchestrator',hold=True)
    report = emit(capture,paths.root)
    assert report.returncode == 2
    assert len(report.stderr) <= 4000
    assert not any(c in report.stderr for c in ('\x00','\x1b','\x07'))
    assert 'OLD-LINE' not in report.stderr
    assert 'x'*301 not in report.stderr
    for field in (job['id'],job['name'],'finished','0'):
        assert field in report.stderr


def test_dj_r4_owner_without_live_work_is_silent_despite_other_role(jobs,root_launch):
    paths, launch, _ = root_launch
    jobs.held(owner='initializer')
    capture, _, _ = launch('orchestrator',hold=True)
    result = emit(capture,paths.root)
    assert result.returncode == 0 and not result.stderr


def test_dj_r4_watcher_wakes_when_a_running_job_ends(jobs,root_launch):
    paths, launch, _ = root_launch
    job, _ = jobs.held()
    capture, _, _ = launch('orchestrator',hold=True)
    watcher = spawn_watch(capture,paths.root)
    try:
        os.kill(job['pid'],signal.SIGTERM)
        eventually(watcher.poll, lambda code: code is not None)
        out, err = watcher.communicate(timeout=CLI_BOUND)
        assert watcher.returncode == 2 and job['id'] in err and not out
    finally:
        if watcher.poll() is None: watcher.kill()
        watcher.communicate(timeout=CLI_BOUND)


def test_dj_r4_duplicate_watchers_share_one_watch_and_exit_when_actual_cli_dies(jobs,root_launch):
    paths, launch, _ = root_launch
    jobs.held()
    capture, process, _ = launch('orchestrator',hold=True)
    watchers = []
    try:
        watchers = [spawn_watch(capture,paths.root),spawn_watch(capture,paths.root)]
        eventually(lambda: [w.poll() for w in watchers],lambda codes: any(c is not None for c in codes))
        codes = [w.poll() for w in watchers]
        assert codes.count(None) == 1 and codes.count(0) == 1, codes
        os.kill(capture['pid'],signal.SIGTERM)
        process.communicate(timeout=CLI_BOUND)
        for watcher in watchers:
            eventually(watcher.poll, lambda code: code is not None)
            out,err = watcher.communicate(timeout=CLI_BOUND)
            assert watcher.returncode == 0 and not out and not err
        assert jobs.listing()[0]['emitted_at'] is None
    finally:
        for watcher in watchers:
            if watcher.poll() is None: watcher.kill()
            watcher.communicate(timeout=CLI_BOUND)
