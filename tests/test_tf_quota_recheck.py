"""TF-R3: the existing scheduler clock drives the 30-minute quota recheck.

Budget readings use the established provider `script: ...` budget action;
its JSON file changes between ticks. No real cooldown elapses.
"""
import json
import sys
from datetime import datetime, timezone

import pytest

from nc_fixture.clock import ClockWorld
from nc_fixture.m4_world import M4World
from test_tf_support import WAIT, failures, failure_patterns, retry_success


class RecoveryClockWorld(ClockWorld, M4World):
    """Compose the existing fake-clock and queue-provider fixtures."""


@pytest.fixture
def clocked(tmp_path, monkeypatch):
    w = RecoveryClockWorld(tmp_path, monkeypatch, now=datetime.now(timezone.utc), tick_seconds=0.05)
    w.fxw = w.provider('fxw')
    w.agent('wk', 'fxw', writes=True)
    failures(w.fxw)
    # The interface's pattern block: this provider's usage-limit message and
    # reset spelling. No transient pattern, so nothing here is auto-retried.
    failure_patterns(w.fxw, transient=[])
    reading = budget_meter(w)
    yield w, reading
    w.close()


def budget_meter(w):
    """The established `script: ...` budget action, logging every read.

    The reading is re-read from `usage.json` on each call, so a test changes
    the account's usage by rewriting that file between ticks."""
    reading = w.fxw.dir / 'usage.json'
    reading.write_text(json.dumps({'known': True, 'headroom': 0.5}))
    action = w.fxw.dir / 'budget.py'
    action.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
reading = Path({str(reading)!r})
if sys.argv[1] == 'budget':
    with open({str(w.fxw.dir / 'budget-reads.jsonl')!r}, 'a') as out:
        out.write(reading.read_text() + '\\n')
    print(reading.read_text())
    sys.exit(0)
if sys.argv[1] == 'check':
    sys.exit(0)
sys.exit(64)
''')
    action.chmod(0o755)
    w.fxw.entry['script'] = str(action)
    script = w.fxw.dir / 'agent.py'
    source = script.read_text().replace('if fx.get("stderr"):', '''
if fx.get("exhaust"):
    _usage = {"known": True, "headroom": 0.0}
    if fx.get("exhaust_resets_at"):
        _usage["resets_at"] = fx["exhaust_resets_at"]
    open(%r, 'w').write(json.dumps(_usage))
if fx.get("stderr"):''' % str(reading))
    script.write_text(source)
    return reading


def budget_reads(w):
    path = w.fxw.dir / 'budget-reads.jsonl'
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.is_file() else []


def exhaust(w):
    w.fxw.queue({'exhaust': True, 'stderr': 'Error: weekly usage limit exhausted\n', 'exit': 1}, {})
    w.start_scheduler()
    node = w.simple('QUOTA', 'wk')
    w.wait_held(node, 'quota_exhausted', timeout=WAIT)
    return node


def test_tf_r3_unknown_reset_rechecks_at_thirty_minutes_and_resumes_session(clocked):
    w, reading = clocked
    node = exhaust(w)
    first = w.fxw.calls()[0]
    reading.write_text(json.dumps({'known': True, 'headroom': 0.9}))
    w.advance(seconds=1799)
    assert w.get(node)['state'] == 'held'
    assert len(w.fxw.calls()) == 1
    w.advance(seconds=1)
    retry_success(w, node)
    assert len(w.fxw.calls()) == 2
    assert w.fxw.calls()[1]['resume'] == first['session']


def test_tf_r3_due_recheck_does_not_release_account_still_exhausted(clocked):
    w, reading = clocked
    node = exhaust(w)
    w.advance(seconds=1800)
    assert w.get(node)['state'] == 'held'
    assert len(w.fxw.calls()) == 1
    # The next scheduled check must actually refresh usage, rather than
    # inferring success from expiry of a cooldown or a cached reading.
    reading.write_text(json.dumps({'known': True, 'headroom': 0.9}))
    w.advance(seconds=1800)
    retry_success(w, node)
    assert len(w.fxw.calls()) == 2


def test_tf_r3_the_configured_quota_recheck_interval_shortens_the_wait(clocked):
    """The interface's `scheduler.quota_recheck_seconds`: a project may set
    its own interval; the default 30 minutes must not be hard-wired."""
    w, reading = clocked
    w.project['scheduler']['quota_recheck_seconds'] = 90
    node = exhaust(w)
    reading.write_text(json.dumps({'known': True, 'headroom': 0.9}))
    w.advance(seconds=89)
    assert w.get(node)['state'] == 'held'
    assert len(w.fxw.calls()) == 1
    w.advance(seconds=1)
    retry_success(w, node)
    assert len(w.fxw.calls()) == 2
