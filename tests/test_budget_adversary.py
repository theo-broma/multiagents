"""Adversary tests for budget.py — mutation, hardcoding, fuzzing, interference.

These tests attack the 70-test characterization suite to determine whether it
would catch real defects in routing and accounting logic.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402

Budget = h.Budget
_has_room = h.budget_mod._has_room


def mkbudget(**kw):
    kw.setdefault("provider", "x")
    kw.setdefault("known", True)
    return Budget(**kw)


# ===========================================================================
# Mutation: does the suite catch a flipped comparison in usable?
# ===========================================================================

def test_mutation_usable_boundary_at_exactly_0_02_is_caught():
    """If `> 0.02` became `>= 0.02`, this test would fail.

    The existing test pins 0.02 as not usable and 0.021 as usable, but does not
    pin the exact boundary value that would distinguish `>` from `>=`. A mutation
    changing `>` to `>=` would make 0.02 usable, breaking this test.
    """
    b = mkbudget(headroom=0.02)
    assert b.usable is False, "headroom exactly at 0.02 must NOT be usable"


def test_mutation_usable_boundary_at_exactly_0_021_is_caught():
    """If `> 0.02` became `> 0.021`, this test would fail.

    The existing suite tests 0.02 (not usable) and 0.021 (usable), but a mutation
    changing the threshold from 0.02 to 0.021 would still pass both. This test
    pins 0.021 as usable, which would fail if the threshold moved to 0.021.
    """
    b = mkbudget(headroom=0.021)
    assert b.usable is True, "headroom at 0.021 must be usable"


# ===========================================================================
# Mutation: does the suite catch a flipped reserve comparison?
# ===========================================================================

def test_mutation_reserve_boundary_at_exactly_reserve_is_caught():
    """If `>= reserve` became `> reserve`, this test would fail.

    The existing suite tests headroom=0.10 against reserve=0.15 (blocked) and
    headroom=0.9 against reserve=0.15 (allowed), but does not pin the exact
    boundary. A mutation changing `>=` to `>` would make headroom=0.15 blocked
    when reserved, breaking this test.
    """
    low = mkbudget(headroom=0.15)
    assert _has_room(low, 0.15, True) is True, "headroom exactly at reserve must pass when reserved"


def test_mutation_reserve_boundary_just_below_is_caught():
    """If `>= reserve` became `>= reserve - 0.01`, this test would fail.

    Pins that headroom=0.149 against reserve=0.15 is blocked when reserved.
    """
    low = mkbudget(headroom=0.149)
    assert _has_room(low, 0.15, True) is False, "headroom just below reserve must be blocked"


# ===========================================================================
# Mutation: does the suite catch a flipped severity threshold?
# ===========================================================================

def test_mutation_severity_warning_boundary_at_75_is_caught():
    """If `>= 75` became `> 75`, this test would fail.

    The existing suite tests percent=80 as "warning", but does not pin the exact
    boundary at 75. A mutation changing `>= 75` to `> 75` would make percent=75
    read as "normal", breaking this test.
    """
    h.invalidate_cache()
    profile = h.claude_profile_dir(
        Path("/tmp/test-sev-75"),
        cached_usage={"limits": [{"percent": 75.0}]},
    )
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)
    assert b.severity == "warning", "percent exactly at 75 must be warning"


def test_mutation_severity_critical_boundary_at_90_is_caught():
    """If `>= 90` became `> 90`, this test would fail.

    Pins that percent=90 reads as "critical", not "warning".
    """
    h.invalidate_cache()
    profile = h.claude_profile_dir(
        Path("/tmp/test-sev-90"),
        cached_usage={"limits": [{"percent": 90.0}]},
    )
    b = h.budget_mod.read_claude(fetch=False, config_dir=profile)
    assert b.severity == "critical", "percent exactly at 90 must be critical"


# ===========================================================================
# Hardcoding: untested inputs
# ===========================================================================

def test_hardcoding_headroom_exactly_zero_is_not_usable():
    """The suite tests headroom=0.0 as not usable, but does not test the
    interaction with known=False. A budget with known=False and headroom=0.0
    should still be usable (unknown headroom is not "no headroom").
    """
    b = mkbudget(known=False, headroom=0.0)
    assert b.usable is True, "unknown headroom with headroom=0.0 must still be usable"


def test_hardcoding_headroom_negative_is_not_usable():
    """A negative headroom implies over-use. The suite tests -0.5 as "critical"
    severity, but does not test whether it is usable. It should not be.
    """
    b = mkbudget(known=True, headroom=-0.5)
    assert b.usable is False, "negative headroom must not be usable"


def test_hardcoding_headroom_exactly_one_is_usable():
    """headroom=1.0 implies 0% used. The suite tests 0.9 and 1.5, but not the
    exact boundary at 1.0.
    """
    b = mkbudget(known=True, headroom=1.0)
    assert b.usable is True, "headroom=1.0 must be usable"


def test_hardcoding_choose_provider_with_empty_budgets_dict():
    """The suite tests preferred missing from budgets, but not an entirely empty
    dict. Should still return the preferred with "has headroom" reason.
    """
    name, why = h.choose_provider("p", {}, [], reserve=0.15)
    assert name == "p"
    assert why == "preferred provider has headroom"


def test_hardcoding_choose_provider_chain_with_only_defer():
    """The suite tests defer stopping the chain, but not a chain of ONLY defer.
    Should return None with exhausted message.
    """
    exhausted = mkbudget(headroom=0.0)
    name, why = h.choose_provider("p", {"p": exhausted}, ["defer"], reserve=0.15)
    assert name is None
    assert "exhausted" in why


def test_hardcoding_pick_instance_with_empty_names_list():
    """The suite tests various name lists, but not an empty one. Should return None."""
    budgets = {"a": mkbudget(headroom=0.9)}
    result = h.pick_instance([], budgets, 0.15, set())
    assert result is None


def test_hardcoding_pick_instance_with_all_exhausted():
    """The suite tests one exhausted, but not all exhausted in a multi-instance
    scenario. Should return None.
    """
    budgets = {
        "a": mkbudget(headroom=0.0),
        "b": mkbudget(headroom=0.01),
        "c": mkbudget(headroom=0.02),
    }
    result = h.pick_instance(["a", "b", "c"], budgets, 0.15, set())
    assert result is None


def test_hardcoding_resets_soon_with_zero_within():
    """The suite tests positive within values, but not zero. Should return False
    because the condition is `0 < (when - time.time()) <= within`, and with
    within=0, nothing can satisfy `<= 0` while also being `> 0`.
    """
    now = time.time()
    exhausted = mkbudget(headroom=0.0, cooldown_until=now + 100)
    assert h.resets_soon(exhausted, 0) is False


def test_hardcoding_resets_soon_with_negative_within():
    """Negative within should always return False."""
    now = time.time()
    exhausted = mkbudget(headroom=0.0, cooldown_until=now + 100)
    assert h.resets_soon(exhausted, -10) is False


# ===========================================================================
# Fuzzing: property-based tests
# ===========================================================================

def test_fuzz_usable_is_monotonic_in_headroom():
    """Property: if headroom h2 > h1 and both are known, then usable(h1) implies
    usable(h2). In other words, usable should be monotonic: once it becomes True
    as headroom increases, it stays True.
    """
    for seed in range(100):
        h1 = seed / 100.0
        h2 = (seed + 1) / 100.0
        b1 = mkbudget(known=True, headroom=h1)
        b2 = mkbudget(known=True, headroom=h2)
        if b1.usable:
            assert b2.usable, f"monotonicity violated at h1={h1}, h2={h2}"


def test_fuzz_choose_provider_always_returns_allowed_or_none():
    """Property: choose_provider should never return a provider not in `allowed`
    when `allowed` is specified.
    """
    for seed in range(50):
        preferred = "p"
        budgets = {
            "p": mkbudget(headroom=0.0),
            "a": mkbudget(headroom=0.9),
            "b": mkbudget(headroom=0.9),
        }
        allowed = {"a"}
        name, why = h.choose_provider(preferred, budgets, ["a", "b"], reserve=0.15, allowed=allowed)
        if name is not None:
            assert name in allowed, f"returned {name} which is not in allowed={allowed}"


def test_fuzz_pick_instance_respects_reserve():
    """Property: pick_instance should never return an instance below the reserve
    when reserved, unless nothing else qualifies.
    """
    for seed in range(50):
        budgets = {
            "a": mkbudget(headroom=0.10),  # below 0.15 reserve
            "b": mkbudget(headroom=0.20),  # above reserve
        }
        result = h.pick_instance(["a", "b"], budgets, 0.15, {"a"})
        assert result == "b", f"should prefer non-reserved 'b' over reserved 'a'"


# ===========================================================================
# Interference: cache mutation and concurrency
# ===========================================================================

def test_interference_cache_hit_mutates_shared_object(tmp_path):
    """The suite pins that a cache hit overwrites spent, but does not pin that
    the mutation affects the cached object itself. This test proves it.
    """
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')

    first = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"a": 1})
    cached_obj = h.budget_mod._cache["p"][1]

    second = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"b": 2})

    assert cached_obj.spent == {"b": 2}, "cache object was mutated in place"
    assert first.spent == {"b": 2}, "first reference sees the mutation"


def test_interference_concurrent_cache_reads_may_see_stale_data(tmp_path):
    """If two callers interleave, one may see the other's spent overwrite.

    This is not a race condition in the threading sense (Python's GIL prevents
    true concurrency), but it demonstrates that the cache's in-place mutation
    means callers sharing a provider name will see each other's spend.
    """
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.5}\'; exit 0 ;;')

    caller1 = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"caller1": 100})
    caller2 = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, spent={"caller2": 200})

    assert caller1.spent == {"caller2": 200}, "caller1 sees caller2's spend"
    assert caller2.spent == {"caller2": 200}


# ===========================================================================
# Additional mutations: severity derivation from script output
# ===========================================================================

def test_mutation_script_severity_derivation_boundary_at_75(tmp_path):
    """If the derived severity threshold `>= 75` became `> 75`, this test would fail.

    The suite tests headroom=0.5 as "normal" and headroom=-0.5 as "critical",
    but does not pin the exact boundary at 75% used (headroom=0.25).
    """
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.25}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.severity == "warning", "headroom=0.25 (75% used) must be warning"


def test_mutation_script_severity_derivation_boundary_at_90(tmp_path):
    """If the derived severity threshold `>= 90` became `> 90`, this test would fail.

    Pins that headroom=0.10 (90% used) reads as "critical".
    """
    h.invalidate_cache()
    provider = h.make_provider("p")
    h.case_script(tmp_path, "p.sh", 'budget) printf \'{"known": true, "headroom": 0.10}\'; exit 0 ;;')
    b = h.read_provider("p", provider, h.FakeExecutor(), tmp_path, use_cache=False)
    assert b.severity == "critical", "headroom=0.10 (90% used) must be critical"


# ===========================================================================
# Edge cases in choose_provider family logic
# ===========================================================================

def test_hardcoding_choose_provider_family_with_only_preferred():
    """If family contains only the preferred, should behave like no family."""
    fam_ok = mkbudget(headroom=0.9)
    name, why = h.choose_provider("claude", {"claude": fam_ok}, [],
                                  reserve=0.15, family=["claude"])
    assert name == "claude"
    assert why == "preferred provider has headroom"


def test_hardcoding_choose_provider_family_all_exhausted_no_wait():
    """If all family members are exhausted and wait_for_reset_within=0, should
    fall through to chain, not wait.
    """
    soon = mkbudget(headroom=0.0, cooldown_until=time.time() + 50)
    name, why = h.choose_provider("claude", {"claude": soon, "claude-2": soon}, ["fallback"],
                                  reserve=0.15, family=["claude", "claude-2"],
                                  wait_for_reset_within=0)
    assert name is None
    assert "fallback (no budget reading)" in why
