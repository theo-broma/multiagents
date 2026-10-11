"""DJ-R4/R5: installed hooks use their own timeout, including on first Stop."""
import io
import json
import shlex

import pytest

from multiagents import bg, cli
from multiagents.procs import start_time
from test_dj_seams import NOW, live_pid, record
from test_dj_support import jobs


@pytest.mark.parametrize('state', ['starting', 'running'])
@pytest.mark.parametrize('preparation_delay', [0, 30])
def test_dj_r4_r5_installed_short_hook_waits_and_heartbeats(
        jobs, live_pid, monkeypatch, capsys, state, preparation_delay):
    monkeypatch.setenv('MULTIAGENTS_STATE_DIR', jobs.env['MULTIAGENTS_STATE_DIR'])
    elapsed = [0]
    monkeypatch.setattr(bg, 'now', lambda: NOW + elapsed[0])
    session = 'short-hook-session'
    settings = bg.prepare_hook(jobs.paths, 'orchestrator', session, 120)
    bg.bind(jobs.paths, 'orchestrator', session, live_pid.pid)
    value = record(jobs, state=state, started_at=NOW,
                   pid=live_pid.pid if state == 'running' else None,
                   pid_start=start_time(live_pid.pid) if state == 'running' else None)

    def advance(seconds):
        elapsed[0] += 30
        assert elapsed[0] <= 60, 'installed watcher ignored its 120-second hook timeout'
        # A starting job at the first Stop must keep the watcher armed while
        # its launch transaction commits the command's identity.
        if state == 'starting':
            record(jobs, state='running', started_at=NOW, pid=live_pid.pid,
                   pid_start=start_time(live_pid.pid))

    hook = json.loads(settings.read_text())['hooks']['Stop'][0]['hooks'][0]
    command = shlex.split(hook['command'])
    write_json = bg.write_json

    def delayed_write(path, value):
        write_json(path, value)
        if path.name == 'watch-orchestrator.lock':
            elapsed[0] += preparation_delay

    with monkeypatch.context() as watching:
        watching.setattr(bg, 'write_json', delayed_write)
        watching.setattr(bg.time, 'sleep', advance)
        watching.setattr(bg.sys, 'stdin', io.StringIO(json.dumps({'session_id': session})))
        assert cli.main(command[command.index('--path'):]) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Detached jobs heartbeat:' in output.err and value['id'] in output.err
    assert elapsed[0] == 60
    row = bg.Registry(jobs.paths).get(value['id'])
    assert row['emitted_at'] is None and row['acked_at'] is None
