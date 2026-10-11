#!/bin/sh
# DJ-R10: manual Claude Code evidence; dry-run never probes or launches Claude.
set -eu
if [ "${1:-}" = "--dry-run" ]; then
    printf '%s\n' 'DRY RUN: Claude Code version (not queried)' \
        'DRY RUN: CLI SIGTERM survival' 'DRY RUN: Stop hook notification' \
        'DRY RUN: heartbeat' 'DRY RUN: empty PATH hook command'
    exit 0
fi
if [ "$#" -ne 0 ]; then
    printf '%s\n' 'usage: dj-live-check.sh [--dry-run]' >&2
    exit 1
fi
DJ_SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/../src" && pwd)
export PYTHONPATH="$DJ_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 - <<'PY'
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from multiagents import bg
from multiagents.paths import ProjectPaths

print('Claude Code version:', subprocess.check_output(['claude', '--version'], text=True).strip(), flush=True)
failed = False
with tempfile.TemporaryDirectory(prefix='dj-live-') as temporary:
    root = Path(temporary)
    os.environ['MULTIAGENTS_STATE_DIR'] = str(root / 'state')
    os.environ['MULTIAGENTS_CONFIG_DIR'] = str(root / 'config')
    paths = ProjectPaths(root / 'project')
    paths.ensure()
    registry = bg.Registry(paths)
    children = []
    captures = {}

    def cli(*args, env=None):
        return subprocess.run(bg.module_command('multiagents.cli') +
            ['--path', str(paths.root), 'bg', *args], cwd=paths.root,
            env=env, capture_output=True, text=True, check=True)

    def rows():
        return json.loads(cli('list', '--json').stdout)

    def launch(prompt, timeout=180):
        session = str(uuid.uuid4())
        settings = bg.prepare_hook(paths, 'orchestrator', session, timeout)
        stdout = root / f'child-{len(children)}.stdout'
        stderr = root / f'child-{len(children)}.stderr'
        with stdout.open('w') as out, stderr.open('w') as err:
            child = subprocess.Popen(['claude', '-p', '--model', 'haiku',
                '--dangerously-skip-permissions', '--session-id', session,
                '--settings', str(settings), prompt], cwd=paths.root,
                env=dict(os.environ, MULTIAGENTS_SESSION_ID=session, MULTIAGENTS_ROLE='orchestrator'),
                stdout=out, stderr=err, text=True)
        children.append(child)
        captures[child] = dict(session=session, settings=settings, stdout=stdout,
                               stderr=stderr, started=time.monotonic(), watchers={})
        bg.bind(paths, 'orchestrator', session, child.pid)
        return child, session, settings

    def observe_watchers():
        holder = bg.read_json(registry.root / 'watch-orchestrator.lock')
        if not isinstance(holder, dict): return
        for child in children:
            if child.poll() is None:
                capture = captures[child]
                key = (holder.get('pid'), holder.get('pid_start'))
                capture['watchers'].setdefault(key, round(time.monotonic() - capture['started'], 1))

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            observe_watchers()
            if predicate(): return True
            time.sleep(.2)
        return False

    def output(child, stream):
        return captures[child][stream].read_text(errors='replace')

    def terminate(child):
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()

    def clear_jobs():
        for row in rows():
            if row['state'] in ('starting', 'running'): cli('stop', row['id'])
        for row in rows():
            if row['unacked']: cli('ack', row['id'])

    def verdict(label, passed, child, names, detail='', hook_result=None):
        global failed
        failed |= not passed
        print(('PASS: ' if passed else 'FAIL: ') + label, flush=True)
        if passed: return
        capture = captures[child]
        lock = registry.root / 'watch-orchestrator.lock'
        try: lock_contents = lock.read_text().strip()
        except OSError as exc: lock_contents = f'(unavailable: {exc})'
        print(f"  child pid={child.pid} session={capture['session']} exit={child.poll()} "
              f"elapsed={time.monotonic() - capture['started']:.1f}s {detail}", flush=True)
        for stream in ('stdout', 'stderr'):
            tail = '\n'.join(output(child, stream).splitlines()[-40:])[-8000:]
            print(f'  child {stream} tail:\n{tail or "(empty)"}', flush=True)
        print('  hook settings:', capture['settings'].read_text().strip(), flush=True)
        print('  watcher first seen (seconds after child launch):',
              json.dumps([dict(pid=pid, pid_start=start, first_seen=elapsed,
                               alive=bg.living(pid, start))
                          for (pid, start), elapsed in capture['watchers'].items()]), flush=True)
        print(f'  watcher lock {lock}:', lock_contents, flush=True)
        try:
            relevant = [r for r in rows() if r.get('name') in names
                        or r.get('origin_session') == capture['session']
                        or r.get('owner') == 'orchestrator' and
                        (r['state'] in ('starting', 'running') or r['unacked'])]
            print('  bg list --json relevant rows:', json.dumps(relevant, indent=2), flush=True)
        except (subprocess.CalledProcessError, ValueError) as exc:
            print('  bg list --json failed:', exc, getattr(exc, 'stderr', ''), flush=True)
        if hook_result is not None:
            print(f'  direct hook exit={hook_result.returncode}\n'
                  f'  direct hook stdout tail:\n{hook_result.stdout[-8000:]}\n'
                  f'  direct hook stderr tail:\n{hook_result.stderr[-8000:]}', flush=True)

    bg_command = shlex.join(bg.module_command('multiagents.cli') + ['--path', str(paths.root), 'bg'])
    command = bg_command + ' run --owner orchestrator'
    synchronous = 'Use an ordinary synchronous Bash call, wait for it to return, and do not use run_in_background. '
    try:
        marker = paths.root / 'survived'
        jobcode = f'import time,pathlib; time.sleep(5); pathlib.Path({str(marker)!r}).touch(); time.sleep(120)'
        start = command + ' --name survival -- ' + shlex.join([sys.executable, '-c', jobcode])
        child, session, settings = launch(synchronous + f'Run this Bash command exactly: {start}. Then finish your answer with WAITING. When the hook notifies you, do nothing else.')
        started = wait_for(lambda: any(r['name'] == 'survival' and r['state'] == 'running' for r in rows()), 45)
        terminate(child)
        verdict('bg run survives CLI SIGTERM', started and wait_for(marker.exists, 10), child, ['survival'])
        clear_jobs()

        start = command + ' --name notification -- ' + shlex.join([sys.executable, '-c', 'import time; time.sleep(8); print("DJ_JOB_DONE")'])
        child, _, _ = launch(synchronous + f'Run this Bash command exactly: {start}. Finish this turn with WAITING. When a detached job report arrives from the Stop hook, answer exactly DJ_WAKE_OBSERVED and do not run further commands.')
        ended = wait_for(lambda: child.poll() is not None, 100)
        verdict('Stop hook wakes the idle session', ended and 'DJ_WAKE_OBSERVED' in output(child, 'stdout'),
                child, ['notification'], '' if ended else 'timed out waiting for Claude')
        terminate(child)
        clear_jobs()

        start = command + ' --name heartbeat -- ' + shlex.join([sys.executable, '-c', 'import time; time.sleep(300)'])
        child, session, settings = launch(synchronous + f'Run this Bash command exactly: {start}. Finish this turn with WAITING. When a heartbeat arrives from the Stop hook, use {bg_command} stop ID on the heartbeat job and {bg_command} ack ID, replacing ID with its job id. Then answer exactly DJ_HEARTBEAT_OBSERVED and finish.', timeout=120)
        # The 60-second heartbeat starts at the watcher, after the first model
        # turn. Allow time for both that turn and the response/stop/ack turn.
        ended = wait_for(lambda: child.poll() is not None, 180)
        verdict('heartbeat at short bg.watch_timeout', ended and 'DJ_HEARTBEAT_OBSERVED' in output(child, 'stdout'),
                child, ['heartbeat'], '' if ended else 'timed out waiting for Claude')
        terminate(child)
        clear_jobs()

        # A real Claude PID remains alive for the empty-PATH invocation. A due
        # end gives the hook an immediate result without waiting a whole window.
        cli('run', '--owner', 'orchestrator', '--name', 'empty-path', '--', sys.executable, '-c', 'pass')
        wait_for(lambda: any(r['name'] == 'empty-path' and r['unacked'] for r in rows()), 10)
        child, session, settings = launch('Use Bash to sleep for 30 seconds, then answer DONE.')
        hook = json.loads(settings.read_text())['hooks']['Stop'][0]['hooks'][0]['command']
        try:
            result = subprocess.run(shlex.split(hook), input=json.dumps({'session_id': session}),
                env=dict(os.environ, PATH='', PYTHONPATH=''), cwd=root,
                capture_output=True, text=True, timeout=10)
            verdict('hook command with empty PATH and another cwd', result.returncode == 2 and 'empty-path' in result.stderr,
                    child, ['empty-path'], hook_result=result)
        except subprocess.TimeoutExpired as exc:
            verdict('hook command with empty PATH and another cwd', False, child, ['empty-path'],
                    f'direct hook timed out: {exc}')
    finally:
        for child in children:
            terminate(child)
        clear_jobs()
raise SystemExit(1 if failed else 0)
PY
