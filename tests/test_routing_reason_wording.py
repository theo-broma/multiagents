"""Sibling routing explains the criterion without changing the selection."""

import pytest

from multiagents.budget import Budget, choose_provider, pick_instance


@pytest.mark.parametrize(
    "headroom,known,reserved,load,last_used,reason",
    [
        (0.0, True, set(), {}, {}, "agy is constrained; using agy-b"),
        (0.9, True, {"agy"}, {}, {},
         "agy is held for the orchestrator; using agy-b"),
        (0.1, True, {"agy"}, {}, {}, "agy is constrained; using agy-b"),
        (0.9, True, {"agy", "agy-b"}, {"agy": 2, "agy-b": 1}, {},
         "sharing accounts: agy-b has fewer running agents"),
        (0.9, True, {"agy", "agy-b"}, {}, {"agy": 100, "agy-b": 50},
         "sharing accounts: agy-b was used less recently"),
        (None, False, set(), {"agy-b": 5}, {},
         "agy has no quota reading; using agy-b"),
        (0.9, True, set(), {"agy": 2, "agy-b": 1}, {"agy-b": 100},
         "sharing accounts: agy-b has fewer running agents"),
        (0.9, True, set(), {"agy": 1, "agy-b": 1}, {"agy": 100, "agy-b": 50},
         "sharing accounts: agy-b was used less recently"),
    ],
)
def test_sibling_reason_matches_winning_criterion(
    headroom, known, reserved, load, last_used, reason,
):
    budgets = {
        "agy": Budget("agy", known=known, headroom=headroom),
        "agy-b": Budget("agy-b", known=True, headroom=0.9),
    }
    family = ["agy", "agy-b"]
    expected = pick_instance(family, budgets, 0.15, reserved, load, last_used)
    chosen, why = choose_provider(
        "agy", budgets, [], reserve=0.15, reserved=reserved,
        family=family, load=load, last_used=last_used,
    )
    assert chosen == expected == "agy-b"
    assert why == reason


def test_name_tie_break_does_not_claim_load_or_quota_pressure():
    budgets = {name: Budget(name, known=True, headroom=0.9)
               for name in ["agy", "agy-b"]}
    chosen, why = choose_provider(
        "agy-b", budgets, [], reserved=set(), family=["agy", "agy-b"],
    )
    assert chosen == pick_instance(list(budgets), budgets, 0.15, set()) == "agy"
    assert why == "sharing accounts: agy wins the account name tie-break"
