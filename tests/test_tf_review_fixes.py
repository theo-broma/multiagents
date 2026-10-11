"""TF review regressions: fallback specs, pre-spawn retries and questions."""
import asyncio
import copy
import json
import os
from unittest.mock import AsyncMock

import pytest

from multiagents.runner import Runner
from multiagents.scheduler.engine import attempts, save_attempt
from multiagents.scheduler.worker import supervise
from multiagents.spendcap import ModelCap, SpendCap

from test_nc_m2_guard_recovery import local, deposit, claim, read
from test_tf_support import w, WAIT, start


def fallback(engine, monkeypatch, models):
    config = engine.service.configuration()
    provider = copy.copy(engine.runner.providers['fx'])
    provider.name = provider.family = 'backup'
    provider.billing = 'metered'
    provider.spend_cap = SpendCap(models={'backup/m2': ModelCap(0, 'day')})
    engine.runner.providers['backup'] = provider
    monkeypatch.setattr(engine.runner, '_cap_providers', lambda: engine.runner.providers)
    config.agents['worker'] = config.agents['worker'].replace(models=models)
    return provider


def quota_node(engine):
    node = deposit(engine, state='held')
    node.update(hold={'reason': 'quota_exhausted'},
                quota_hold={'agent': 'worker', 'provider': 'fx',
                            'recheck_at': engine.instant() + 3600})
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    return node


@pytest.mark.parametrize('entry', [{'model': 'backup/m2', 'effort': 'high'},
                                 {'id': 'backup/m2', 'effort': 'high'}])
def test_tf_r4_structured_quota_fallback_checks_the_model_cap(local, monkeypatch, entry):
    _, engine = local
    provider = fallback(engine, monkeypatch, {'backup': entry})
    node = quota_node(engine)
    assert not engine.quota_fallback_usable(node, node['quota_hold'])
    provider.spend_cap = SpendCap(models={'backup/m2': ModelCap(10, 'day')})
    assert engine.quota_fallback_usable(node, node['quota_hold'])


def test_tf_r4_bad_quota_spec_does_not_abort_tick_or_starve_other_nodes(local, monkeypatch):
    _, engine = local
    fallback(engine, monkeypatch, ['backup'])  # malformed models mapping
    held = quota_node(engine)
    healthy = deposit(engine)
    config = engine.service.configuration()
    config.agents['healthy'] = config.agents['worker'].replace(name='healthy', models={})
    healthy['agent'] = 'healthy'
    with engine.store.transaction() as db:
        engine.store.save_node(db, healthy)
    admitted = AsyncMock(return_value={'blocked': []})
    monkeypatch.setattr(engine.runner, 'start', admitted)
    asyncio.run(engine.tick())
    admitted.assert_awaited_once()
    assert admitted.call_args.args[0] == 'healthy'
    nodes, _ = read(engine)
    assert nodes[held['id']]['hold']['reason'] == 'quota_exhausted'


def test_tf_r2_r4_worker_failure_before_spawn_retries_to_the_budget(local, monkeypatch):
    _, engine = local
    engine.service.configuration().project['scheduler']['retries'] = {
        'budget': 2, 'backoff_seconds': [0]}
    node = deposit(engine)
    monkeypatch.setattr(engine, 'spawn', lambda attempt: None)

    def before_spawn(*args, **kwargs):
        raise KeyError('launch setup missing')

    for used in range(3):
        asyncio.run(engine.tick())
        _, journal = read(engine)
        attempt = next(a for a in journal.values() if a['state'] == 'claimed')
        # Admission already succeeded; fail in the detached worker before
        # Runner creates a tree run or starts the executor.
        with monkeypatch.context() as patch:
            patch.setattr(Runner, '_settle_effort', before_spawn)
            asyncio.run(supervise(engine.paths.root, attempt['attempt_id']))
        with engine.store.transaction(write=False) as db:
            captured = attempts(db)[attempt['attempt_id']]
        assert captured['state'] == 'captured'
        assert captured['result']['cause']['class'] == 'launch'
        run_dir = engine.paths.run_dir(attempt['run_id'])
        assert not (run_dir / 'wrapper.pid').exists()
        assert not (run_dir / 'exit_status').exists()
        asyncio.run(engine.reconcile())
        asyncio.run(engine.evaluate_retries())
        nodes, _ = read(engine)
        current = nodes[node['id']]
        if used < 2:
            assert current['state'] == 'open', current
            assert current['tf_retries'] == used + 1
            assert not current.get('retry_pending')
        else:
            assert current['state'] == 'held'
            assert current['hold']['reason'] == 'retries_exhausted'
            assert current['tf_retries'] == 2
    assert engine.predecessor_liveness('ag-no-evidence') == 'unknown'


@pytest.mark.parametrize('proof', [None, False, True])
def test_tf_r2_r4_launch_class_without_no_spawn_proof_stays_unknown(local, proof):
    _, engine = local
    node = deposit(engine)
    attempt = claim(engine, node, state='recorded')
    attempt['result'] = {'status': 'failed', 'cause': {'class': 'launch'}}
    if proof is not None:
        attempt['never_spawned'] = proof
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    assert engine.predecessor_liveness(attempt['run_id'], attempt['attempt_id']) == (
        'dead' if proof else 'unknown')
    assert engine.predecessor_liveness('ag-different', attempt['attempt_id']) == 'unknown'
    run_dir = engine.paths.run_dir(attempt['run_id'])
    run_dir.mkdir(parents=True)
    (run_dir / 'wrapper.pid').write_text(str(os.getpid()))
    assert engine.predecessor_liveness(attempt['run_id'], attempt['attempt_id']) == 'alive'


@pytest.mark.parametrize('marker', ['NEED_INFO', 'NEED_DECISION'])
@pytest.mark.parametrize('stderr', ['', 'Error: invalid task\n',
                                   'Error: OAuth token expired\n',
                                   'Error: usage limit exhausted\n'])
def test_tf_r1_nonzero_question_keeps_question_in_the_runner_result(w, marker, stderr):
    node = start(w, {'text': f'{marker}(review): explain this?',
                     'stderr': stderr, 'exit': 1})
    held = w.wait_held(node, 'needs_info', timeout=WAIT)
    run_id = held['runs'][0]['run_id']
    record = json.loads((w.paths.run_dir(run_id) / 'result.json').read_text())
    assert record['exit_code'] == 1
    assert record['failure_cause']['class'] == 'question'
    assert len(held['runs']) == 1
