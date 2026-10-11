"""DJ-R2: durable records, private host storage and concurrent launches."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat

from test_dj_support import jobs

FIELDS = {'id','key','name','owner','argv','cwd','origin_session','pid','pid_start',
          'started_at','state','exit_code','ended_at','emitted_at','emitted_session','acked_at'}


def test_dj_r2_record_survives_fresh_cli_and_has_contract_fields(jobs):
    job, _ = jobs.run('pass', name='durable')
    ended = jobs.ended(job)
    assert FIELDS <= ended.keys()
    assert ended['owner'] == 'orchestrator'
    assert ended['cwd'] == str(jobs.paths.root)
    assert ended['origin_session'] is None
    assert ended['ended_at'] is not None
    assert ended['acked_at'] is None


def test_dj_r2_registry_is_private_and_outside_project(jobs):
    jobs.run('pass')
    root = Path(jobs.env['MULTIAGENTS_STATE_DIR']) / 'bg' / jobs.paths.slug
    assert root.is_dir()
    assert not root.is_relative_to(jobs.paths.root)
    for path in [root, *root.rglob('*')]:
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        elif path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_dj_r2_twenty_concurrent_runs_preserve_twenty_records(jobs):
    def launch(n):
        return jobs.cli('run','--owner','orchestrator','--name',f'job-{n}', '--', 'sh','-c','exit 0')
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(launch, range(20)))
    assert all(r.returncode == 0 for r in outcomes), [r.stderr for r in outcomes]
    found = jobs.listing()
    assert len(found) == 20
    assert len({r['id'] for r in found}) == 20
    assert {r['name'] for r in found} == {f'job-{n}' for n in range(20)}
    assert all(FIELDS <= r.keys() for r in found)

from test_dj_r5 import root_launch


def test_dj_r2_origin_session_is_the_driver_binding_for_root_callers(jobs, root_launch):
    _, launch, _ = root_launch
    capture, _, _ = launch('orchestrator',hold=True)
    jobs.env['MULTIAGENTS_SESSION_ID'] = capture['session']
    jobs.env['MULTIAGENTS_ROLE'] = 'orchestrator'
    job, _ = jobs.run('pass')
    assert job['origin_session'] == capture['session']
