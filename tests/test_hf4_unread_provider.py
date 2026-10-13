"""HF4.1-HF4.4 / F121: unread quota is usable in every routing position.

Contract: context/specs/findings-c1-c2-part1.md, including the Decisions.
Only choose_provider's public inputs and returned provider/reason are checked.
The existing Budget.stale flag represents a reading already aged out for routing;
age computation belongs to the reader's existing RM-R4b tests.
"""

from datetime import datetime, timezone
import re

import pytest

from multiagents import budget as budget_mod
from multiagents.budget import Budget, choose_provider


NOW = 2_000_000_000.0
POSITIONS = ("preferred", "sibling", "route", "chain")
UNREAD = ("missing", "unknown", "unknown-zero", "stale-empty",
          "stale-below-reserve", "known-without-headroom")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    # No reset/cooldown test waits for real time to pass.
    monkeypatch.setattr(budget_mod.time, "time", lambda: NOW)


def unread(name, state):
    if state == "missing":
        return None
    fields = {
        "unknown": dict(known=False),
        "unknown-zero": dict(known=False, headroom=0.0),
        "stale-empty": dict(known=True, headroom=0.0, stale=True),
        "stale-below-reserve": dict(known=True, headroom=0.10, stale=True),
        "known-without-headroom": dict(known=True, headroom=None),
    }
    return Budget(name, **fields[state])


def positioned(position, state, **overrides):
    """Put the sole usable candidate in the requested public routing slot."""
    target = "p" if position == "preferred" else "q"
    budgets = {"p": Budget("p", known=True, headroom=0.0)}
    reading = unread(target, state)
    if reading is None:
        budgets.pop(target, None)
    else:
        budgets[target] = reading
    options = dict(reserve=0.15, reserved={"p", "q"}, allowed={"p", "q"})
    chain = []
    if position == "sibling":
        options["family"] = ["p", "q"]
    elif position == "route":
        options["routes"] = ["q"]
    elif position == "chain":
        chain = ["q"]
    options.update(overrides)
    return target, budgets, chain, options


def assert_no_reading_reason(reason):
    assert re.search(r"\bno (?:quota |budget )?reading\b", reason.lower()), reason
    assert "has headroom" not in reason.lower(), reason


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("state", UNREAD)
def test_hf4_1_no_reading_is_usable_even_when_reserved(position, state):
    target, budgets, chain, options = positioned(position, state)
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == target, reason


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("state", UNREAD)
def test_hf4_2_unread_winner_reason_says_no_reading(position, state):
    target, budgets, chain, options = positioned(position, state)
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == target, reason
    assert_no_reading_reason(reason)


@pytest.mark.parametrize("position", POSITIONS)
def test_hf4_2_stale_roomy_reading_never_claims_current_headroom(position):
    target, budgets, chain, options = positioned(position, "stale-below-reserve")
    budgets[target].headroom = 0.9
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == target, reason
    assert_no_reading_reason(reason)


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("state", [s for s in UNREAD if s != "missing"])
def test_hf4_1_active_cooldown_still_blocks_unread_candidates(position, state):
    target, budgets, chain, options = positioned(position, state)
    budgets[target].cooldown_until = NOW + 1
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen is None, reason


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("offset", [-1, 0])
def test_hf4_1_expired_cooldown_does_not_block_no_reading(position, offset):
    target, budgets, chain, options = positioned(position, "unknown")
    budgets[target].cooldown_until = NOW + offset
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == target, reason


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("state", ["missing", "known-without-headroom"])
def test_hf4_1_allowed_still_excludes_unread_candidates(position, state):
    target, budgets, chain, options = positioned(position, state, allowed=set())
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen is None, reason


def test_hf4_1_disallowed_unread_preferred_does_not_shadow_allowed_sibling():
    chosen, reason = choose_provider(
        "p", {}, [], reserved=set(), allowed={"q"}, family=["p", "q"],
        load={"q": 9}, last_used={"q": NOW},
    )
    assert chosen == "q", reason


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("headroom,usable", [
    (-0.1, False), (0.0, False), (0.02, False), (0.021, True),
])
def test_hf4_1_fresh_readings_keep_the_usable_floor(position, headroom, usable):
    target, budgets, chain, options = positioned(position, "unknown", reserved=set())
    budgets[target] = Budget(target, known=True, headroom=headroom)
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == (target if usable else None), reason


@pytest.mark.parametrize("tier", ["A", "B", "C"])
@pytest.mark.parametrize("state", UNREAD)
def test_hf4_3_known_roomy_beats_unread_then_unread_beats_constrained(tier, state):
    budgets = {
        "p": Budget("p", known=True, headroom=0.0),
        "empty": Budget("empty", known=True, headroom=0.0),
        "low": Budget("low", known=True, headroom=0.149),
        "roomy": Budget("roomy", known=True, headroom=0.15),
    }
    reading = unread("unread", state)
    if reading is not None:
        budgets["unread"] = reading
    candidates = ["unread", "empty", "low", "roomy"]
    options = dict(reserved=set(candidates), allowed={"p", *candidates},
                   load={"unread": 0, "roomy": 9},
                   last_used={"unread": 0, "roomy": NOW})
    chain = []
    if tier == "A":
        options["family"] = ["p", *candidates]
    elif tier == "B":
        options["routes"] = candidates
    else:
        chain = candidates
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == "roomy", reason

    # The fresh provider's usable headroom crosses the reserve boundary.
    budgets["roomy"] = Budget("roomy", known=True, headroom=0.149)
    chosen, reason = choose_provider("p", budgets, chain, **options)
    assert chosen == "unread", reason


def test_hf4_3_reservation_precedence_keeps_unread_worker_ahead_of_known_owner():
    budgets = {"owner": Budget("owner", known=True, headroom=0.9)}
    chosen, reason = choose_provider(
        "owner", budgets, [], family=["owner", "worker"],
        reserved={"owner"}, allowed={"owner", "worker"},
        load={"worker": 9}, last_used={"worker": NOW},
    )
    assert chosen == "worker", reason
    assert_no_reading_reason(reason)


@pytest.mark.parametrize("minutes,loads,last_used,expected", [
    ((30, 90), {"early": 9}, {}, "early"),
    ((30, 40), {"early": 2, "later": 1}, {}, "later"),
    ((30, 40), {"early": 1, "later": 1}, {"early": 20, "later": 10}, "later"),
    ((30, 40), {}, {}, "early"),
])
def test_hf4_3_tier_a_keeps_reset_tolerance_load_and_last_use(
    minutes, loads, last_used, expected,
):
    budgets = {"unread": Budget("unread", known=False)}
    for name, delay in zip(["early", "later"], minutes):
        reset = datetime.fromtimestamp(NOW + delay * 60, timezone.utc).isoformat()
        budgets[name] = Budget(name, known=True, headroom=0.5,
                               windows={"5h": {"percent": 50, "resets_at": reset}})
    chosen, reason = choose_provider(
        "unread", budgets, [], family=["unread", "early", "later"],
        reserved=set(), load=loads, last_used=last_used,
        strategy="soonest_reset", tolerance_minutes=15,
    )
    assert chosen == expected, reason


@pytest.mark.parametrize("state", UNREAD)
def test_hf4_4_unread_tier_b_route_precedes_known_tier_c(state):
    budgets = {"p": Budget("p", known=True, headroom=0.0),
               "chain": Budget("chain", known=True, headroom=0.9)}
    reading = unread("route", state)
    if reading is not None:
        budgets["route"] = reading
    chosen, reason = choose_provider(
        "p", budgets, ["chain"], routes=["route"], reserved=set(),
    )
    assert chosen == "route", reason
    assert "agent's own models list" in reason, reason
    assert_no_reading_reason(reason)


@pytest.mark.parametrize("position", ["preferred", "sibling"])
def test_hf4_4_unread_tier_a_precedes_known_routes_and_chain(position):
    budgets = {"route": Budget("route", known=True, headroom=0.9),
               "chain": Budget("chain", known=True, headroom=0.9)}
    options = dict(reserved=set())
    target = "p"
    if position == "sibling":
        budgets["p"] = Budget("p", known=True, headroom=0.0)
        options["family"] = ["p", "unread"]
        target = "unread"
    chosen, reason = choose_provider(
        "p", budgets, ["chain"], routes=["route"], **options,
    )
    assert chosen == target, reason
    assert_no_reading_reason(reason)


@pytest.mark.parametrize("within,expected", [(0, "route"), (49, "route"),
                                            (50, None), (51, None)])
def test_hf4_4_short_reset_wait_stays_before_unread_cross_family_fallback(within, expected):
    budgets = {
        "p": Budget("p", known=True, headroom=0.0, cooldown_until=NOW + 500),
        "sibling": Budget("sibling", known=True, headroom=0.0,
                          resets_at=datetime.fromtimestamp(NOW + 50, timezone.utc).isoformat()),
        "chain": Budget("chain", known=True, headroom=0.9),
    }
    chosen, reason = choose_provider(
        "p", budgets, ["chain"], family=["p", "sibling"], routes=["route"],
        reserved=set(), wait_for_reset_within=within,
    )
    assert chosen == expected, reason
    if expected is None:
        assert "waiting rather than moving the work" in reason, reason
    else:
        assert_no_reading_reason(reason)


@pytest.mark.parametrize("tier", ["B", "C"])
def test_hf4_1_defer_still_prevents_using_an_unread_candidate_after_it(tier):
    options = dict(reserved=set())
    chain = ["unread"]
    if tier == "B":
        options["routes"] = ["defer", "unread"]
    else:
        chain = ["defer", "unread"]
    chosen, reason = choose_provider(
        "p", {"p": Budget("p", known=True, headroom=0.0)}, chain, **options,
    )
    assert chosen is None, reason
