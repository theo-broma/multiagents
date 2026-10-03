"""Characterization of C2's quota reading and failover — budget.py.

Pins what `read_provider`, `read_all`, `choose_provider`, `pick_instance`,
`reserved_providers`, `resets_soon`, `Budget`, and the claude-profile isolation
helpers do TODAY, including the parts that look wrong. Findings are recorded in
`context/review/C2-budget.md` as F120+; each is cross-referenced here by id in
the test that pins it.

`budget._cache` is a bare module dict nothing resets between tests (F100) —
every test below that reads through the cache calls `h.invalidate_cache()`
itself on the way in.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402

Budget = h.Budget


def mkbudget(**kw):
    kw.setdefault("provider", "x")
    kw.setdefault("known", True)
    return Budget(**kw)


# ===========================================================================
# 1. Budget.usable — the property everything else is built on
# ===========================================================================

def test_usable_known_headroom_exactly_at_the_2pc_floor_is_not_usable():
    # 0.02 itself is excluded (`> 0.02`, not `>=`); one thousandth more passes.
    assert mkbudget(headroom=0.02).usable is False
    assert mkbudget(headroom=0.021).usable is True


def test_usable_known_true_but_headroom_none_is_usable():
    # "known" without a headroom number falls through the known+headroom
    # branch entirely and lands on the same `return True` as a genuinely
    # unknown budget — known=True conveys nothing on its own here.
    assert mkbudget(known=True, headroom=None).usable is True


def test_usable_unknown_headroom_is_usable():
    assert mkbudget(known=False, headroom=None).usable is True


def test_usable_future_cooldown_overrides_high_headroom():
    b = mkbudget(headroom=0.9, cooldown_until=time.time() + 10)
    assert b.usable is False


def test_usable_past_cooldown_does_not_block_a_healthy_reading():
    b = mkbudget(headroom=0.9, cooldown_until=time.time() - 10)
    assert b.usable is True


def test_usable_past_cooldown_still_defers_to_headroom_for_an_exhausted_reading():
    b = mkbudget(headroom=0.0, cooldown_until=time.time() - 10)
    assert b.usable is False


# ===========================================================================
# 2. read_provider / _from_script — parsing a script's raw budget output
# ===========================================================================

def test_read_provider_parses_known_headroom_and_derives_severity(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh",
                  'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert (b.known, b.headroom, b.severity, b.source) == (True, 0.5, "normal", "script")


def test_read_provider_empty_stdout_parses_as_empty_object_not_an_error(tmp_path):
    # `json.loads(out.strip() or "{}")` treats a blank body as `{}`, which is
    # NOT the same code path as invalid JSON — no explanatory note is set.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf ""; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert (b.known, b.headroom, b.severity, b.note) == (False, None, "unknown", "")


def test_read_provider_missing_fields_object_also_has_no_note(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf "{}"; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert (b.known, b.headroom, b.severity, b.note) == (False, None, "unknown", "")


def test_read_provider_non_json_stdout_is_reported_with_a_note(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf "not json at all"; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.known is False
    assert b.note == "budget action did not print valid JSON"


def test_read_provider_json_array_is_rejected_as_a_non_object(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf "[1,2,3]"; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.known is False
    assert b.note == "budget action printed a non-object"


def test_read_provider_ignores_unknown_extra_fields(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(
        tmp_path, "p.sh",
        'budget) printf \'{"known": true, "headroom": 0.5, "bogus_field": 123}\'; exit 0 ;;',
    )
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.known is True and b.headroom == 0.5
    assert not hasattr(b, "bogus_field")


def test_read_provider_headroom_above_1_is_accepted_unclamped_and_reads_as_normal(tmp_path):
    # F123: no range validation on the script's own "headroom" number. 1.5
    # implies -50% used, which the severity thresholds read as "normal".
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 1.5}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.headroom == 1.5
    assert b.severity == "normal"


def test_read_provider_headroom_below_0_is_accepted_unclamped_and_reads_as_critical(tmp_path):
    # F123, the other direction: -0.5 implies 150% used, read as "critical".
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": -0.5}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.headroom == -0.5
    assert b.severity == "critical"


def test_read_provider_reset_time_already_in_the_past_is_corrected(tmp_path):
    # Pinned the pre-QF behaviour (a past resets_at carried through verbatim,
    # still critical) until QF-R1 (context/specs/quota-freshness.md): a window
    # past its reset by more than the margin does not count. A script's own
    # reading is already the fresh read, so the fallback applies, and with
    # every window past its reset that is headroom 1.0, resets_at none.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(
        tmp_path, "p.sh",
        'budget) printf \'{"known": true, "headroom": 0.0, "resets_at": "2020-01-01T00:00:00+00:00"}\'; exit 0 ;;',
    )
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.resets_at is None
    assert b.headroom == 1.0
    assert b.severity == "normal"


def test_read_provider_nonzero_exit_reports_stderr_over_stdout(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) echo "on stdout"; echo "on stderr" >&2; exit 3 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.known is False
    assert b.note == "on stderr"


def test_read_provider_nonzero_exit_falls_back_to_stdout_when_stderr_is_empty(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) echo "boom-stdout"; exit 3 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.known is False
    assert b.note == "boom-stdout"


def test_read_provider_exit_64_with_no_builtin_reader_reports_unimplemented(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")   # name "p" is not in budget._BUILTIN
    h.case_script(tmp_path, "p.sh", "budget) exit 64 ;;")
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert (b.known, b.source, b.note) == (False, "none", "no budget action and no built-in reader")


def test_read_provider_exit_127_is_treated_identically_to_unimplemented(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", "budget) exit 127 ;;")
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert (b.known, b.source, b.note) == (False, "none", "no budget action and no built-in reader")


def test_read_provider_unimplemented_falls_back_to_opencodes_builtin_with_spent_applied(tmp_path):
    # Contrast with the claude case below: opencode's and agy's builtins DO
    # receive `spent` on the unimplemented-script path.
    h.invalidate_cache()
    provider = h.make_provider("opencode")
    h.case_script(tmp_path, "opencode.sh", "budget) exit 64 ;;")
    b = h.read_provider("opencode", provider, h.FakeExecutor(), tmp_path,
                        spent={"tokens": 500}, use_cache=False)
    assert b.known is False
    assert b.source == "auth.json + tree accounting"
    assert b.spent == {"tokens": 500}


# ===========================================================================
# 3. F121 — the claude builtin fallback drops config_dir entirely
# ===========================================================================
#
# claude.sh's own `budget)` arm (src/multiagents/defaults/providers/claude.sh)
# ALWAYS exits 64 by design ("Deliberately unimplemented ... falls back to its
# built-in reader when a script returns 64"). That means every real
# `read_provider("claude", ...)` call in production goes through this branch.
# `read_provider`'s dispatch for it is `budget = builtin() if builtin is
# read_claude else ...` — called with NO arguments, so `config_dir` (the
# instance's CLAUDE_CONFIG_DIR that `read_claude` exists specifically to
# accept — see budget.py's own docstring on `read_claude`) never reaches it.
# A grep of the whole src tree confirms `read_claude(` has no other call site
# than its own definition: the config_dir parameter is unreachable from any
# real caller today.

def test_claude_builtin_fallback_ignores_the_callers_config_dir(tmp_path, monkeypatch):
    h.invalidate_cache()
    real_dir = h.claude_profile_dir(
        tmp_path / "real-account", access_token="tok",
        cached_usage={"limits": [{"percent": 10.0, "resets_at": "2099-01-01T00:00:00+00:00"}]},
    )
    # A decoy home-shaped location standing in for Path.home() / CLAUDE_STATE,
    # which is what read_claude() actually consults when called bare. Neither
    # file exists here, so if config_dir were honoured we'd see known=True
    # (10% used, from real_dir); if it is ignored we see known=False instead.
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    monkeypatch.setattr(h.budget_mod, "CLAUDE_STATE", decoy / ".claude.json")
    monkeypatch.setattr(h.budget_mod, "CLAUDE_CREDENTIALS", decoy / ".credentials.json")

    provider = h.make_provider("claude")
    h.case_script(real_dir, "claude.sh", "budget) exit 64 ;;")
    b = h.read_provider("claude", provider, h.FakeExecutor(), real_dir, use_cache=False)

    assert b.known is False
    assert "credentials are missing or expired" in b.note


def test_a_second_named_claude_account_reads_its_own_profile_through_extends(tmp_path, monkeypatch):
    # CB, 2026-10-02: reader inherited through extends
    # The built-in reader is resolved through `extends`, and called with the
    # instance's own profile directory (the variable named by the provider's
    # `budget_profile_env`, read from the instance's resolved env) -- never the
    # base account's, and never the home fallback.
    h.invalidate_cache()
    own = h.claude_profile_dir(
        tmp_path / "work", access_token="tok",
        cached_usage={"limits": [{"percent": 10.0, "resets_at": "2099-01-01T00:00:00+00:00"}]},
    )
    other = h.claude_profile_dir(
        tmp_path / "base", access_token="tok",
        cached_usage={"limits": [{"percent": 90.0, "resets_at": "2099-01-01T00:00:00+00:00"}]},
    )
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    monkeypatch.setattr(h.budget_mod, "CLAUDE_STATE", decoy / ".claude.json")
    monkeypatch.setattr(h.budget_mod, "CLAUDE_CREDENTIALS", decoy / ".credentials.json")
    providers = h.load_providers({
        "claude": {"bin": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR",
                   "env": {"CLAUDE_CONFIG_DIR": str(other)}},
        "claude-work": {"extends": "claude", "env": {"CLAUDE_CONFIG_DIR": str(own)}},
    })
    h.case_script(tmp_path, "claude.sh", "budget) exit 64 ;;")   # inherited script name
    b = h.read_provider("claude-work", providers["claude-work"], h.FakeExecutor(),
                        tmp_path, use_cache=False, providers=providers)
    assert b.known is True
    assert b.headroom == pytest.approx(0.9)   # its 10% used, not the base's 90%


# ===========================================================================
# 4. F120 — caching: TTL, invalidate_cache, and the spent merge/overwrite split
# ===========================================================================

def test_cache_hit_serves_without_rerunning_the_script(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(
        tmp_path, "p.sh",
        f'budget) echo -n x >> "{calls}"; printf \'{{"known": true, "headroom": 0.3}}\'; exit 0 ;;',
    )
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "x"


def test_use_cache_false_always_reruns_the_script(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(
        tmp_path, "p.sh",
        f'budget) echo -n x >> "{calls}"; printf \'{{"known": true, "headroom": 0.3}}\'; exit 0 ;;',
    )
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert calls.read_text() == "xx"


def test_cache_entry_expires_after_the_ttl_elapses(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(
        tmp_path, "p.sh",
        f'budget) echo -n x >> "{calls}"; printf \'{{"known": true, "headroom": 0.3}}\'; exit 0 ;;',
    )
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "x"
    # Backdate the cached entry past _CACHE_TTL without sleeping for it.
    ts, cached_budget = h.budget_mod._cache["p"]
    h.budget_mod._cache["p"] = (ts - h.budget_mod._CACHE_TTL - 1, cached_budget)
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "xx"


def test_cache_distinguishes_two_config_dirs_for_one_provider_name(tmp_path):
    # Was a narrower pin of the same root cause as F100
    # (context/review/C2-provider.md): the cache was keyed on provider name
    # alone, so dir_b's read returned dir_a's cached Budget and dir_b's script
    # never ran. F100 was fixed under context/specs/phase1-budget-cache.md
    # (R10), so this fact changed and the assertion is deliberately inverted —
    # same two config_dirs, same two scripts, opposite expectation. The name
    # now states what is true: one provider name, two config_dirs, two
    # answers.
    h.invalidate_cache()
    provider = h.make_provider("p")
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    h.case_script(dir_a, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.9}\'; exit 0 ;;')
    h.case_script(dir_b, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.1}\'; exit 0 ;;')
    first = h.read_provider("p", provider, h.FakeExecutor(), dir_a)
    second = h.read_provider("p", provider, h.FakeExecutor(), dir_b)
    assert first.headroom == 0.9
    assert second.headroom == 0.1        # dir_b's own script ran
    h.invalidate_cache()


def test_cache_hit_merges_spent_onto_a_copy_and_leaves_the_cached_object_alone(tmp_path):
    # F122 (context/review/C2-budget.md) and F150
    # (context/review/C2-budget-adversary.md). **This inversion is
    # deliberate.** The test was called
    # `test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`
    # and pinned F122's defect — a cache hit doing
    # `budget.spent = spent or budget.spent` on the very object sitting in
    # `_cache`, so one caller's spend dict permanently erased another's — under
    # a name that read like an intended invariant. F150 is that hazard itself:
    # whoever fixed F122 would have seen this go red and reverted the fix.
    # context/specs/phase3-cache-aliasing.md (R16, R17) makes the opposite
    # true, so the assertions are inverted and the name states what is now
    # true. Kept rather than deleted, because what it pinned is still a
    # behaviour worth holding — from the other side.
    #
    # (Its original comment cited F120. F120 is the discarded `config_dir`,
    # a different defect in the same file; the finding this pins is F122.)
    #
    # The exact merged dicts are pinned in tests/test_phase3_cache_aliasing.py,
    # where the reader contributes spend of its own. Here the script reports
    # none, so this asserts only what does not depend on whether a caller's
    # `spent` is itself kept in the cache entry — see that file's NEED_INFO.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')

    first = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"a": 1})
    assert first.spent == {"a": 1}

    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"b": 2})
    assert second is not first            # R16: never the cache's own object
    assert second.spent["b"] == 2         # the caller's own figures reach its own result

    third = h.read_provider("p", provider, h.FakeExecutor(), tmp_path)   # no spent at all
    assert "b" not in third.spent         # R17: a later call inherits no caller's spent


def test_fresh_read_merges_spent_rather_than_replacing_it(tmp_path):
    # The contrasting, correct-looking half of the same code: on an actual
    # (non-cached) read, spent passed in is merged over whatever the script
    # itself may have reported, not swapped in wholesale.
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"a": 1}, use_cache=False)
    assert b.spent == {"a": 1}


def test_invalidate_cache_clears_every_provider(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("p")
    calls = tmp_path / "calls"
    h.case_script(
        tmp_path, "p.sh",
        f'budget) echo -n x >> "{calls}"; printf \'{{"known": true, "headroom": 0.3}}\'; exit 0 ;;',
    )
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    h.invalidate_cache()
    h.read_provider("p", provider, h.FakeExecutor(), tmp_path)
    assert calls.read_text() == "xx"


# ===========================================================================
# 5. read_all — driving the providers map, disabled skip, cooldown overlay
# ===========================================================================

class _LocalExecutor:
    kind = "local"


def test_read_all_skips_a_disabled_provider(tmp_path):
    h.invalidate_cache()
    enabled = h.make_provider("a", enabled=True)
    disabled = h.make_provider("b", enabled=False)
    h.case_script(tmp_path, "a.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    out = h.read_all(providers={"a": enabled, "b": disabled},
                     executor_for=lambda n: _LocalExecutor(), config_dir=tmp_path,
                     use_cache=False)
    assert set(out) == {"a"}


def test_read_all_applies_an_active_cooldown_over_a_healthy_reading(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("a")
    h.case_script(tmp_path, "a.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    until = time.time() + 100
    out = h.read_all(providers={"a": provider}, executor_for=lambda n: _LocalExecutor(),
                     config_dir=tmp_path, use_cache=False,
                     cooldowns={"a": {"until": until, "reason": "rate limited"}})
    b = out["a"]
    assert b.cooldown_until == until
    assert b.severity == "critical"       # forced, despite headroom=0.5 reading "normal" alone
    assert b.note == "rate limited"
    assert b.headroom == 0.5              # the underlying reading is untouched


def test_read_all_ignores_an_expired_cooldown(tmp_path):
    h.invalidate_cache()
    provider = h.make_provider("a")
    h.case_script(tmp_path, "a.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')
    out = h.read_all(providers={"a": provider}, executor_for=lambda n: _LocalExecutor(),
                     config_dir=tmp_path, use_cache=False,
                     cooldowns={"a": {"until": time.time() - 10, "reason": "stale"}})
    b = out["a"]
    assert b.cooldown_until is None
    assert b.severity == "normal"


# ===========================================================================
# 6. read_claude — the claude-profile isolation helpers
# ===========================================================================

def test_read_claude_empty_profile_reports_run_claude_code_once(tmp_path):
    profile = h.claude_profile_dir(tmp_path)
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)
    assert b.known is False
    assert profile.name in b.note or "unreadable" in b.note


def test_read_claude_fresh_cache_is_used_without_fetching(tmp_path):
    profile = h.claude_profile_dir(
        tmp_path, cached_usage={"limits": [{"percent": 20.0, "resets_at": "2099-01-01T00:00:00+00:00"}]},
    )
    b = h.budget_mod.read_claude(fetch=True, config_dir=profile)
    assert b.known is True
    assert b.headroom == 0.8
    assert b.source == "cachedUsageUtilization"    # never switched to the fetch source
    assert b.resets_at == "2099-01-01T00:00:00+00:00"


def test_read_claude_stale_cache_triggers_a_fetch_and_switches_source(tmp_path, monkeypatch):
    profile = h.claude_profile_dir(tmp_path, cached_usage={"limits": [{"percent": 20.0}]})
    import json
    data = json.loads((profile / ".claude.json").read_text())
    data["cachedUsageUtilization"]["fetchedAtMs"] = (time.time() - 2000) * 1000   # > STALE_AFTER
    (profile / ".claude.json").write_text(json.dumps(data))

    h.fake_claude_fetch(monkeypatch, {"limits": [{"percent": 55.0, "resets_at": "2030-01-01T00:00:00+00:00"}]})
    b = h.budget_mod.read_claude(fetch=True, config_dir=profile)
    assert b.headroom == pytest.approx(0.45)
    assert b.source == "api/oauth/usage"
    assert b.stale_seconds == 0.0


def test_read_claude_expired_token_short_circuits_without_a_network_call(tmp_path):
    # No fake_claude_fetch installed here at all — if this reached the network
    # path it would try a real HTTPS request. It doesn't: the expired-token
    # check inside fetch_claude_usage -> _claude_token returns None first.
    profile = h.claude_profile_dir(tmp_path, access_token="tok",
                                   expires_at_ms=(time.time() - 1000) * 1000)
    b = h.budget_mod.read_claude(fetch=True, config_dir=profile)
    assert b.known is False
    assert "credentials are missing or expired" in b.note


def test_read_claude_picks_the_worst_of_multiple_limit_buckets(tmp_path):
    profile = h.claude_profile_dir(tmp_path, cached_usage={"limits": [
        {"percent": 10.0, "resets_at": "A"},
        {"percent": 80.0, "resets_at": "B"},
        {"percent": 40.0, "resets_at": "C"},
    ]})
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)
    assert b.headroom == pytest.approx(0.2)
    assert b.resets_at == "B"             # the bucket the worst percent came from
    assert b.severity == "warning"


def test_r11_read_claude_reports_every_window_not_only_the_worst(tmp_path):
    # R11 of context/specs/phase1-quota-windows.md (ticket bug-e1cb10). The
    # test above is this one's other half: it pins that the WORST bucket drives
    # headroom/resets_at and never looks at `windows`, which is how a provider
    # that reports none of its buckets passed for a provider that reports all
    # of them. `budget_status`'s documented promise is that every window a
    # provider reports is listed, because which bucket is binding decides what
    # to do: a session window clears in hours, a weekly one does not. A caller
    # told only "5% left, resets Sep 9" cannot tell those two apart, and routing
    # on that is what committed long agents against a five-hour wall.
    #
    # The `kind`/`percent`/`resets_at` limits shape is the real one — see
    # USAGE_PAYLOAD in test_core.py, trimmed from GET /api/oauth/usage. The
    # per-window shape asserted here is the one opencode.sh and agy.sh already
    # emit and `Budget.to_dict` already labels: {"percent": ..., "resets_at": ...}.
    session_reset = "2026-09-09T16:50:00+00:00"        # hours away
    weekly_reset = "2026-09-14T14:00:00+00:00"         # days away
    profile = h.claude_profile_dir(tmp_path, cached_usage={"limits": [
        {"kind": "session", "percent": 95.0, "resets_at": session_reset},
        {"kind": "weekly_all", "percent": 30.0, "resets_at": weekly_reset},
    ]})
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)

    # Unchanged by R11 and asserted here so a fix cannot quietly move them:
    # the headline numbers still come from the fullest bucket.
    assert b.known is True
    assert b.severity == "critical"
    assert b.headroom == pytest.approx(0.05)
    assert b.resets_at == session_reset

    # The requirement: BOTH buckets are reported, each carrying its own percent
    # and its own reset time. Keyed here by reset time rather than by name —
    # what a window is called is the implementation's business, but which clock
    # it runs on is the whole point of reporting it.
    assert len(b.windows) == 2, "both reported buckets, not only the worst"
    by_clock = {w["resets_at"]: w for w in b.windows.values()}
    assert set(by_clock) == {session_reset, weekly_reset}
    assert by_clock[session_reset]["percent"] == pytest.approx(95.0)
    assert by_clock[weekly_reset]["percent"] == pytest.approx(30.0)

    # And it reaches the caller. `to_dict` omits `windows` entirely when it is
    # falsy, which is why its absence was silent for so long — so assert the
    # contents, not the key.
    emitted = b.to_dict().get("windows") or {}
    assert {round(w["percent"]) for w in emitted.values()} == {95, 30}
    assert {w["resets_at"] for w in emitted.values()} == {session_reset, weekly_reset}


def test_read_claude_falls_back_to_five_hour_seven_day_when_limits_is_absent(tmp_path):
    profile = h.claude_profile_dir(tmp_path, cached_usage={
        "five_hour": {"utilization": 30.0, "resets_at": "X"},
        "seven_day": {"utilization": 60.0, "resets_at": "Y"},
    })
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)
    assert b.headroom == 0.4
    assert b.resets_at == "Y"


def test_read_claude_exhausted_extra_credits_forces_critical_only_when_enabled(tmp_path):
    profile_enabled = h.claude_profile_dir(tmp_path / "enabled", cached_usage={
        "limits": [{"percent": 10.0}],
        "extra_usage": {"monthly_limit": 100, "used_credits": 100,
                        "spend_limit_reached": True, "is_enabled": True},
    })
    b_enabled = h.budget_mod.read_claude(fetch=False, config_dir=profile_enabled)
    assert b_enabled.severity == "critical"
    assert "nothing carries a session past the window limit" in b_enabled.note
    assert b_enabled.spent == {"extra_credits_used": 100, "extra_credits_limit": 100}

    profile_disabled = h.claude_profile_dir(tmp_path / "disabled", cached_usage={
        "limits": [{"percent": 10.0}],
        "extra_usage": {"monthly_limit": 100, "used_credits": 100,
                        "spend_limit_reached": True, "is_enabled": False},
    })
    b_disabled = h.budget_mod.read_claude(fetch=False, config_dir=profile_disabled)
    # Same exhausted-credits note fires either way; only the derived severity
    # (from the underlying 10% window alone) differs by is_enabled.
    assert b_disabled.severity == "normal"
    assert "nothing carries a session past the window limit" in b_disabled.note


# ===========================================================================
# 7. choose_provider — routing decisions
# ===========================================================================

def test_choose_provider_preferred_with_headroom_wins_outright():
    ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("p", {"p": ok}, [], reserve=0.15)
    assert (name, why) == ("p", "preferred provider has headroom")


def test_choose_provider_exhausted_preferred_with_empty_chain_defers():
    exhausted = mkbudget(headroom=0.0)
    name, why = h.choose_provider("p", {"p": exhausted}, [], reserve=0.15)
    assert name is None
    assert why == "p and all fallbacks are exhausted or cooling down"


def test_choose_provider_falls_back_to_a_chain_entry_with_room():
    exhausted = mkbudget(headroom=0.0)
    ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("p", {"p": exhausted, "q": ok}, ["q"], reserve=0.15)
    assert (name, why) == ("q", "p is constrained; falling back to q")


def test_choose_provider_defer_sentinel_stops_the_chain_before_reaching_it():
    exhausted = mkbudget(headroom=0.0)
    ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("p", {"p": exhausted, "q": ok}, ["defer", "q"], reserve=0.15)
    assert name is None
    assert why == "p and all fallbacks are exhausted or cooling down"


def test_choose_provider_preferred_missing_from_budgets_entirely_is_still_chosen():
    # F122, half one: `_has_room(None, ...)` returns True (unknown headroom is
    # not "no headroom"), and the preferred branch reaches it directly — a
    # provider never read at all is treated exactly like one with headroom,
    # down to the reported reason.
    name, why = h.choose_provider("p", {}, [], reserve=0.15)
    assert (name, why) == ("p", "preferred provider has headroom")


def test_choose_provider_fallback_missing_from_budgets_entirely_is_rejected():
    # F122, half two: the SAME "never read" state, reached as a fallback
    # instead of as the preferred slot, is excluded outright — the chain loop
    # requires `budgets.get(name) is not None` before it will even ask
    # `_has_room`. A provider missing from `budgets` is usable as a preferred
    # but never as a fallback.
    exhausted = mkbudget(headroom=0.0)
    name, why = h.choose_provider("p", {"p": exhausted}, ["q"], reserve=0.15)
    assert name is None
    assert "q (no budget reading)" in why


def test_choose_provider_fallback_with_known_false_reading_is_accepted():
    # Contrast with the previous test: a provider that WAS read and came back
    # known=False (a real "unknown headroom" Budget, not an absent dict entry)
    # passes `_has_room` and is chosen — the missing-vs-unknown line falls
    # between "never in the dict" and "read but inconclusive", not between
    # "known" and "not known".
    exhausted = mkbudget(headroom=0.0)
    unknown = mkbudget(known=False)
    name, why = h.choose_provider("p", {"p": exhausted, "q": unknown}, ["q"], reserve=0.15)
    assert (name, why) == ("q", "p is constrained; falling back to q")


def test_choose_provider_reserve_blocks_a_fallback_only_when_it_is_reserved():
    exhausted = mkbudget(headroom=0.0)
    low = mkbudget(headroom=0.10)     # above the 2% floor, below a 15% reserve
    name, why = h.choose_provider("p", {"p": exhausted, "q": low}, ["q"],
                                  reserve=0.15, reserved={"q"})
    assert name is None
    assert "below the 15% reserve" in why

    name2, why2 = h.choose_provider("p", {"p": exhausted, "q": low}, ["q"],
                                    reserve=0.15, reserved=set())
    assert (name2, why2) == ("q", "p is constrained; falling back to q")


def test_choose_provider_allowed_excludes_a_chain_entry_by_missing_model():
    exhausted = mkbudget(headroom=0.0)
    ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("p", {"p": exhausted, "a": ok, "b": ok}, ["a", "b"],
                                  reserve=0.15, allowed={"b"})
    assert name == "b"


def test_choose_provider_reports_both_no_room_and_no_model_reasons_together():
    exhausted = mkbudget(headroom=0.0)
    also_exhausted = mkbudget(headroom=0.0)
    name, why = h.choose_provider("p", {"p": exhausted, "a": also_exhausted}, ["a", "b"],
                                  reserve=0.15, allowed={"a"})
    assert name is None
    assert "a (window empty)" in why
    assert "no model named for b" in why


def test_choose_provider_family_prefers_preferred_when_it_has_room():
    fam_ok = mkbudget(headroom=0.9)
    fam_low = mkbudget(headroom=0.01)
    name, why = h.choose_provider("claude", {"claude": fam_ok, "claude-2": fam_low},
                                  [], reserve=0.15, family=["claude", "claude-2"])
    assert (name, why) == ("claude", "preferred provider has headroom")


def test_choose_provider_family_moves_to_a_sibling_when_preferred_is_reserved():
    fam_low = mkbudget(headroom=0.01)
    fam_ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("claude", {"claude": fam_low, "claude-2": fam_ok},
                                  [], reserve=0.15, reserved={"claude"},
                                  family=["claude", "claude-2"])
    assert name == "claude-2"
    # no room comes first in the order pick_instance decides: exhausted AND reserved is "constrained"
    assert why == "claude is constrained; using claude-2"


def test_choose_provider_family_says_held_when_reserved_preferred_still_has_room():
    roomy = mkbudget(headroom=0.5)    # above the 15% reserve, so it has room
    fam_ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("claude", {"claude": roomy, "claude-2": fam_ok},
                                  [], reserve=0.15, reserved={"claude"},
                                  family=["claude", "claude-2"])
    assert name == "claude-2"
    assert why == "claude is held for the orchestrator; using claude-2"


def test_choose_provider_family_wording_differs_when_preferred_is_merely_constrained():
    fam_low = mkbudget(headroom=0.01)
    fam_ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("claude", {"claude": fam_low, "claude-2": fam_ok},
                                  [], reserve=0.15, reserved=set(),
                                  family=["claude", "claude-2"])
    assert name == "claude-2"
    assert why == "claude is constrained; using claude-2"


def test_choose_provider_family_all_exhausted_waits_when_one_resets_soon():
    soon = mkbudget(headroom=0.0, cooldown_until=time.time() + 50)
    name, why = h.choose_provider("claude", {"claude": soon, "claude-2": soon}, [],
                                  reserve=0.15, family=["claude", "claude-2"],
                                  wait_for_reset_within=100)
    assert name is None
    assert "waiting rather than moving the work" in why


def test_choose_provider_family_all_exhausted_falls_through_to_chain_when_reset_window_too_small():
    soon = mkbudget(headroom=0.0, cooldown_until=time.time() + 50)
    name, why = h.choose_provider("claude", {"claude": soon, "claude-2": soon}, ["fallback"],
                                  reserve=0.15, family=["claude", "claude-2"],
                                  wait_for_reset_within=10)
    assert name is None
    assert "fallback (no budget reading)" in why    # fell through into the ordinary chain loop


# ===========================================================================
# 8. pick_instance — ranking by load and last_used, never by headroom
# ===========================================================================

def test_pick_instance_ties_are_broken_alphabetically():
    budgets = {"a": mkbudget(headroom=0.9), "b": mkbudget(headroom=0.9), "c": mkbudget(headroom=0.0)}
    assert h.pick_instance(["a", "b", "c"], budgets, 0.15, set()) == "a"


def test_pick_instance_prefers_lower_load_over_more_headroom():
    budgets = {"a": mkbudget(headroom=0.9), "b": mkbudget(headroom=0.5)}
    # b has less headroom but less load; load wins.
    assert h.pick_instance(["a", "b"], budgets, 0.15, set(), load={"a": 3, "b": 1}) == "b"


def test_pick_instance_breaks_a_load_tie_with_last_used():
    budgets = {"a": mkbudget(headroom=0.9), "b": mkbudget(headroom=0.9)}
    assert h.pick_instance(["a", "b"], budgets, 0.15, set(),
                           load={"a": 1, "b": 1}, last_used={"a": 100, "b": 50}) == "b"


def test_pick_instance_returns_none_when_nothing_has_room():
    budgets = {"c": mkbudget(headroom=0.0)}
    assert h.pick_instance(["c"], budgets, 0.15, set()) is None


def test_pick_instance_prefers_a_non_reserved_instance_over_a_reserved_one():
    budgets = {"a": mkbudget(headroom=0.9), "b": mkbudget(headroom=0.9)}
    assert h.pick_instance(["a", "b"], budgets, 0.15, {"a"}) == "b"


def test_pick_instance_falls_back_to_a_reserved_instance_if_nothing_else_qualifies():
    budgets = {"a": mkbudget(headroom=0.9), "b": mkbudget(headroom=0.9)}
    assert h.pick_instance(["a", "b"], budgets, 0.15, {"a", "b"}) == "a"


# ===========================================================================
# 9. reserved_providers
# ===========================================================================

def test_reserved_providers_defaults_to_just_the_orchestrator():
    assert h.reserved_providers({}, ["a", "b"], "a") == {"a"}


def test_reserved_providers_empty_without_an_orchestrator_name():
    assert h.reserved_providers({}, ["a", "b"], "") == set()


def test_reserved_providers_reserve_true_covers_everything():
    assert h.reserved_providers({"budget": {"reserve": True}}, ["a", "b"], "a") == {"a", "b"}


def test_reserved_providers_reserve_orchestrator_false_disables_the_default():
    assert h.reserved_providers({"budget": {"reserve_orchestrator": False}}, ["a", "b"], "a") == set()


# ===========================================================================
# 10. resets_soon
# ===========================================================================

def test_resets_soon_is_false_for_none():
    assert h.resets_soon(None, 100) is False


def test_resets_soon_is_false_for_an_already_usable_candidate():
    ok = mkbudget(headroom=0.5)
    assert h.resets_soon(ok, 100) is False


def test_resets_soon_boundary_is_inclusive_at_exactly_within():
    now = time.time()
    exhausted = mkbudget(headroom=0.0, cooldown_until=now + 100)
    assert h.resets_soon(exhausted, 100) is True


def test_resets_soon_just_past_the_boundary_is_false():
    now = time.time()
    exhausted = mkbudget(headroom=0.0, cooldown_until=now + 100.5)
    assert h.resets_soon(exhausted, 100) is False


def test_resets_soon_is_false_once_the_cooldown_has_already_passed():
    now = time.time()
    exhausted = mkbudget(headroom=0.0, cooldown_until=now - 5)
    assert h.resets_soon(exhausted, 100) is False


def test_resets_soon_uses_resets_at_when_there_is_no_cooldown():
    when = datetime.fromtimestamp(time.time() + 50, tz=timezone.utc).isoformat()
    exhausted = mkbudget(headroom=0.0, resets_at=when)
    assert h.resets_soon(exhausted, 100) is True
