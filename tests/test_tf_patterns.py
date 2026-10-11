"""TF-R1/TF-R3: failure classification and reset times come from the
provider's configured `failures:` patterns (the interface of 2026-10-10).

- a pattern list that is absent means no match: the same error text is
  `transient` on a provider that configures it and `other` on one that does
  not, so the words alone classify nothing;
- matching is case-insensitive;
- the reset time comes, in order, from a structured reset in the provider's
  result, the provider's `reset` pattern, and the account's usage window
  `resets_at` from the budget reading (source names are the contract's own;
  the structured field is assumed to be `resets_at`, the vocabulary the
  budget reading already uses).

Reset-timing tests drive the scheduler's clock file, never real time.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tf_support import (WAIT, w, failures, failure_patterns, start, settle,  # noqa: E402,F401
                             captured, cause, retry_success, reset_instants)
from test_tf_quota_recheck import RecoveryClockWorld, budget_meter, budget_reads

CODEX = ('workspace routing discovery failed', "You've hit your usage limit",
         'try again at')


def test_tf_r1_configured_transient_pattern_classifies_transient_and_retries(w):
    node = start(w, {'stderr': 'Error: TF wiring fault on node 7\n', 'exit': 1}, {})
    view = captured(w, node)
    cause(view, 'transient')
    done = retry_success(w, node)
    assert len(done['runs']) == 2


def test_tf_r1_transient_patterns_match_case_insensitively(w):
    node = start(w, {'stderr': 'error: tf wiring FAULT happened twice\n', 'exit': 1},
                 {'gate': 'ci'})
    try:
        cause(captured(w, node), 'transient')
    finally:
        w.gate('ci', w.fxw)


def test_tf_r1_an_absent_pattern_list_matches_nothing(w):
    """The same provider error words, configured on one provider only."""
    fxn = w.provider('fxn')
    failures(fxn)                       # directives, but no `failures:` block
    w.agent('nag', 'fxn', writes=True)
    words = {'stderr': 'Error: TF wiring fault\n', 'exit': 1}
    configured = start(w, words, {'gate': 'absent'})
    try:
        cause(captured(w, configured), 'transient')
    finally:
        w.gate('absent', w.fxw)
    w.fxw.queue(words)
    bare = w.simple('TF2', 'nag')
    view = settle(w, bare)
    cause(view, 'other')
    assert len(view['runs']) == 1, 'an unconfigured provider error is never retried'
    assert len(w.fxn.calls()) == 1


def test_tf_r1_quota_classification_comes_from_the_configured_pattern(w):
    """Not from the words: a custom quota phrase holds only where declared."""
    failure_patterns(w.fxr, quota=['TF kredits are op'])
    w.agent('kr', 'fxr', writes=True)
    w.fxr.queue({'stderr': 'Error: TF kredits are op this week\n', 'exit': 1})
    w.start_scheduler()
    node = w.simple('KR', 'kr')
    held = w.wait_held(node, 'quota_exhausted', timeout=WAIT)
    cause(held['hold']['detail'], 'quota')
    # The same words on the provider that does not configure them stay `other`.
    w.fxw.queue({'stderr': 'Error: TF kredits are op this week\n', 'exit': 1})
    plain = w.simple('KR2', 'wk')
    cause(settle(w, plain), 'other')
    assert len(w.fxw.calls()) == 1


def test_tf_r1_codex_default_failure_patterns_ship():
    from multiagents.paths import shipped_defaults_dir
    shipped = yaml.safe_load((shipped_defaults_dir() / 'providers.yaml').read_text())
    failures = shipped['providers']['codex'].get('failures') or {}
    assert 'workspace routing discovery failed' in ' '.join(failures.get('transient') or [])
    assert "You've hit your usage limit" in ' '.join(failures.get('quota') or [])
    reset = ' '.join(failures.get('reset') or [])
    assert 'try again at' in reset and '(?P<reset>' in reset


# ------------------------------------------------------- reset-time sources

@pytest.fixture
def resettable(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    w = RecoveryClockWorld(tmp_path, monkeypatch, now=datetime.now(timezone.utc),
                           tick_seconds=0.05)
    w.fxw = w.provider('fxw')
    w.agent('wk', 'fxw', writes=True)
    failures(w.fxw)
    failure_patterns(w.fxw, transient=[])
    # A structured quota result may carry the reset (source 1). The field is
    # assumed to be `resets_at`, the budget vocabulary's own name for it.
    w.fxw.entry['stream']['rules'].append(
        {'match': {'type': 'result'}, 'as': 'result',
         'fields': {'status': 'part.status', 'resets_at': 'part.resets_at'}})
    w.project['scheduler']['retries'] = {'backoff_seconds': [0.1]}
    reading = budget_meter(w)
    yield w, reading
    w.close()


def exhaust(w, **directives):
    w.fxw.queue({'exhaust': True, 'stderr': 'Error: weekly usage limit exhausted\n',
                 'exit': 1, **directives}, {})
    w.start_scheduler()
    node = w.simple('QUOTA', 'wk')
    return node, w.wait_held(node, 'quota_exhausted', timeout=WAIT)


def at(w, seconds):
    return (w.now + timedelta(seconds=seconds)).isoformat()


def test_tf_r3_reset_time_comes_from_the_configured_reset_pattern(resettable):
    w, reading = resettable
    announced = at(w, 600)
    window = at(w, 900)        # source 3 also names one; the pattern (2) wins
    node, held = exhaust(w, stderr='Error: weekly usage limit exhausted; '
                                   f'try again at {announced}\n',
                         exhaust_resets_at=window)
    from datetime import datetime
    found = reset_instants(held)
    assert datetime.fromisoformat(announced) in found, held
    assert datetime.fromisoformat(window) not in found, \
        f'the usage window outranked the provider\'s own announcement: {found}'
    # A reset time is a recheck time, not proof: no usage read before it...
    base = len(budget_reads(w))
    w.advance(seconds=599)
    assert len(budget_reads(w)) == base
    # ...and the account is read at/after it (still exhausted here: held on).
    w.advance(seconds=1)
    w.until(lambda: len(budget_reads(w)) > base, WAIT, what='recheck at the reset')
    assert w.get(node)['state'] == 'held'


def test_tf_r3_reset_time_falls_back_to_the_usage_window_resets_at(resettable):
    w, reading = resettable
    window = at(w, 900)
    node, held = exhaust(w, exhaust_resets_at=window)
    from datetime import datetime
    assert datetime.fromisoformat(window) in reset_instants(held), held
    base = len(budget_reads(w))
    w.advance(seconds=899)
    assert len(budget_reads(w)) == base
    w.advance(seconds=1)
    w.until(lambda: len(budget_reads(w)) > base, WAIT, what='recheck at the window reset')


def test_tf_r3_structured_reset_in_the_result_wins_over_the_pattern(resettable):
    w, reading = resettable
    structured, announced = at(w, 300), at(w, 700)
    node, held = exhaust(w, quota_result=structured,
                         stderr='Error: weekly usage limit exhausted; '
                                f'try again at {announced}\n')
    from datetime import datetime
    found = reset_instants(held)
    assert datetime.fromisoformat(structured) in found, held
    assert datetime.fromisoformat(announced) not in found, held
    base = len(budget_reads(w))
    w.advance(seconds=299)
    assert len(budget_reads(w)) == base
    w.advance(seconds=1)
    w.until(lambda: len(budget_reads(w)) > base, WAIT, what='recheck at the structured reset')


def test_tf_r3_reset_time_is_unknown_when_no_source_names_one(resettable):
    w, reading = resettable
    node, held = exhaust(w)
    assert reset_instants(held) == [], held
    # Unknown reset keeps the default recheck interval (TF-R3).
    reading.write_text(json.dumps({'known': True, 'headroom': 0.9}))
    w.advance(seconds=1799)
    assert w.get(node)['state'] == 'held'
    w.advance(seconds=1)
    retry_success(w, node)
