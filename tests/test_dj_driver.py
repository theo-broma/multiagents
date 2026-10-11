"""DJ-R6: observe the resume prompt through the existing real-child harness.

The harness drives the real attached driver, substitutes only the provider,
and exposes the arguments/environment delivered to that external provider.
No private driver state is asserted.
"""
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent/'support'))
from test_phase0_interactive_compact import Session, OVER
from test_dj_support import jobs
from test_dj_seams import record, registry, live_pid
from multiagents.procs import start_time


@pytest.mark.parametrize('state', ['starting','running','finished'])
def test_dj_r6_detached_only_work_produces_resume_prompt(tmp_path, monkeypatch, capsys, jobs, live_pid, state):
    # Session owns its project and its sole root node; no spawned agent exists.
    base = tmp_path/'driver'; base.mkdir()
    session = Session(base,monkeypatch,capsys)
    jobs.paths = session.paths
    import time
    now = time.time()
    value = record(jobs,state=state,started_at=now,
                   pid=live_pid.pid if state=='running' else None,
                   pid_start=start_time(live_pid.pid) if state=='running' else None,
                   ended_at=now if state=='finished' else None)
    if state=='finished': (registry(jobs)/f"{value['id']}.exit").write_text('7\n')
    session.reading(OVER)
    code, _ = session.run([{'life':3}, {'life':0}])
    launches = session.calls('launch')
    assert code == 0 and len(launches) >= 2, 'driver did not compact and resume'
    prompt = launches[1]['env'].get('MULTIAGENTS_RESUME_PROMPT','')
    assert value['id'] in prompt and value['name'] in prompt, prompt
    assert state in prompt.lower()
    assert str(registry(jobs)/f"{value['id']}.log") in prompt
    row = next(r for r in jobs.listing() if r['id']==value['id'])
    assert row['acked_at'] is None
