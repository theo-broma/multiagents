"""TF-R2/R3: retries are bounded, recover work and preserve sessions."""
import signal
from pathlib import Path

import pytest

from test_tf_support import w, WAIT, start, settle, cause, retry_success
from test_lr_loop_resume import mkloop, work
from nc_fixture.m4_world import commit_entry, verdict_entry, finding
from nc_fixture.agent import alive

CRASH = {'exit': 1}  # nonzero after launch, no provider error text


def test_tf_r2_crash_once_then_success_resumes_same_session(w):
    node = start(w, CRASH, {})
    done = retry_success(w, node)
    assert len(done['runs']) == 2
    first, second = w.fxw.calls()
    assert not alive(first['pid'])
    assert second['resume'] == first['session']


def test_tf_r2_four_crashes_exhaust_three_retries_despite_successful_launches(w):
    node = start(w, *([CRASH] * 4), {'gate': 'must-not-launch'})
    try:
        held = w.wait_held(node, 'retries_exhausted', timeout=WAIT)
        assert len(held['runs']) == 4
        assert len(w.fxw.calls()) == 4
        cause(held['hold']['detail'], 'crash')
        w.restart_scheduler()
        assert w.get(node)['state'] == 'held'
        assert len(w.fxw.calls()) == 4
    finally:
        w.gate('must-not-launch', w.fxw)


def test_tf_r2_root_relaunch_resets_exhausted_budget(w):
    node = start(w, *([CRASH] * 4))
    w.wait_held(node, 'retries_exhausted', timeout=WAIT)
    w.fxw.queue(CRASH, {})
    assert w.root_op('relaunch_node', node).get('ok')
    done = retry_success(w, node)
    assert len(done['runs']) == 6


def test_tf_r2_retry_starts_from_committed_dead_work(w):
    node = start(w, commit_entry('tf-wip.txt', 'recovered\n', exit=1), {'gate': 'recovered'})
    try:
        def retried():
            n = w.get(node)
            if n['state'] in {'held', 'done'} and len(n['runs']) == 1:
                raise AssertionError(f'committed crash was not retried: {n}')
            return w.fxw.calls() if len(w.fxw.calls()) == 2 else None
        calls = w.until(retried, WAIT, what='recovered retry')
        assert (Path(calls[1]['cwd']) / 'tf-wip.txt').read_text() == 'recovered\n'
        first_attempt = w.get(node)['runs'][0]['attempt_id']
        captured = w.git('rev-parse', f'refs/heads/node-results/{first_attempt}').stdout.strip()
        assert calls[1]['head'] == captured
        assert not any(g.get('approved') for g in w.get(node)['generations'])
    finally:
        w.gate('recovered', w.fxw)


@pytest.mark.parametrize('kind,directive', [
    ('question', {'text': 'NEED_INFO(tf): need an answer?'}),
    ('auth', {'stderr': 'Error: OAuth token expired\n', 'exit': 1}),
    ('refusal', {'text': 'TF content refused', 'exit': 1}),
    ('other', {'stderr': 'Error: TF invalid task syntax\n', 'exit': 1}),
])
def test_tf_r2_never_retried_classes_do_not_launch_another_attempt(w, kind, directive):
    node = start(w, directive, {'gate': 'forbidden-retry'})
    try:
        view = settle(w, node)
        cause(view, kind)
        assert len(view['runs']) == 1
        assert len(w.fxw.calls()) == 1
        w.restart_scheduler()
        assert len(w.get(node)['runs']) == 1
    finally:
        w.gate('forbidden-retry', w.fxw)


def test_tf_r2_loop_crash_uses_shared_budget_without_advancing_round_or_sibling(w):
    w.fxw.queue(*([CRASH] * 4), {'gate': 'extra'})
    w.fxr.queue(verdict_entry('approved'))
    w.start_scheduler()
    loop, worker, reviewer = mkloop(w)
    try:
        held = w.wait_held(worker, 'retries_exhausted', timeout=WAIT)
        assert len(held['runs']) == 4, 'per-round retry must not stack with shared retries'
        assert w.get(loop)['loop']['rounds_rejected'] == 0
        assert w.get(loop)['loop']['max_rounds'] == 3
        assert not w.fxr.calls()
        assert w.get(reviewer)['state'] == 'open'
    finally:
        w.gate('extra', w.fxw)


def test_tf_r2_a_successful_round_does_not_restore_the_retry_budget(w):
    """TF-R2: "a successful launch does not restore it", and only a root
    relaunch or a change of pins/input resets the budget — a loop moving to
    its next round is neither. Crash, then a successful launch that finishes
    round 1, then three crashes in round 2: one shared budget across the
    whole activation, so the worker holds `retries_exhausted` on its fifth
    attempt, not the eighth."""
    w.fxw.queue(CRASH, work(1), CRASH, CRASH, CRASH)
    w.fxr.queue(verdict_entry('rejected', [finding('round 2 please')]))
    w.start_scheduler()
    loop, worker, reviewer = mkloop(w)
    held = w.wait_held(worker, 'retries_exhausted', timeout=WAIT)
    assert len(held['runs']) == 5, \
        'a successful launch or a new round restored the retry budget'
    assert len(w.fxw.calls()) == 5
    assert w.get(loop)['loop']['rounds_rejected'] == 1
    assert w.get(reviewer)['state'] == 'open', 'round 2 never reached a verdict'


def test_tf_r3_usage_limit_message_holds_instead_of_failing(w):
    node = start(w, {'stderr': 'Error: weekly usage limit exhausted\n', 'exit': 1})
    held = w.wait_held(node, 'quota_exhausted', timeout=WAIT)
    assert held['outcome'] is None
    cause(held['hold']['detail'], 'quota')
    w.restart_scheduler()
    assert w.get(node)['state'] == 'held'


def test_tf_r3_provider_error_words_in_ordinary_advice_do_not_hold(w):
    node = start(w, {'text': 'Discuss quota, usage limit, HTTP 429 and authentication.'})
    assert retry_success(w, node)['outcome'] == 'completed'
    assert len(w.fxw.calls()) == 1


def test_tf_r3_configured_fallback_launches_fresh_from_quota_work(w):
    w.agents['wk']['models'] = {'fxr': 'fxr/m1'}
    w.fxr.queue({'gate': 'fallback'})
    node = start(w, commit_entry('quota-wip.txt', 'kept\n',
                                stderr='Error: weekly usage limit exhausted\n', exit=1))
    try:
        def fallback():
            n = w.get(node)
            if n['state'] == 'done' and n['outcome'] == 'failed':
                raise AssertionError(f'quota failed instead of falling back: {n}')
            return w.fxr.calls() or None
        calls = w.until(fallback, WAIT, what='quota fallback')
        assert calls[0]['resume'] is None
        assert (Path(calls[0]['cwd']) / 'quota-wip.txt').read_text() == 'kept\n'
        w.gate('fallback', w.fxr)
        retry_success(w, node)
    finally:
        w.gate('fallback', w.fxr)


def test_tf_r2_cancelled_run_is_never_retried(w):
    from test_tf_support import captured
    node = start(w, {'gate': 'cancel'}, {'gate': 'extra'})
    try:
        w.until(lambda: w.fxw.calls(), WAIT, what='run to cancel')
        assert w.cancel(node).get('ok')
        cause(captured(w, node), 'stopped')
        assert len(w.fxw.calls()) == 1
        w.restart_scheduler()
        assert len(w.get(node)['runs']) == 1
    finally:
        w.gate('cancel', w.fxw)
        w.gate('extra', w.fxw)


def test_tf_r3_exhausting_one_account_leaves_its_sibling_usable(w):
    w.fxw.entry['family'] = 'tf-family'
    w.fxr.entry['family'] = 'tf-family'
    w.agent('sibling', 'fxr', writes=True)
    node = start(w, {'stderr': 'Error: weekly usage limit exhausted\n', 'exit': 1})
    w.wait_held(node, 'quota_exhausted', timeout=WAIT)
    w.fxr.queue({})
    sibling = w.simple('SIBLING', 'sibling')
    retry_success(w, sibling)
    assert len(w.fxr.calls()) == 1
    assert w.get(node)['state'] == 'held'


def test_tf_r2_spend_cap_is_never_retried(w):
    from test_tf_support import captured
    w.fxw.entry['spend_cap'] = {'usd': 0.1}
    node = start(w, {'cost': 1, 'gate': 'cap'}, {'gate': 'extra'})
    try:
        cause(captured(w, node), 'spend_cap')
        assert len(w.fxw.calls()) == 1
    finally:
        w.gate('cap', w.fxw)
        w.gate('extra', w.fxw)
