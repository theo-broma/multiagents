"""TF-R1: classified causes survive capture and are publicly readable."""
import signal

import pytest

from test_tf_support import w, WAIT, SECRET, start, settle, cause, field, captured


@pytest.mark.parametrize('kind,directive', [
    ('question', {'text': 'NEED_INFO(tf): explain this?'}),
    ('auth', {'stderr': 'Error: OAuth token expired\n', 'exit': 1}),
    ('refusal', {'text': 'TF content refused', 'exit': 1}),
    ('throttle', {'stderr': 'Error: HTTP 429 Too Many Requests\nRetry-After: 3600\n', 'exit': 1}),
    ('quota', {'stderr': 'Error: weekly usage limit exhausted\n', 'exit': 1}),
    ('crash', {'signal': signal.SIGUSR1}),
    ('other', {'stderr': 'Error: TF invalid task syntax\n', 'exit': 1}),
])
def test_tf_r1_classified_failure_is_visible_on_get_node(w, kind, directive):
    node = start(w, directive, {'gate': 'retry'})
    try:
        # For retryable/pending classes, inspect as soon as the predecessor
        # has been captured; don't wait for a retry's unspecified backoff.
        view = captured(w, node)
        c = cause(view, kind)
        assert c['provider'] == 'fxw' and c['model'] == 'fxw/m1'
        field(c, 'account', 'account_id')
        code = field(c, 'exit_code', 'exit')
        sig = field(c, 'signal')
        field(c, 'reset_time', 'reset_at', 'resets_at')
        if kind == 'crash':
            assert sig in {signal.SIGUSR1, 'SIGUSR1', 'USR1'}
        elif directive.get('exit') == 1:
            assert code == 1
            assert sig is None
        assert len(field(c, 'excerpt', 'error_excerpt')) <= 2000
    finally:
        w.gate('retry', w.fxw)


def test_tf_r1_cancelled_attempt_records_stopped(w):
    node = start(w, {'gate': 'running'})
    try:
        w.until(lambda: w.fxw.calls(), WAIT, what='provider running')
        assert w.cancel(node).get('ok')
        cause(captured(w, node), 'stopped')
    finally:
        w.gate('running', w.fxw)


def test_tf_r1_question_precedes_quota_and_refusal(w):
    w.fxw.entry['refusal_markers'] = [r'.*TF content refused.*']
    node = start(w, {'text': 'NEED_DECISION(tf): TF content refused; choose?',
                     'stderr': 'Error: usage limit exhausted\n', 'exit': 1})
    view = captured(w, node)
    cause(view, 'question')
    assert view['state'] == 'held'


@pytest.mark.parametrize('size', [0, 1999, 2000, 2001, 5000])
def test_tf_r1_error_excerpt_is_bounded_and_redacted_before_persistence(w, size):
    node = start(w, {'stderr': 'Error: api_key=' + SECRET + ' ' + 'x' * size, 'exit': 1})
    view = settle(w, node)
    c = cause(view, 'other')
    excerpt = field(c, 'excerpt', 'error_excerpt')
    assert len(excerpt) <= 2000
    assert SECRET not in str(view)
    assert 'Error:' in excerpt
    w.restart_scheduler()
    assert cause(w.get(node), 'other') == c


def test_tf_r1_check_and_collect_show_the_same_cause(w, monkeypatch):
    from multiagents.config import load
    from multiagents.runner import Runner
    node = start(w, {'stderr': 'Error: TF invalid task syntax\n', 'exit': 1})
    view = settle(w, node)
    c = cause(view, 'other')
    run_id = view['runs'][-1]['run_id']
    reader = Runner(w.paths, load(w.paths, seed=False))
    assert cause(reader.check(run_id), 'other') == c
    assert cause(reader.collect(run_id), 'other') == c


def test_tf_r1_spend_cap_is_classified_and_never_retried(w):
    w.fxw.entry['spend_cap'] = {'usd': 0.1}
    node = start(w, {'cost': 1, 'gate': 'cap'}, {'gate': 'extra'})
    try:
        view = captured(w, node)
        cause(view, 'spend_cap')
        assert len(w.fxw.calls()) == 1
    finally:
        w.gate('cap', w.fxw)
        w.gate('extra', w.fxw)
