"""TF-R2/TF-R3 timing and precedence, on the scheduler's clock seam.

Backoff, the repeating last backoff value, the configured budget and the
configured quota recheck interval are all driven by moving the clock file
(`--clock-file`), never by waiting. The same seam parks a retry "pending"
(one crash, a backoff far in the future) so a question, a cancel, a root
relaunch, a close, or an unestablishable predecessor liveness can compete
with it — each must win and cancel the retry.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tf_support import WAIT, failures, retry_success, pending_retry
from nc_fixture.clock import ClockWorld
from nc_fixture.m4_world import M4World

CRASH = {'exit': 1}  # nonzero after launch, no provider error text: class crash


class RetryClockWorld(ClockWorld, M4World):
    """The fake clock composed with the queue provider world."""


@pytest.fixture
def clocked(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    w = RetryClockWorld(tmp_path, monkeypatch, now=datetime.now(timezone.utc),
                        tick_seconds=0.05)
    w.fxw = w.provider('fxw')
    w.agent('wk', 'fxw', writes=True)
    failures(w.fxw)
    yield w
    w.close()


def begin(w, *directives, **scheduler):
    w.project['scheduler'].update(scheduler)
    w.fxw.queue(*directives)
    w.start_scheduler()
    node = w.simple('TF', 'wk')
    return node, pending_retry(w, node)


def calls(w, count, what):
    return w.until(lambda: len(w.fxw.calls()) >= count or None, WAIT, what=what)


# ------------------------------------------------------------------ backoff

def test_tf_r2_a_retry_waits_for_the_configured_backoff(clocked):
    w = clocked
    node, _ = begin(w, CRASH, {}, retries={'backoff_seconds': [60]})
    w.advance(seconds=59)
    assert len(w.fxw.calls()) == 1, 'a retry launched before its backoff elapsed'
    w.advance(seconds=1)
    done = retry_success(w, node)
    assert len(done['runs']) == 2


def test_tf_r2_the_last_backoff_value_repeats(clocked):
    w = clocked
    node, _ = begin(w, *([CRASH] * 6), retries={'budget': 5, 'backoff_seconds': [60, 120]})
    steps = [(59, 1), (1, 2), (119, 2), (1, 3), (120, 4), (120, 5), (120, 6)]
    for seconds, expected in steps:
        w.advance(seconds=seconds)
        if expected > len(w.fxw.calls()):
            calls(w, expected, f'{expected} attempts after +{seconds}s')
        assert len(w.fxw.calls()) == expected, (seconds, len(w.fxw.calls()))
    held = w.wait_held(node, 'retries_exhausted', timeout=WAIT)
    assert len(held['runs']) == 6
    w.advance(seconds=600)
    assert len(w.fxw.calls()) == 6, 'a spent budget must not buy a seventh attempt'


def test_tf_r2_the_configured_budget_bounds_the_retries(clocked):
    w = clocked
    node, _ = begin(w, *([CRASH] * 3), retries={'budget': 1, 'backoff_seconds': [10]})
    held = w.wait_held(node, 'retries_exhausted', timeout=WAIT)
    assert len(held['runs']) == 2
    assert len(w.fxw.calls()) == 2
    w.advance(seconds=120)
    assert len(w.fxw.calls()) == 2


# ------------------------------------------------- what beats a pending retry

def test_tf_r2_a_cancel_during_a_pending_retry_cancels_it(clocked):
    w = clocked
    node, _ = begin(w, CRASH, retries={'backoff_seconds': [3600]})
    assert w.cancel(node).get('ok')
    w.until(lambda: w.get(node)['state'] == 'cancelled' or None, WAIT,
            what='the node to be cancelled')
    w.advance(seconds=7200)
    assert len(w.fxw.calls()) == 1, 'a cancelled pending retry launched anyway'
    assert w.get(node)['state'] == 'cancelled'


def test_tf_r2_a_root_relaunch_during_a_pending_retry_replaces_it(clocked):
    w = clocked
    node, _ = begin(w, CRASH, {}, retries={'backoff_seconds': [3600]})
    assert w.root_op('relaunch_node', node).get('ok'), \
        'a root relaunch must win over a pending retry'
    w.advance(seconds=7200)
    done = retry_success(w, node)
    assert done['outcome'] == 'completed'
    assert len(w.fxw.calls()) == 2, 'the pending retry stacked onto the relaunch'


def test_tf_r2_a_close_during_a_pending_retry_ends_the_node(clocked):
    w = clocked
    node, _ = begin(w, CRASH, retries={'backoff_seconds': [3600]})
    assert w.root_op('close_node', node, outcome='failed').get('ok'), \
        'a close must win over a pending retry'
    w.advance(seconds=7200)
    view = w.get(node)
    assert view['state'] == 'done' and view['outcome'] == 'failed'
    assert len(w.fxw.calls()) == 1


def test_tf_r2_a_question_during_a_pending_retry_wins_and_cancels_it(clocked):
    w = clocked
    w.project['scheduler'].update(retries={'backoff_seconds': [3600]})
    w.fxw.queue({'text': 'NEED_DECISION(tf): which way should this go?',
                 'exit': 1})
    w.start_scheduler()
    node = w.simple('TF', 'wk')
    view = pending_retry(w, node)
    held = w.wait_held(node, 'needs_info', timeout=WAIT)
    assert held['outcome'] is None
    w.advance(seconds=7200)
    assert len(w.fxw.calls()) == 1, 'a pending retry launched past a question'


# ------------------------------------------------------- predecessor liveness

def test_tf_r2_unestablishable_predecessor_liveness_holds_liveness_unknown(clocked):
    """The scheduler that must decide the retry never supervised the crashed
    predecessor's death (it was restarted after the crash), and the run's
    pid evidence is unreadable — so death cannot be confirmed and the node is
    held `liveness_unknown` instead of retried."""
    w = clocked
    node, _ = begin(w, CRASH, {}, retries={'backoff_seconds': [3600]})
    run_id = w.get(node)['runs'][0]['run_id']
    w.kill9()
    run_dir = w.paths.run_dir(run_id)
    for name in ('wrapper.pid', 'wrapper.pid.start', 'exit_status'):
        (run_dir / name).unlink(missing_ok=True)
    w.start_scheduler()
    held = w.wait_held(node, 'liveness_unknown', timeout=WAIT)
    assert held['outcome'] is None
    w.advance(seconds=7200)
    assert w.get(node)['state'] == 'held', 'an unconfirmed predecessor was retried'
    assert len(w.fxw.calls()) == 1
    w.restart_scheduler()
    assert w.get(node)['state'] == 'held'
    assert len(w.fxw.calls()) == 1
