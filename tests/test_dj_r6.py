"""DJ-R6: public return-message renderer includes delivery-time detached work."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from multiagents.compact_return import message, snapshot
from multiagents.tree import Tree
from test_dj_support import jobs


def render(jobs, snap=None):
    tree = Tree(jobs.paths.tree_file,jobs.paths.events_file)
    return message(tree,'session',snap or snapshot(tree,'session',10000),
                   {'code':0,'detail':'10000 -> 1000','unsupported':64})


def test_dj_r6_return_names_running_and_unacknowledged_finished_jobs(jobs):
    running, out = jobs.held(name='running-suite')
    finished, endout = jobs.run('raise SystemExit(7)',name='finished-suite')
    jobs.ended(finished)
    text = render(jobs)
    for job in (running,finished):
        assert job['id'] in text and job['name'] in text
    assert 'running' in text.lower() and '7' in text
    logs = [str(p) for p in Path(jobs.env['MULTIAGENTS_STATE_DIR']).rglob('*')
            if p.is_file() and str(p) in out.stdout+endout.stdout]
    assert len(logs) == 2 and all(p in text for p in logs)
    assert all(r['acked_at'] is None for r in jobs.listing())


def test_dj_r6_states_are_read_at_delivery_not_snapshot(jobs):
    job, _ = jobs.held(name='changed-during-compaction')
    tree = Tree(jobs.paths.tree_file,jobs.paths.events_file)
    snap = snapshot(tree,'session',10000)
    assert jobs.cli('stop',job['id']).returncode == 0
    text = render(jobs,snap)
    assert job['id'] in text and 'stopped' in text.lower()


def test_dj_r6_fifty_jobs_keep_return_under_four_thousand_characters(jobs):
    def launch(n):
        return jobs.cli('run','--owner','orchestrator','--name',f'suite-{n}-'+'x'*100,'--','sh','-c','exit 0')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(launch,range(50)))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    text = render(jobs)
    assert len(text) <= 4000
    assert any(r['id'] in text for r in jobs.listing()), 'cap discarded every job'
    assert all(r['acked_at'] is None for r in jobs.listing())


def test_dj_r6_no_detached_job_rows_when_registry_empty(jobs):
    text = render(jobs)
    assert 'bg/' not in text and 'exit_code' not in text
