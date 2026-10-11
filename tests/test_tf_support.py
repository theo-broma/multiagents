"""TF directives for the existing RR/LR M4 provider, without a new harness.

Cause location is deliberately unspecified: find class-bearing cause objects
in the public JSON views. No retry storage schema is assumed.

The interface (context/specs/transient-failures.md, decided 2026-10-10) fixes
the configuration surface: providers.yaml `failures: {transient, quota, reset}`
pattern lists, project.yaml `scheduler.retries.{budget,backoff_seconds}` and
`scheduler.quota_recheck_seconds`, all timing on the scheduler's clock-file
clock. Tests configure their own patterns and backoffs and drive the clock
rather than wait, so no shipped value constrains them.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lr_loop_resume import make_world
from nc_fixture.agent import alive

WAIT = 4
SECRET = 'tf-secret-0123456789abcdef'

# The pattern lists the fixture providers classify with (case-insensitive).
# 'TF wiring fault' is deliberately not anything a real provider says, so the
# pattern tests prove configuration drives classification, not magic words.
PATTERNS = {'transient': ['TF wiring fault'],
            'quota': ['usage limit exhausted'],
            'reset': [r'try again at (?P<reset>\S+)']}


def failure_patterns(provider, **override):
    provider.entry['failures'] = {**PATTERNS, **override}


def failures(provider):
    script = provider.dir / 'agent.py'
    source = script.read_text()
    if 'tf-fx-effects' in source:          # idempotent: never patch twice
        return
    source = source.replace('"cost": 0,', '"cost": fx.get("cost", 0),')
    source = source.replace('sys.exit(fx.get("exit", 0))', '''
# tf-fx-effects
if fx.get("quota_result"):
    emit("result", {"status": "quota", "resets_at": fx["quota_result"]})
if fx.get("break_next_launch"):
    original_script = open(__file__).read()
    open(__file__, "w").write("#!/tf-missing-interpreter\\n" + original_script.split("\\n", 1)[1])
if fx.get("stderr"):
    sys.stderr.write(fx["stderr"]); sys.stderr.flush()
if fx.get("signal"):
    os.kill(os.getpid(), fx["signal"])
sys.exit(fx.get("exit", 0))''')
    script.write_text(source)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, threshold=100)
    world.project['scheduler']['tick_seconds'] = 0.05
    # The interface's timing keys: a short backoff so retries arrive in test
    # time. The budget stays at the shipped default (3) — TF-R2 pins it.
    world.project['scheduler']['retries'] = {'backoff_seconds': [0.1, 0.2, 0.3]}
    # make_world already applied the failure directives; the pattern lists say
    # which of this provider's errors are transient/quota and how a reset
    # time is read (the interface's `failures:` block in providers.yaml).
    failure_patterns(world.fxw)
    failure_patterns(world.fxr)
    world.fxw.entry['refusal_markers'] = ['TF content refused']
    yield world
    world.close()


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def cause(view, expected):
    found = [item for item in objects(view) if item.get('class') == expected]
    assert found, f'public view has no classified {expected} cause: {view}'
    return found[-1]


def field(record, *names):
    for name in names:
        if name in record:
            return record[name]
    raise AssertionError(f'cause is missing {names}: {record}')


def settle(w, node):
    return w.until(lambda: (n if n['state'] in {'done', 'held', 'cancelled'} else None)
                   if (n := w.get(node)) else None, WAIT, what='failure to settle')


def start(w, *directives, **fields):
    w.fxw.queue(*directives)
    w.start_scheduler()
    return w.simple('TF', 'wk', **fields)


def wait_attempts(w, node, count):
    """Bound each real launch/recovery separately, rather than their sum.

    Missing retry progress still fails within WAIT seconds. Loop children
    may finish a successful round between attempts, so `done` is permitted.
    """
    for expected in range(1, count + 1):
        w.until(lambda: (n if len(n['runs']) >= expected else None)
                if (n := w.get(node)) else None, WAIT,
                what=f'attempt {expected} of {count}')


def retry_success(w, node):
    def probe():
        n = w.get(node)
        if n['state'] == 'done':
            assert n['outcome'] == 'completed', f'retry lost: {n}'
            return n
        if n['state'] == 'held':
            raise AssertionError(f'retry unexpectedly held: {n}')
    return w.until(probe, WAIT, what='automatic retry success')


def captured(w, node):
    """A provider has ended and its classified capture is publicly visible.

    The runner's terminal status can precede scheduler capture by a tick;
    it alone is not evidence that `get_node` has integrated the cause.
    """
    def probe():
        n = w.get(node)
        calls = w.fxw.calls()
        ended = bool(n['runs']) and w.tree_nodes().get(n['runs'][0]['run_id'], {}).get('status') in {
            'failed', 'quota', 'rate_limited', 'refused', 'unauthenticated',
            'limited', 'awaiting_user', 'cancelled', 'discarded', 'quota_paused'}
        classified = any('class' in record for record in objects(n))
        if classified and calls and n['runs'] and (n.get('active_run') is None or len(n['runs']) > 1 or ended):
            return n
    return w.until(probe, WAIT, what='captured predecessor')


def pending_retry(w, node):
    """A retryable failure has been captured and the node is waiting for its
    retry: the failed attempt is released (no active run) and the node is
    non-terminal (TF-R2 — an automatic retry keeps the node alive)."""
    def probe():
        n = w.get(node)
        if w.fxw.calls() and n['runs'] and n.get('active_run') is None:
            return n
    view = w.until(probe, WAIT, what='the failed attempt to be released')
    assert view['state'] not in ('done', 'cancelled'), \
        f'a pending retry is not terminal: {view}'
    assert view['outcome'] is None, view
    return view


def instant(value):
    """A reset/reset_time value as a timezone-aware datetime, or None."""
    from datetime import datetime, timezone
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value).strip()
    try:
        out = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def reset_instants(view):
    """Every reset instant named in a public view, from any cause or hold
    field (`reset_time`/`reset_at`/`resets_at`) — the field name is not
    pinned by the contract."""
    found = []
    for item in objects(view):
        for key in ('reset_time', 'reset_at', 'resets_at'):
            if key in item:
                moment = instant(item[key])
                if moment is not None:
                    found.append(moment)
    return found
