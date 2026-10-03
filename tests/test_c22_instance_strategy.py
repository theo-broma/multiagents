"""C22 — a configurable choice among a family's accounts.

Contract: `context/specs/c22-instance-strategy.md` (IS-R1..R5, with the revision
section IS-R1a, R1b, R1c, R2a, R3a, R5, which overrides the earlier wording).
Tests are named after the requirement they pin.

Seams:
  budget.pick_instance / budget.choose_provider   pure, over fixture `Budget`s
  Runner.start (fake CLIs, `budget.read_all` injected)   precedence, end to end
  config.load                                     layering, bad values
  providers.load_providers                        bad provider values
  cli.cmd_doctor                                  the problem line
  the codex budget adapter                        `span_minutes` on each window

ASSUMPTIONS (the contract is silent; each is the loosest reading):
- The strategy and the tolerances reach the pure functions as keyword
  arguments `strategy=`, `tolerance_minutes=`, `tolerance_points=` on BOTH
  `pick_instance` and `choose_provider`. They are spelled once, in `pick()`
  and `choose()` below, so a different spelling is a one-line change. Omitted,
  they mean the defaults (`soonest_reset`, 15 minutes, 5 points).
- A window is `{"percent": used, "resets_at": iso, "counted": bool,
  "span_minutes": n}` (the shape `Budget.windows` has today, plus the span
  IS-R1c names). Fixture readings carry a `headroom` of 0.5 unless a test says
  otherwise: eligibility reads `headroom`, strategies read the windows, and the
  two are deliberately independent here so a window at 100% used stays a
  candidate.
- An unknown strategy name handed to `pick_instance` raises `ValueError`
  ("a bad value never silently falls back"); at config load it is a
  `ValueError` naming the value, the file and the line.
- `doctor` reports the bad name as a problem: exit code 1, one more
  `N problem(s)` than a clean project, and the output names the value.
- Not tested, because the contract is silent: the exact tolerance boundary
  (tests stay 1 or more units clear of it), a window whose own reset is past
  under the remaining-quota strategies, an invalid `span_minutes` value, a
  windowless reading mixed with a windowed one under the remaining-quota
  strategies, name matching case, and the reset wording ("21:50").
- IS-R5 (the claude vault's own representative) is a statement that nothing
  changes; it has no test.

Stubs: none. Every test is meant to fail today on its own assertion
(a missing keyword, a winner chosen by load, a reason without the strategy).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
import codex_harness as ch  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import cli, manifest  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.budget import Budget, choose_provider, pick_instance  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli  # noqa: E402

STRATEGIES = ("soonest_reset", "shortest_window_least_remaining",
              "shortest_window_most_remaining", "longest_window_least_remaining",
              "longest_window_most_remaining", "least_loaded")
REMAINING = STRATEGIES[1:5]
SOONEST = "soonest_reset"
S_LEAST, S_MOST = "shortest_window_least_remaining", "shortest_window_most_remaining"
L_LEAST, L_MOST = "longest_window_least_remaining", "longest_window_most_remaining"


# ---------------------------------------------------------------------------
# fixtures: readings, and the one place the new keywords are spelled
# ---------------------------------------------------------------------------

def at(minutes: float) -> str:
    """An aware ISO instant `minutes` from now."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def win(used, resets=None, *, span=None, counted=None, **extra) -> dict:
    window = {"percent": used, **extra}
    if resets is not None:
        window["resets_at"] = resets if isinstance(resets, str) else at(resets)
    if span is not None:
        window["span_minutes"] = span
    if counted is not None:
        window["counted"] = counted
    return window


def bud(name, windows=None, *, headroom=0.5, known=True, stale=False) -> Budget:
    return Budget(name, known=known, headroom=headroom if known else None,
                  windows=dict(windows or {}), stale=stale)


def pick(names, budgets, *, strategy=None, tolerance_minutes=None,
         tolerance_points=None, load=None, last=None, reserved=(), reserve=0.15):
    extra = {key: value for key, value in (
        ("strategy", strategy), ("tolerance_minutes", tolerance_minutes),
        ("tolerance_points", tolerance_points)) if value is not None}
    return pick_instance(list(names), budgets, reserve, set(reserved),
                         load or {}, last or {}, **extra)


def winner(budgets, **kwargs):
    """The winner, which must not depend on the order the pool is listed in."""
    found = {pick(order, budgets, **kwargs)
             for order in itertools.permutations(budgets)}
    assert len(found) == 1, f"the winner depends on pool order: {found}"
    return found.pop()


def choose(budgets, preferred="agy", *, strategy=None, tolerance_minutes=None,
           tolerance_points=None, load=None, last=None, reserved=(), reserve=0.15):
    extra = {key: value for key, value in (
        ("strategy", strategy), ("tolerance_minutes", tolerance_minutes),
        ("tolerance_points", tolerance_points)) if value is not None}
    return choose_provider(preferred, budgets, [], reserve=reserve,
                           reserved=set(reserved), family=list(budgets),
                           load=load or {}, last_used=last or {}, **extra)


def reset_pool(**minutes) -> dict:
    """Instances whose only window (a 5h one) resets in the given minutes."""
    return {name: bud(name, {"gemini-5h": win(50, m)}) for name, m in minutes.items()}


def used_pool(window="gemini-5h", **used) -> dict:
    return {name: bud(name, {window: win(u, 60 if window == "gemini-5h" else 4000)})
            for name, u in used.items()}


# ===========================================================================
# IS-R1 — the strategies
# ===========================================================================

def test_is_r1_default_is_soonest_reset_when_no_strategy_is_given():
    pool = reset_pool(a=120, b=60, c=200)
    assert winner(pool) == "b"


def test_is_r1_soonest_reset_picks_the_earliest_upcoming_reset():
    pool = reset_pool(a=120, b=60, c=200)
    assert winner(pool, strategy=SOONEST) == "b"


def test_is_r1_soonest_reset_looks_across_every_counted_window():
    pool = {"a": bud("a", {"gemini-5h": win(50, 300), "gemini-weekly": win(50, 40)}),
            "b": bud("b", {"gemini-5h": win(50, 90), "gemini-weekly": win(50, 6000)}),
            "c": bud("c", {"gemini-5h": win(50, 200)})}
    assert winner(pool, strategy=SOONEST) == "a"


def test_is_r1_soonest_reset_ignores_usage_and_load_outside_the_tolerance():
    pool = {"a": bud("a", {"gemini-5h": win(95, 120)}),
            "b": bud("b", {"gemini-5h": win(5, 60)})}
    assert winner(pool, strategy=SOONEST, load={"b": 9}) == "b"


def two_windows(**pairs) -> dict:
    """name=(5h used, weekly used). Resets are the same for all."""
    return {name: bud(name, {"gemini-5h": win(five, 100), "gemini-weekly": win(week, 4000)})
            for name, (five, week) in pairs.items()}


# a: busy in the short window, empty in the long one; b: the reverse; c: middle.
MIXED = dict(a=(30, 90), b=(70, 10), c=(50, 50))


@pytest.mark.parametrize("strategy,expected", [
    (S_LEAST, "b"),    # the 5h window with the least left: 70% used
    (S_MOST, "a"),     # the 5h window with the most left: 30% used
    (L_LEAST, "a"),    # the weekly window with the least left: 90% used
    (L_MOST, "b"),     # the weekly window with the most left: 10% used
])
def test_is_r1_each_remaining_strategy_judges_the_window_it_names(strategy, expected):
    assert winner(two_windows(**MIXED), strategy=strategy) == expected


def test_is_r1_least_loaded_is_todays_order_and_ignores_windows():
    pool = {"a": bud("a", {"gemini-5h": win(5, 10)}),
            "b": bud("b", {"gemini-5h": win(95, 900)}),
            "c": bud("c", {"gemini-5h": win(50, 500)})}
    assert winner(pool, strategy="least_loaded", load={"a": 2, "b": 0, "c": 1}) == "b"
    assert winner(pool, strategy="least_loaded", load={"a": 1, "b": 1, "c": 1},
                  last={"a": 50.0, "b": 100.0, "c": 10.0}) == "c"
    assert winner(pool, strategy="least_loaded") == "a"


def test_is_r1_a_strategy_ranks_only_the_pool_it_is_given_two_instances():
    pool = reset_pool(a=60, b=200)
    assert winner(pool, strategy=SOONEST) == "a"
    assert pick(["b"], pool, strategy=SOONEST) == "b"


def test_is_r1_pick_instance_does_not_alter_the_readings():
    pool = two_windows(**MIXED)
    before = {name: (b.headroom, json.dumps(b.windows, sort_keys=True))
              for name, b in pool.items()}
    first = pick(list(pool), pool, strategy=L_MOST)
    second = pick(list(pool), pool, strategy=L_MOST)
    assert first == second == "b"
    assert before == {name: (b.headroom, json.dumps(b.windows, sort_keys=True))
                      for name, b in pool.items()}


def test_is_r1_an_unknown_strategy_name_is_refused_not_defaulted():
    pool = reset_pool(a=60, b=200)
    with pytest.raises(ValueError):
        pick(list(pool), pool, strategy="soonest-reset")
    with pytest.raises(ValueError):
        pick(list(pool), pool, strategy="")


# --- missing data ranks after data, then today's order ----------------------

@pytest.mark.parametrize("strategy", REMAINING + (SOONEST,))
def test_is_r1_missing_data_ranks_after_an_instance_that_has_it(strategy):
    # `a` is the worst candidate on every metric; `x` has no usable data but the
    # lowest load and the first name.
    if strategy == SOONEST:
        a = bud("b", {"gemini-5h": win(50, 900)})
        x = bud("a", {"gemini-5h": win(50)})              # no reset time
    else:
        a = bud("b", {"gemini-5h": win(50, 60), "gemini-weekly": win(50, 4000)})
        x = bud("a", {"rolling": win(99, 60)})            # no window of known span
    assert winner({"a": x, "b": a}, strategy=strategy, load={"b": 4}) == "b"


@pytest.mark.parametrize("strategy", REMAINING + (SOONEST,))
def test_is_r1_instances_that_all_lack_the_data_fall_back_to_load_last_use_name(strategy):
    def none():
        if strategy == SOONEST:
            return {"gemini-5h": win(50)}
        return {"rolling": win(50, 30)}

    pool = {n: bud(n, none()) for n in ("a", "b", "c")}
    assert winner(pool, strategy=strategy, load={"a": 2, "b": 1, "c": 1},
                  last={"b": 90.0, "c": 10.0}) == "c"
    assert winner(pool, strategy=strategy, load={"a": 1, "b": 1, "c": 1},
                  last={"a": 50.0, "b": 90.0, "c": 10.0}) == "c"
    assert winner(pool, strategy=strategy) == "a"


def test_is_r1_two_with_data_and_one_without_the_data_pair_still_decides():
    pool = {"a": bud("a", {"gemini-5h": win(50)}),
            "b": bud("b", {"gemini-5h": win(50, 100)}),
            "c": bud("c", {"gemini-5h": win(50, 160)})}
    assert winner(pool, strategy=SOONEST, load={"b": 3}) == "b"


# ===========================================================================
# IS-R1a — a tolerance around the BEST score
# ===========================================================================

def test_is_r1a_soonest_reset_scores_within_15_minutes_of_the_best_are_tied():
    pool = reset_pool(a=60, b=74)                         # b is 14 minutes behind
    assert winner(pool, strategy=SOONEST, load={"a": 3}) == "b"
    assert winner(pool, strategy=SOONEST, load={"a": 1, "b": 1},
                  last={"a": 900.0, "b": 100.0}) == "b"
    assert winner(pool, strategy=SOONEST, load={"a": 1, "b": 1},
                  last={"a": 100.0, "b": 100.0}) == "a"


def test_is_r1a_soonest_reset_a_score_beyond_15_minutes_is_not_tied():
    pool = reset_pool(a=60, b=76)                         # 16 minutes behind
    assert winner(pool, strategy=SOONEST, load={"a": 9}) == "a"


def test_is_r1a_the_default_is_15_minutes_not_more():
    pool = reset_pool(a=60, b=100)
    assert winner(pool, strategy=SOONEST, load={"a": 9}) == "a"


@pytest.mark.parametrize("strategy,best,near,far", [
    (S_LEAST, 58, 54, 50),     # least remaining = most used
    (S_MOST, 42, 46, 50),
    (L_LEAST, 58, 54, 50),
    (L_MOST, 42, 46, 50),
])
def test_is_r1a_remaining_strategies_tie_within_5_points_of_the_best_chain(
        strategy, best, near, far):
    # c is the best; b is 4 points behind it; a is 8 behind c and 4 behind b.
    # Against the BEST the group is {b, c}; pairwise chaining would pull a in.
    pool = {n: bud(n, {"gemini-5h": win(u, 60), "gemini-weekly": win(u, 4000)})
            for n, u in (("a", far), ("b", near), ("c", best))}
    load = {"a": 0, "b": 5, "c": 5}
    assert winner(pool, strategy=strategy, load=load, last={"b": 10.0, "c": 99.0}) == "b"
    assert winner(pool, strategy=strategy, load={"a": 9, "b": 5, "c": 0}) == "c"


@pytest.mark.parametrize("strategy,best,worse", [
    (S_LEAST, 60, 54), (L_LEAST, 60, 54),       # least remaining = most used
    (S_MOST, 40, 46), (L_MOST, 40, 46),
])
def test_is_r1a_a_gap_just_beyond_5_points_is_decided_by_the_metric(strategy, best, worse):
    pool = {n: bud(n, {"gemini-5h": win(u, 60), "gemini-weekly": win(u, 4000)})
            for n, u in (("a", worse), ("b", best))}
    # Load and name both favour `a`; the metric still sends the work to `b`.
    assert winner(pool, strategy=strategy, load={"b": 7}) == "b"


def test_is_r1a_soonest_reset_chain_a_b_c_leaves_c_out_when_b_is_the_best():
    # best = c at +60; b at +74 (14 behind); a at +88 (28 behind c, 14 behind b).
    pool = reset_pool(a=88, b=74, c=60)
    load = {"a": 0, "b": 5, "c": 5}
    assert winner(pool, strategy=SOONEST, load=load, last={"b": 10.0, "c": 99.0}) == "b"


def test_is_r1a_inside_the_group_load_then_last_use_then_name():
    pool = reset_pool(a=60, b=65, c=70)
    assert winner(pool, strategy=SOONEST, load={"a": 2, "b": 1, "c": 3}) == "b"
    assert winner(pool, strategy=SOONEST, load={"a": 1, "b": 1, "c": 1},
                  last={"a": 30.0, "b": 20.0, "c": 10.0}) == "c"
    assert winner(pool, strategy=SOONEST) == "a"


def test_is_r1a_minutes_tolerance_is_configurable_and_zero_is_strict():
    pool = reset_pool(a=60, b=70)
    assert winner(pool, strategy=SOONEST, load={"a": 3}) == "b"                   # 15 default
    assert winner(pool, strategy=SOONEST, load={"a": 3}, tolerance_minutes=5) == "a"
    assert winner(pool, strategy=SOONEST, load={"a": 3}, tolerance_minutes=0) == "a"
    far = reset_pool(a=60, b=300)
    assert winner(far, strategy=SOONEST, load={"a": 3}, tolerance_minutes=400) == "b"


def test_is_r1a_points_tolerance_is_configurable():
    pool = two_windows(a=(50, 50), b=(60, 60))
    assert winner(pool, strategy=S_LEAST, load={"b": 3}) == "b"                    # 10 apart
    assert winner(pool, strategy=S_LEAST, load={"b": 3}, tolerance_points=15) == "a"
    assert winner(pool, strategy=S_LEAST, load={"b": 3}, tolerance_points=0) == "b"


def test_is_r1a_each_tolerance_belongs_to_its_own_strategies():
    reset = reset_pool(a=60, b=100)
    assert winner(reset, strategy=SOONEST, load={"a": 3}, tolerance_points=100) == "a"
    used = two_windows(a=(50, 50), b=(70, 70))
    assert winner(used, strategy=S_LEAST, load={"b": 3}, tolerance_minutes=1000) == "b"


# ===========================================================================
# IS-R1b — which windows count
# ===========================================================================

def test_is_r1b_a_window_marked_not_counted_is_excluded_from_soonest_reset():
    pool = {"a": bud("a", {"gemini-5h": win(50, 10, counted=False),
                           "gemini-weekly": win(50, 4000)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "b"


@pytest.mark.parametrize("strategy", REMAINING)
def test_is_r1b_a_window_marked_not_counted_is_excluded_from_remaining_strategies(strategy):
    # The uncounted window is the one the strategy would judge, at 99% used.
    # Counted, it makes `a` the instance with the least left (it wins the
    # `least` strategies and loses the `most` ones). Uncounted, `a` is 50% used
    # next to b's 60%: the verdicts flip.
    name = "gemini-5h" if strategy in (S_LEAST, S_MOST) else "gemini-weekly"
    other = "gemini-weekly" if name == "gemini-5h" else "gemini-5h"
    a = bud("a", {name: win(99, 60 if name == "gemini-5h" else 4000, counted=False),
                  other: win(50, 4000 if name == "gemini-5h" else 60)})
    b = bud("b", {"gemini-5h": win(60, 60), "gemini-weekly": win(60, 4000)})
    # Uncounted, a's only window is `other` (50% used, which is then both its
    # shortest and its longest); b is 60% used, 10 points away.
    expected = "b" if strategy in (S_LEAST, L_LEAST) else "a"
    assert winner({"a": a, "b": b}, strategy=strategy, load={expected: 5}) == expected


def test_is_r1b_a_window_counted_true_or_absent_flag_counts():
    pool = {"a": bud("a", {"gemini-5h": win(50, 10, counted=True)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "a"


HIGH = [float("inf"), 100.5, 101, 1000]
LOW = [float("-inf"), -1, -0.5]


def short_window_pair(bad, weekly_used):
    x = bud("x", {"gemini-5h": win(bad, 60), "gemini-weekly": win(weekly_used, 4000)})
    y = bud("y", {"gemini-weekly": win(50, 4000)})
    return {"x": x, "y": y}


@pytest.mark.parametrize("bad", [float("nan"), *HIGH, *LOW])
def test_is_r1b_a_non_finite_or_out_of_range_percent_is_ignored_by_soonest_reset(bad):
    pool = {"a": bud("a", {"gemini-5h": win(bad, 10), "gemini-weekly": win(50, 4000)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "b"


@pytest.mark.parametrize("bad", [float("nan"), *HIGH])
def test_is_r1b_a_too_high_or_nan_percent_is_ignored_by_remaining_strategies(bad):
    # Ignored, X is its weekly window alone: 10% used, so Y (50% used) has less left.
    assert winner(short_window_pair(bad, 10), strategy=S_LEAST) == "y"


@pytest.mark.parametrize("bad", [float("nan"), *LOW])
def test_is_r1b_a_negative_or_nan_percent_is_ignored_by_remaining_strategies(bad):
    # Ignored, X is its weekly window alone: 90% used, so Y has more left.
    assert winner(short_window_pair(bad, 90), strategy=S_MOST) == "y"


def test_is_r1b_zero_and_one_hundred_percent_are_valid():
    full = {"x": bud("x", {"gemini-5h": win(100, 60), "gemini-weekly": win(10, 4000)}),
            "y": bud("y", {"gemini-weekly": win(50, 4000)})}
    assert winner(full, strategy=S_LEAST) == "x"            # 0% left < 50% left
    empty = {"x": bud("x", {"gemini-5h": win(0, 60), "gemini-weekly": win(90, 4000)}),
             "y": bud("y", {"gemini-weekly": win(50, 4000)})}
    assert winner(empty, strategy=S_MOST) == "x"            # 100% left > 50% left


def test_is_r1b_a_past_reset_is_ignored_for_soonest_reset():
    pool = {"a": bud("a", {"gemini-5h": win(50, -10), "gemini-weekly": win(50, 4000)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "b"


def test_is_r1b_a_naive_reset_is_ignored_for_soonest_reset():
    naive = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    pool = {"a": bud("a", {"gemini-5h": win(50, naive), "gemini-weekly": win(50, 4000)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "b"


def test_is_r1b_an_unparseable_reset_is_ignored_for_soonest_reset():
    pool = {"a": bud("a", {"gemini-5h": win(50, "soon"), "gemini-weekly": win(50, 4000)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "b"


def test_is_r1b_an_aware_reset_in_zulu_notation_counts():
    zulu = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pool = {"a": bud("a", {"gemini-5h": win(50, zulu)}),
            "b": bud("b", {"gemini-5h": win(50, 120)})}
    assert winner(pool, strategy=SOONEST) == "a"


def test_is_r1b_only_past_or_naive_resets_means_no_data_so_load_decides():
    naive = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    pool = {"a": bud("a", {"gemini-5h": win(50, naive)}),
            "b": bud("b", {"gemini-5h": win(50, -30)})}
    assert winner(pool, strategy=SOONEST, load={"a": 2, "b": 1}) == "b"
    assert winner(pool, strategy=SOONEST, load={"a": 1, "b": 2}) == "a"


def test_is_r1b_a_stale_reading_scores_nothing_and_ranks_as_missing_data():
    pool = {"fresh": bud("fresh", {"gemini-5h": win(50, 600)}),
            "stale": bud("stale", {"gemini-5h": win(50, 5)}, stale=True)}
    assert winner(pool, strategy=SOONEST, load={"fresh": 4}) == "fresh"
    used = {"fresh": bud("fresh", {"gemini-5h": win(10, 60)}),
            "stale": bud("stale", {"gemini-5h": win(99, 60)}, stale=True)}
    assert winner(used, strategy=S_LEAST, load={"fresh": 4}) == "fresh"


@pytest.mark.parametrize("strategy,low,high", [(SOONEST, 5, 300), (S_LEAST, 90, 10)])
def test_is_r1b_when_every_candidate_is_stale_load_last_use_name_decide(strategy, low, high):
    def stale(name, v):
        windows = {"gemini-5h": win(50, v) if strategy == SOONEST else win(v, 60)}
        return bud(name, windows, stale=True)

    pool = {"a": stale("a", low), "b": stale("b", high)}
    assert winner(pool, strategy=strategy, load={"a": 3, "b": 0}) == "b"
    assert winner(pool, strategy=strategy, load={"a": 1, "b": 1},
                  last={"a": 100.0, "b": 10.0}) == "b"
    assert winner(pool, strategy=strategy) == "a"


@pytest.mark.parametrize("strategy,expected", [(S_LEAST, "a"), (S_MOST, "c"),
                                               (L_LEAST, "a"), (L_MOST, "c")])
def test_is_r1b_with_no_windows_the_top_level_percent_is_the_single_window(strategy, expected):
    pool = {"a": bud("a", headroom=0.2), "b": bud("b", headroom=0.5),
            "c": bud("c", headroom=0.9)}
    assert not any(b.windows for b in pool.values())
    assert winner(pool, strategy=strategy) == expected


def test_is_r1b_equal_spans_the_most_used_window_represents_the_instance():
    a = bud("a", {"w1": win(30, 60, span=300), "w2": win(80, 60, span=300)})
    b = bud("b", {"w": win(60, 60, span=300)})
    pool = {"a": a, "b": b}
    assert winner(pool, strategy=S_LEAST) == "a"            # 80% used, not 30%
    assert winner(pool, strategy=S_MOST) == "b"             # a: 20% left, b: 40%
    a = bud("a", {"w1": win(30, 60, span=10080), "w2": win(80, 60, span=10080)})
    b = bud("b", {"w": win(60, 60, span=10080)})
    pool = {"a": a, "b": b}
    assert winner(pool, strategy=L_LEAST) == "a"
    assert winner(pool, strategy=L_MOST) == "b"


def test_is_r1b_equal_spans_by_inferred_name_also_count_as_equal():
    a = bud("a", {"default/session": win(30, 60), "b/session": win(80, 60)})
    b = bud("b", {"5h": win(60, 60)})
    assert winner({"a": a, "b": b}, strategy=S_LEAST) == "a"


def test_is_r1b_remaining_compares_percentages_not_capacity():
    # b is a bigger plan with a smaller percentage left; only the percent counts.
    a = bud("a", {"gemini-5h": win(40, 60, limit=100)})
    b = bud("b", {"gemini-5h": win(60, 60, limit=100000)})
    assert winner({"a": a, "b": b}, strategy=S_MOST) == "a"


# ===========================================================================
# IS-R1c — window spans
# ===========================================================================

MIN = {"5h": 300, "day": 1440, "week": 10080, "month": 43200}
NAMES = {
    "5h": ["5h", "five_hour", "gemini-5h", "3p-5h", "codex-5h", "session",
           "default/session", "b/session", "default/5h", "b/gemini-5h",
           "default/five_hour"],
    "day": ["daily", "day", "default/daily", "b/day"],
    "week": ["weekly", "seven_day", "weekly_all", "default/weekly_all", "b/weekly_all",
             "gemini-weekly", "3p-weekly", "codex-weekly", "b/weekly",
             "default/seven_day"],
    "month": ["monthly", "b/monthly"],
}
INFERRED = [(name, kind) for kind, names in NAMES.items() for name in names]


def span_probe(name, reference_span, strategy):
    """X = {name: 90% used, no span; ref: 10% used at `reference_span`}.
    Y = {ref: 50% used at `reference_span`}.

    Under the `least_remaining` strategy of each direction, X wins exactly when
    X's shortest (or longest) window is the unnamed-span one: i.e. when the
    name's span is below (or above) the reference.
    """
    x = bud("x", {name: win(90, 4000), "ref": win(10, 4000, span=reference_span)})
    y = bud("y", {"ref": win(50, 4000, span=reference_span)})
    return winner({"x": x, "y": y}, strategy=strategy)


@pytest.mark.parametrize("name,kind", INFERRED)
def test_is_r1c_name_inference_table(name, kind):
    span = MIN[kind]
    # shortest: the name is shorter than a reference 1.5x its span, longer than one 0.7x.
    assert span_probe(name, span * 1.5, S_LEAST) == "x", f"{name!r} is not shorter than {span * 1.5}"
    assert span_probe(name, span * 0.7, S_LEAST) == "y", f"{name!r} is not longer than {span * 0.7}"
    # longest: mirrored.
    assert span_probe(name, span * 0.7, L_LEAST) == "x", f"{name!r} is not longer than {span * 0.7}"
    assert span_probe(name, span * 1.5, L_LEAST) == "y", f"{name!r} is not shorter than {span * 1.5}"


def test_is_r1c_the_four_classes_are_ordered_5h_day_week_month():
    x = bud("x", {"5h": win(90, 4000), "daily": win(40, 4000),
                  "weekly": win(10, 4000), "monthly": win(70, 4000)})
    # X's shortest is the 5h window (90% used), its longest the monthly (70%).
    # Y's one window sits between 5h and a day, then past a month.
    near = {"x": x, "y": bud("y", {"w": win(50, 4000, span=600)})}
    assert winner(near, strategy=S_LEAST) == "x"           # 90% used vs 50%
    far = {"x": x, "y": bud("y", {"w": win(50, 4000, span=100000)})}
    assert winner(far, strategy=L_LEAST) == "x"            # 70% used beats 50% used
    assert winner(far, strategy=L_MOST) == "y"


@pytest.mark.parametrize("name", ["rolling", "foo", "custom-window", "default/rolling",
                                  "weekly-ish", "hourly"])
def test_is_r1c_unknown_and_rolling_names_are_never_guessed(name):
    # Reset 100 minutes away: a span derived from the time left would call it short.
    x = bud("x", {name: win(90, 100), "weekly": win(10, 4000)})
    y = bud("y", {"weekly": win(50, 4000)})
    # With the unknown window out, X is its weekly window alone: 10% used.
    for strategy in (S_LEAST, L_LEAST):
        assert winner({"x": x, "y": y}, strategy=strategy) == "y"
    for strategy in (S_MOST, L_MOST):
        assert winner({"x": x, "y": y}, strategy=strategy) == "x"


@pytest.mark.parametrize("strategy", REMAINING)
def test_is_r1c_an_instance_with_only_unknown_spans_has_no_matching_window(strategy):
    x = bud("a", {"rolling": win(95 if strategy in (S_LEAST, L_LEAST) else 5, 100)})
    y = bud("b", {"weekly": win(50, 4000)})
    assert winner({"a": x, "b": y}, strategy=strategy) == "b"


def test_is_r1c_a_span_the_reading_carries_beats_the_name():
    x = bud("x", {"weekly": win(90, 4000, span=300), "ref": win(10, 4000, span=1440)})
    y = bud("y", {"ref": win(50, 4000, span=1440)})
    assert winner({"x": x, "y": y}, strategy=S_LEAST) == "x"
    named = bud("x", {"rolling": win(90, 4000, span=300), "ref": win(10, 4000, span=1440)})
    assert winner({"x": named, "y": y}, strategy=S_LEAST) == "x"


# --- the codex adapter keeps the span ---------------------------------------

def _codex_budget(tmp_path, fake, **extra):
    result = ch.invoke(["budget"], ch.base_env(tmp_path, fake, **extra), timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_is_r1c_codex_rollout_windows_carry_span_minutes(tmp_path):
    fake = ch.FakeCodex(tmp_path)
    now = time.time()
    ch.write_rollout(tmp_path / "profile", "a", [ch.token_count_line(
        now - 30, ch.window(25.0, 300, int(now + 3600)),
        ch.window(60.0, 10080, int(now + 4 * 86400)))])
    data = _codex_budget(tmp_path, fake)
    assert data["windows"]["5h"]["span_minutes"] == 300
    assert data["windows"]["weekly"]["span_minutes"] == 10080


def test_is_r1c_codex_windows_that_share_a_name_each_keep_their_own_span(tmp_path):
    fake = ch.FakeCodex(tmp_path)
    now = time.time()
    ch.write_rollout(tmp_path / "profile", "a", [ch.token_count_line(
        now - 30, ch.window(25.0, 300, int(now + 3600)),
        ch.window(60.0, 300, int(now + 7200)))])
    data = _codex_budget(tmp_path, fake)
    assert len(data["windows"]) == 2
    assert [w["span_minutes"] for w in data["windows"].values()] == [300, 300]


def test_is_r1c_codex_live_windows_carry_span_minutes(tmp_path):
    fake = ch.FakeCodexAppServer(tmp_path)
    now = time.time()
    fake.app_server(mode="ok", result=ch.rate_limits_response(ch.rl_snapshot(
        ch.rl_window(25, 300, int(now + 3600)),
        ch.rl_window(60, 10080, int(now + 4 * 86400)), limit_id="codex")))
    data = _codex_budget(tmp_path, fake)
    assert data["source"] == "app-server"
    assert data["windows"]["5h"]["span_minutes"] == 300
    assert data["windows"]["weekly"]["span_minutes"] == 10080


# ===========================================================================
# IS-R4 — eligibility is unchanged
# ===========================================================================

@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r4_an_instance_with_no_room_never_wins_whatever_its_score(strategy):
    a = bud("a", {"gemini-5h": win(99, 1), "gemini-weekly": win(99, 1)}, headroom=0.0)
    b = bud("b", {"gemini-5h": win(10, 600), "gemini-weekly": win(10, 6000)})
    assert winner({"a": a, "b": b}, strategy=strategy) == "b"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r4_an_instance_under_the_reserve_never_wins_when_the_reserve_applies(strategy):
    a = bud("a", {"gemini-5h": win(90, 1), "gemini-weekly": win(90, 1)}, headroom=0.10)
    b = bud("b", {"gemini-5h": win(20, 600), "gemini-weekly": win(20, 6000)}, headroom=0.8)
    assert winner({"a": a, "b": b}, strategy=strategy, reserved={"a", "b"}) == "b"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r4_instances_held_for_the_orchestrator_come_last(strategy):
    a = bud("a", {"gemini-5h": win(99, 1), "gemini-weekly": win(99, 1)})
    b = bud("b", {"gemini-5h": win(10, 600), "gemini-weekly": win(10, 6000)})
    assert winner({"a": a, "b": b}, strategy=strategy, reserved={"a"}) == "b"
    only = {"a": a}
    assert pick(["a"], only, strategy=strategy, reserved={"a"}) == "a"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r4_a_known_reading_beats_an_unknown_one_whatever_the_score(strategy):
    unknown = bud("a", {"gemini-5h": win(99, 1), "gemini-weekly": win(99, 1)}, known=False)
    known = bud("b", {"gemini-5h": win(10, 900), "gemini-weekly": win(10, 9000)})
    assert winner({"a": unknown, "b": known}, strategy=strategy) == "b"
    assert winner({"a": unknown}, strategy=strategy) == "a"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r4_nothing_eligible_is_still_none(strategy):
    pool = {"a": bud("a", {}, headroom=0.0), "b": bud("b", {}, headroom=0.01)}
    assert pick(list(pool), pool, strategy=strategy) is None


def test_is_r4_outside_a_family_routing_is_unchanged():
    budgets = {"opencode": Budget("opencode", known=True, headroom=0.9,
                                  windows={"5h": win(10, 5)}),
               "agy": Budget("agy", known=True, headroom=0.0)}
    kwargs = dict(reserve=0.15, reserved=set(), allowed={"agy", "opencode"})
    assert choose_provider("agy", budgets, ["opencode", "defer"], **kwargs)[0] == "opencode"
    assert choose_provider("opencode", budgets, ["agy"], reserve=0.15, reserved=set()) \
        == ("opencode", "preferred provider has headroom")
    exhausted = {"opencode": Budget("opencode", known=True, headroom=0.0),
                 "agy": Budget("agy", known=True, headroom=0.0)}
    assert choose_provider("agy", exhausted, ["opencode", "defer"], **kwargs)[0] is None


def test_is_r4_a_pool_with_no_window_data_still_orders_by_load_last_use_name():
    # Readings that never heard of windows rank exactly as before, by default.
    pool = {n: Budget(n, known=True, headroom=0.9) for n in ("a", "b", "c")}
    assert winner(pool, load={"a": 2, "b": 1, "c": 1}, last={"b": 5.0, "c": 1.0}) == "c"
    assert winner(pool) == "a"


# ===========================================================================
# IS-R3 / R3a — the reason names what decided
# ===========================================================================

def agy_pool(a_windows, b_windows, **kw):
    return {"agy": bud("agy", a_windows, **kw.get("a", {})),
            "agy-b": bud("agy-b", b_windows, **kw.get("b", {}))}


def test_is_r3_soonest_reset_reason_names_the_strategy_the_winner_and_the_window():
    pool = agy_pool({"gemini-5h": win(50, 200)}, {"gemini-5h": win(50, 40)})
    chosen, why = choose(pool, strategy=SOONEST)
    assert chosen == "agy-b"
    assert SOONEST in why and "agy-b" in why and "gemini-5h" in why


def test_is_r3_default_strategy_reason_names_soonest_reset():
    pool = agy_pool({"gemini-5h": win(50, 200)}, {"gemini-5h": win(50, 40)})
    chosen, why = choose(pool)
    assert chosen == "agy-b" and SOONEST in why


@pytest.mark.parametrize("strategy,window", [
    (S_MOST, "gemini-5h"), (S_LEAST, "gemini-5h"),
    (L_MOST, "gemini-weekly"), (L_LEAST, "gemini-weekly"),
])
def test_is_r3_remaining_strategy_reasons_name_strategy_window_and_both_figures(
        strategy, window):
    # 84% left against 9% left; the strategy decides which of them wins.
    winner_left, loser_left = (84, 9) if strategy in (S_MOST, L_MOST) else (9, 84)

    def reading(left):
        return {"gemini-5h": win(100 - left, 60), "gemini-weekly": win(100 - left, 4000)}

    pool = agy_pool(reading(loser_left), reading(winner_left))
    chosen, why = choose(pool, strategy=strategy)
    assert chosen == "agy-b"
    assert strategy in why and "agy-b" in why and window in why
    assert re.search(rf"\b{winner_left}\b", why) and re.search(rf"\b{loser_left}\b", why), why


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r3a_the_preferred_providers_win_keeps_its_wording(strategy):
    same = {"gemini-5h": win(50, 60), "gemini-weekly": win(50, 4000)}
    pool = agy_pool(dict(same), dict(same))
    assert choose(pool, strategy=strategy) == ("agy", "preferred provider has headroom")


def test_is_r3a_within_the_tolerance_load_decided_and_the_reason_says_load():
    pool = agy_pool({"gemini-5h": win(50, 60)}, {"gemini-5h": win(50, 70)})
    chosen, why = choose(pool, strategy=SOONEST, load={"agy": 2, "agy-b": 1})
    assert chosen == "agy-b"
    assert "fewer running agents" in why and "agy-b" in why
    assert "gemini-5h" not in why


def test_is_r3a_within_the_tolerance_last_use_decided_and_the_reason_says_so():
    pool = agy_pool({"gemini-5h": win(50, 70)}, {"gemini-5h": win(50, 60)})
    chosen, why = choose(pool, strategy=SOONEST, load={"agy": 1, "agy-b": 1},
                         last={"agy": 100.0, "agy-b": 50.0})
    assert chosen == "agy-b"
    assert "used less recently" in why and "gemini-5h" not in why


def test_is_r3a_within_the_tolerance_the_name_decided_and_the_reason_says_so():
    pool = {"agy": bud("agy", {"gemini-5h": win(50, 70)}),
            "agy-b": bud("agy-b", {"gemini-5h": win(50, 60)})}
    chosen, why = choose(pool, preferred="agy-b", strategy=SOONEST)
    assert chosen == "agy"
    assert "name tie-break" in why and "agy" in why and "gemini-5h" not in why


@pytest.mark.parametrize("strategy", REMAINING)
def test_is_r3a_remaining_strategy_within_tolerance_does_not_quote_a_quota_comparison(strategy):
    left_wins = strategy in (S_MOST, L_MOST)
    a_used, b_used = (60, 58) if left_wins else (58, 60)

    def reading(used):
        return {"gemini-5h": win(used, 60), "gemini-weekly": win(used, 4000)}

    pool = agy_pool(reading(a_used), reading(b_used))
    chosen, why = choose(pool, strategy=strategy, load={"agy": 3})
    assert chosen == "agy-b"
    assert "fewer running agents" in why
    assert "gemini-5h" not in why and "gemini-weekly" not in why
    assert "42%" not in why and "40%" not in why


def test_is_r3a_a_metric_gap_beyond_tolerance_is_named_even_when_load_also_differs():
    pool = agy_pool({"gemini-5h": win(50, 200)}, {"gemini-5h": win(50, 40)})
    chosen, why = choose(pool, strategy=SOONEST, load={"agy": 2, "agy-b": 0})
    assert chosen == "agy-b"
    assert SOONEST in why and "gemini-5h" in why


def test_is_r3a_the_reason_does_not_claim_a_reset_comparison_when_the_preferred_has_no_data():
    pool = agy_pool({"gemini-5h": win(50)}, {"gemini-5h": win(50, 40)})
    chosen, why = choose(pool, strategy=SOONEST)
    assert chosen == "agy-b"
    assert re.search(r"no (reset|window|quota|data|usable)|missing|unknown", why, re.I), why


def test_is_r3a_stale_preferred_reading_is_named_as_missing_not_as_a_comparison():
    pool = agy_pool({"gemini-5h": win(50, 500)}, {"gemini-5h": win(50, 40)},
                    a={"stale": True})
    chosen, why = choose(pool, strategy=SOONEST)
    assert chosen == "agy-b"
    assert "resets" not in why and "before" not in why


def test_is_r3a_when_neither_has_data_load_decides_without_a_metric_claim():
    pool = agy_pool({"rolling": win(50, 10)}, {"rolling": win(50, 10)})
    chosen, why = choose(pool, strategy=L_MOST, load={"agy": 2, "agy-b": 1})
    assert chosen == "agy-b"
    assert "fewer running agents" in why and "rolling" not in why


def test_is_r3a_a_tied_group_does_not_credit_the_metric_to_a_winner_chosen_by_load():
    # three instances: agy 70 min, agy-b 75 min (tied with agy), agy-c far later.
    pool = {"agy": bud("agy", {"gemini-5h": win(50, 70)}),
            "agy-b": bud("agy-b", {"gemini-5h": win(50, 75)}),
            "agy-c": bud("agy-c", {"gemini-5h": win(50, 600)})}
    chosen, why = choose(pool, strategy=SOONEST, load={"agy": 2})
    assert chosen == "agy-b"
    assert "fewer running agents" in why and "gemini-5h" not in why


@pytest.mark.parametrize("reserved,headroom,expected", [
    ({"agy"}, 0.9, "agy is held for the orchestrator; using agy-b"),
    (set(), 0.0, "agy is constrained; using agy-b"),
])
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r3a_constrained_and_held_keep_their_wording_whatever_the_strategy(
        strategy, reserved, headroom, expected):
    pool = agy_pool({"gemini-5h": win(50, 5), "gemini-weekly": win(50, 4000)},
                    {"gemini-5h": win(50, 600), "gemini-weekly": win(50, 9000)},
                    a={"headroom": headroom})
    chosen, why = choose(pool, strategy=strategy, reserved=reserved)
    assert (chosen, why) == ("agy-b", expected)


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_is_r3a_an_unknown_preferred_reading_keeps_its_wording(strategy):
    pool = agy_pool({"gemini-5h": win(50, 5)}, {"gemini-5h": win(50, 600)},
                    a={"known": False})
    chosen, why = choose(pool, strategy=strategy)
    assert (chosen, why) == ("agy-b", "agy has no quota reading; using agy-b")


def test_is_r3_least_loaded_keeps_todays_reasons():
    pool = agy_pool({"gemini-5h": win(50, 5)}, {"gemini-5h": win(50, 600)})
    chosen, why = choose(pool, strategy="least_loaded", load={"agy": 2, "agy-b": 1})
    assert (chosen, why) == ("agy-b", "sharing accounts: agy-b has fewer running agents")


# ===========================================================================
# IS-R2 / R2a — configuration, end to end through Runner.start
# ===========================================================================

def reading(five_used, five_reset, week_used):
    return {"gemini-5h": win(five_used, five_reset),
            "gemini-weekly": win(week_used, 7000)}


# acme resets in 2h with 80% of the week left; bravo in 30 min with 20% left.
# soonest_reset -> bravo; longest_window_most_remaining -> acme.
CLASSIC = dict(acme=reading(50, 120, 20), bravo=reading(50, 30, 80))


def run_route(tmp_path, monkeypatch, readings, *, project_budget=None, extras=None,
              preferred="acme", providers=("acme", "bravo")):
    tmp_path.mkdir(parents=True, exist_ok=True)
    configs, probes = {}, {}
    for name in providers:
        configs[name], probes[name] = _fake_cli(tmp_path, name)
        configs[name]["family"] = "acme"
    for name, extra in (extras or {}).items():
        configs[name].update(extra)
    other = [n for n in providers if n != preferred]
    agent = AgentSpec.from_dict("worker", {"provider": preferred, "model": "m1",
                                           "models": {n: "m1" for n in other}})
    runner = h.make_runner(tmp_path / "project", monkeypatch, agents={"worker": agent},
                           providers=configs,
                           project={"budget": dict(project_budget or {})})
    budgets = {name: Budget(name, known=True, headroom=0.5, windows=windows)
               for name, windows in readings.items()}
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: budgets)

    async def go():
        result = await runner.start("worker", "work")
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result

    result = asyncio.run(go())
    ran = [n for n in providers if _calls(probes[n])]
    assert ran == [result.get("provider")], (ran, result)
    return result


def test_is_r2_default_is_soonest_reset(tmp_path, monkeypatch):
    result = run_route(tmp_path, monkeypatch, CLASSIC)
    assert result["provider"] == "bravo"
    assert SOONEST in result["routing"]


def test_is_r2_the_global_setting_replaces_the_default(tmp_path, monkeypatch):
    result = run_route(tmp_path, monkeypatch, CLASSIC,
                       project_budget={"instance_strategy": L_MOST})
    assert result["provider"] == "acme"
    assert L_MOST in result["routing"]


def test_is_r2_the_provider_setting_wins_over_the_global_one(tmp_path, monkeypatch):
    result = run_route(tmp_path, monkeypatch, CLASSIC,
                       project_budget={"instance_strategy": L_MOST},
                       extras={"acme": {"instance_strategy": SOONEST}})
    assert result["provider"] == "bravo"
    result = run_route(tmp_path / "again", monkeypatch, CLASSIC,
                       project_budget={"instance_strategy": SOONEST},
                       extras={"acme": {"instance_strategy": L_MOST}})
    assert result["provider"] == "acme"


def test_is_r2_the_provider_setting_wins_over_the_default(tmp_path, monkeypatch):
    # Reversed readings: here the default (soonest_reset) and today's name
    # tie-break both say acme; only the provider's own setting says bravo.
    reversed_ = dict(acme=reading(50, 30, 20), bravo=reading(50, 120, 80))
    result = run_route(tmp_path, monkeypatch, reversed_,
                       extras={"acme": {"instance_strategy": L_MOST}})
    assert result["provider"] == "bravo"
    assert L_MOST in result["routing"]


def test_is_r2a_an_inherited_provider_setting_counts(tmp_path, monkeypatch):
    # bravo extends acme and says nothing; acme says longest_window_most_remaining.
    result = run_route(tmp_path, monkeypatch, CLASSIC, preferred="bravo",
                       project_budget={"instance_strategy": SOONEST},
                       extras={"acme": {"instance_strategy": L_MOST},
                               "bravo": {"extends": "acme"}})
    assert result["provider"] == "acme"
    assert L_MOST in result["routing"]


def test_is_r2a_explicit_null_clears_an_inherited_provider_setting(tmp_path, monkeypatch):
    result = run_route(tmp_path, monkeypatch, CLASSIC, preferred="bravo",
                       project_budget={"instance_strategy": L_MOST},
                       extras={"acme": {"instance_strategy": SOONEST},
                               "bravo": {"extends": "acme", "instance_strategy": None}})
    assert result["provider"] == "acme"          # the global setting applies
    assert L_MOST in result["routing"]


def test_is_r2a_a_siblings_own_setting_is_ignored_when_it_is_not_preferred(
        tmp_path, monkeypatch):
    result = run_route(tmp_path, monkeypatch, CLASSIC, preferred="acme",
                       extras={"bravo": {"instance_strategy": L_MOST}})
    assert result["provider"] == "bravo"         # the default decided
    result = run_route(tmp_path / "again", monkeypatch, CLASSIC, preferred="bravo",
                       project_budget={"instance_strategy": L_MOST},
                       extras={"acme": {"instance_strategy": SOONEST}})
    assert result["provider"] == "acme"          # the global one decided


def test_is_r1a_the_tolerance_keys_are_read_from_the_project_budget_minutes(
        tmp_path, monkeypatch):
    # bravo resets 10 minutes before acme: tied by default, so the name decides.
    readings = dict(acme=reading(50, 60, 50), bravo=reading(50, 50, 50))
    result = run_route(tmp_path, monkeypatch, readings)
    assert result["provider"] == "acme"
    result = run_route(tmp_path / "strict", monkeypatch, readings,
                       project_budget={"instance_tolerance_minutes": 5})
    assert result["provider"] == "bravo"


def test_is_r1a_the_tolerance_keys_are_read_from_the_project_budget_points(
        tmp_path, monkeypatch):
    # bravo has 8 points more of the week left: ahead by default.
    readings = dict(acme=reading(50, 60, 28), bravo=reading(50, 60, 20))
    result = run_route(tmp_path, monkeypatch, readings,
                       project_budget={"instance_strategy": L_MOST})
    assert result["provider"] == "bravo"
    result = run_route(tmp_path / "wide", monkeypatch, readings,
                       project_budget={"instance_strategy": L_MOST,
                                       "instance_tolerance_points": 10})
    assert result["provider"] == "acme"


# --- config layering and bad values ------------------------------------------

def layered(tmp_path, monkeypatch, project_yaml=None, global_yaml=None, providers_yaml=None):
    root = h.make_git_repo(tmp_path / "proj")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    gdir = tmp_path / "gconf"
    gdir.mkdir(exist_ok=True)
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    if project_yaml is not None:
        (paths.config / "project.yaml").write_text(project_yaml)
    if global_yaml is not None:
        (gdir / "project.yaml").write_text(global_yaml)
    if providers_yaml is not None:
        (paths.config / "providers.yaml").write_text(providers_yaml)
    return paths, gdir


def test_is_r2_project_config_overrides_global_for_strategy_and_tolerances(tmp_path, monkeypatch):
    paths, _ = layered(
        tmp_path, monkeypatch,
        project_yaml=f"budget:\n  instance_strategy: {L_MOST}\n  instance_tolerance_points: 9\n",
        global_yaml=(f"budget:\n  instance_strategy: {S_LEAST}\n"
                     "  instance_tolerance_points: 3\n  instance_tolerance_minutes: 20\n"))
    cfg = config_mod.load(paths, seed=False)
    budget = cfg.project["budget"]
    assert budget["instance_strategy"] == L_MOST
    assert budget["instance_tolerance_points"] == 9
    assert budget["instance_tolerance_minutes"] == 20      # only global sets it


def test_is_r2_the_global_config_applies_when_the_project_is_silent(tmp_path, monkeypatch):
    paths, _ = layered(tmp_path, monkeypatch, project_yaml="budget:\n  reserve: false\n",
                       global_yaml=f"budget:\n  instance_strategy: {S_MOST}\n")
    assert config_mod.load(paths, seed=False).project["budget"]["instance_strategy"] == S_MOST


@pytest.mark.parametrize("value", ["bogus", "", "5", "soonest-reset", "[soonest_reset]"])
def test_is_r2_an_unknown_project_strategy_is_a_config_error_with_file_and_line(
        tmp_path, monkeypatch, value):
    paths, _ = layered(tmp_path, monkeypatch,
                       project_yaml=f"budget:\n  reserve: false\n  instance_strategy: {value or chr(34) * 2}\n")
    with pytest.raises(ValueError) as caught:
        config_mod.load(paths, seed=False)
    text = str(caught.value)
    assert "project.yaml" in text and re.search(r"project\.yaml:3\b", text), text
    assert "instance_strategy" in text


def test_is_r2_an_unknown_global_strategy_is_a_config_error_with_file_and_line(
        tmp_path, monkeypatch):
    paths, gdir = layered(tmp_path, monkeypatch,
                          global_yaml="# top\nbudget:\n  instance_strategy: bogus\n")
    with pytest.raises(ValueError) as caught:
        config_mod.load(paths, seed=False)
    text = str(caught.value)
    assert "bogus" in text and re.search(r"project\.yaml:3\b", text) and str(gdir) in text


def test_is_r2_an_unknown_provider_strategy_is_a_config_error_with_file_and_line(
        tmp_path, monkeypatch):
    paths, _ = layered(tmp_path, monkeypatch, providers_yaml=(
        "providers:\n  acme-b:\n    extends: claude\n    instance_strategy: bogus\n"))
    with pytest.raises(ValueError) as caught:
        config_mod.load(paths, seed=False)
    text = str(caught.value)
    assert "bogus" in text and re.search(r"providers\.yaml:4\b", text), text


def test_is_r2_load_providers_refuses_an_unknown_strategy_instead_of_defaulting():
    with pytest.raises(ValueError):
        load_providers({"acme": {"bin": "acme", "instance_strategy": "bogus"}})


@pytest.mark.parametrize("value", [*STRATEGIES, None])
def test_is_r2_every_named_strategy_and_null_load_cleanly(tmp_path, monkeypatch, value):
    block = "null" if value is None else value
    paths, _ = layered(tmp_path, monkeypatch, providers_yaml=(
        f"providers:\n  acme-b:\n    extends: claude\n    instance_strategy: {block}\n"),
        project_yaml=(f"budget:\n  instance_strategy: {value}\n" if value else None))
    config_mod.load(paths, seed=False)
    load_providers({"acme": {"bin": "acme", "instance_strategy": value}})


@pytest.fixture
def doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *a: {})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *a: {})
    monkeypatch.setattr(cli, "read_all", lambda *a: {})
    monkeypatch.setattr(cli, "_report_agents", lambda *a: 0)
    monkeypatch.setattr(cli, "find_shadowing", lambda *a: [])
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *a: 0)
    counter = {"n": 0}

    def run(project_yaml=None, providers_yaml=None):
        counter["n"] += 1
        sub = tmp_path / f"doc{counter['n']}"
        paths, _ = layered(sub, monkeypatch, project_yaml=project_yaml,
                           providers_yaml=providers_yaml)
        capsys.readouterr()
        code = cli.cmd_doctor(argparse.Namespace(path=str(paths.root), clear=None, force=False))
        out = capsys.readouterr().out
        match = re.search(r"^(\d+) problem\(s\)$", out, re.M)
        return code, int(match.group(1)) if match else 0, out

    return run


def test_is_r2_doctor_reports_an_unknown_global_strategy(doctor):
    _, base, _ = doctor("budget:\n  reserve: false\n")
    code, problems, out = doctor("budget:\n  reserve: false\n  instance_strategy: bogus\n")
    assert code == 1 and problems > base, out
    assert "bogus" in out


def test_is_r2_doctor_reports_an_unknown_provider_strategy(doctor):
    _, base, _ = doctor(providers_yaml="providers:\n  acme-b:\n    extends: claude\n")
    code, problems, out = doctor(providers_yaml=(
        "providers:\n  acme-b:\n    extends: claude\n    instance_strategy: bogus\n"))
    assert code == 1 and problems > base, out
    assert "bogus" in out and "acme-b" in out


def test_is_r2_doctor_has_no_problem_for_a_named_strategy(doctor):
    base_code, base, _ = doctor("budget:\n  reserve: false\n")
    code, problems, out = doctor(f"budget:\n  reserve: false\n  instance_strategy: {S_MOST}\n")
    assert problems == base and code == base_code, out
