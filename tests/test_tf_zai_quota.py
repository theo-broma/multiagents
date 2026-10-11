"""TF-R1/TF-R3: the opencode-zai quota message observed on 2026-10-10.

A real run died on an HTTP 429 error event whose text was a usage-limit
message:

    Usage limit reached for 5 hour. Your limit will reset at
    2026-10-11 06:02:35

That must classify `quota`, never `throttle` (TF-R1: throttle is a 429 with a
retry delay; this one announces a reset, not a delay), the node holds
`quota_exhausted` instead of failing, and the node stays relaunchable: a root
relaunch after the hold is not refused as though the quota attempt were still
active.

The provider here declares the observed message in its own `failures:`
pattern lists (the interface of 2026-10-10), which is how a provider opts its
messages into classification. The reset "2026-10-11 06:02:35" carries no
timezone; it is read as UTC, the surface every other reset is shown in.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tf_support import (WAIT, w, failure_patterns, start, cause,  # noqa: E402
                              objects, reset_instants, retry_success)

# The observed message, verbatim; the 429 is the event's status, the text is
# the provider's own usage-limit wording.
ZAI_QUOTA_STDERR = ('Error: HTTP 429\n'
                    'Usage limit reached for 5 hour. '
                    'Your limit will reset at 2026-10-11 06:02:35\n')
ZAI_RESET = datetime(2026, 10, 11, 6, 2, 35, tzinfo=timezone.utc)
ZAI_QUOTA = {'stderr': ZAI_QUOTA_STDERR, 'exit': 1}


@pytest.fixture
def zai(w):
    """The fixture provider declaring the observed message, as the zai entry
    would: its quota wording and its reset spelling."""
    failure_patterns(w.fxw,
                     quota=['Usage limit reached'],
                     reset=[r'Your limit will reset at (?P<reset>.+)'])
    return w


def classes(view):
    return {c.get('class') for c in objects(view) if isinstance(c, dict) and c.get('class')}


def quota_hold(w, node):
    """Held `quota_exhausted` — failing at once when the message took any
    other path (the 429 suspend-and-retry of the throttle, a failure, another
    hold), rather than waiting out the bound."""
    def probe():
        n = w.get(node)
        if n['state'] == 'held':
            reason = (n.get('hold') or {}).get('reason')
            if reason == 'quota_exhausted':
                return n
            raise AssertionError(f'{node} held for {reason!r}, not quota_exhausted')
        if n['state'] == 'suspended':
            raise AssertionError(
                f'{node} took the 429 throttle path (suspended) instead of the quota hold')
        if n['state'] in ('done', 'cancelled'):
            raise AssertionError(f'{node} reached {n["state"]}: the exhaustion was not held')
        if n['runs']:
            status = w.tree_nodes().get(n['runs'][0]['run_id'], {}).get('status')
            if status == 'rate_limited':
                raise AssertionError(
                    'the usage-limit 429 was captured as a rate limit (throttle path), '
                    f'not a quota exhaustion: run status {status!r}')
        return None
    return w.until(probe, WAIT, what=f'{node} held for quota_exhausted')


def test_tf_r1_the_observed_zai_quota_429_classifies_quota_not_throttle(zai):
    w = zai
    node = start(w, ZAI_QUOTA)
    held = quota_hold(w, node)
    assert held['outcome'] is None, 'an exhaustion failed the node instead of holding it'
    cause(held['hold']['detail'], 'quota')
    assert 'throttle' not in classes(held), \
        f'a usage-limit 429 with no retry delay took the throttle path: {classes(held)}'
    assert ZAI_RESET in reset_instants(held), \
        f'the announced reset is not in the hold: {held["hold"]}'
    assert len(w.fxw.calls()) == 1, 'a quota exhaustion was retried automatically'
    w.restart_scheduler()
    assert w.get(node)['state'] == 'held'
    assert len(w.fxw.calls()) == 1


def test_tf_r3_a_root_relaunch_after_the_quota_hold_is_not_refused_as_active(zai):
    w = zai
    node = start(w, ZAI_QUOTA)
    quota_hold(w, node)
    w.fxw.queue({})
    reply = w.root_op('relaunch_node', node)
    assert reply.get('ok') is True, \
        f'a held quota node must stay relaunchable, not be refused: {reply}'
    done = retry_success(w, node)
    assert done['outcome'] == 'completed'
    assert len(done['runs']) == 2
