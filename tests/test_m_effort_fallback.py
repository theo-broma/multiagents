"""Regression tests for the effort settlement in start()'s routing loop.

The Gemini reviewer (ag-37f81c) found that `_settle_effort` ran inside the
provider loop, before `startup.claim` succeeded: a failed claim left the
next candidate the previous one's normalised effort and an
`effort_normalised` event naming a provider the run never launched on.

The contract (m-routing-fixes.md, RM-R5a with RM-R7 and the reviewer's
findings): each candidate is checked and settled from the un-normalised
spec; the normalisation event is emitted only for the provider actually
launched; an explicit contradiction is still refused before any claim.

Reuses the black-box seams of tests/test_m_routing_fixes.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.startup import StartupUnavailable  # noqa: E402
from test_conversation_provider_change import _calls  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    SUFFIXES, _budgets, _effort_argv, _events, _fakes, _project, _start,
)


def _two_candidates(tmp_path, monkeypatch, *, top_effort=True):
    """`acme` (preferred, model implies medium) and `zeta` (route, model
    implies high), both with `effort_suffixes`, both with headroom."""
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    for name in ("acme", "zeta"):
        providers[name]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-medium",
        **({"effort": "low"} if top_effort else {}),
        "models": {"zeta": "gem-high"},
    })
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["defer"]}})
    _budgets(monkeypatch, acme=1.0, zeta=1.0)
    return runner, probes


def _fail_first_claim(runner, monkeypatch, name="acme"):
    """The PS-R5b race: another server takes the candidate's only probe
    between `availability` and `claim`, so its claim raises."""
    real_claim = runner.startup.claim

    def claim(provider, run_id):
        if provider == name:
            raise StartupUnavailable(provider)
        return real_claim(provider, run_id)

    monkeypatch.setattr(runner.startup, "claim", claim)


def test_a_failed_claim_leaves_no_normalisation_for_a_provider_never_launched(
        tmp_path, monkeypatch):
    """The run falls through to zeta, whose OWN model governs: effort high,
    one event, naming zeta. Nothing records acme's medium."""
    runner, probes = _two_candidates(tmp_path, monkeypatch)
    _fail_first_claim(runner, monkeypatch)

    result = _start(runner)

    assert result.get("provider") == "zeta" and not result.get("error"), result
    argv, effort = _effort_argv(probes)
    assert effort == "high", argv
    assert "gem-high" in argv, argv
    events = [e for e in _events(runner) if e.get("kind") == "effort_normalised"]
    assert [e.get("provider") for e in events] == ["zeta"], (
        f"the normalisation events name providers the run never launched "
        f"on: {events}")
    node = runner.tree.get(result["agent_id"])
    assert node.effort == "high", node


def test_each_candidate_settles_from_the_un_normalised_spec(tmp_path, monkeypatch):
    """Both models imply the SAME effort only through their own suffix map:
    give zeta a model with no implied effort, so a candidate that arrived
    still carrying acme's normalisation would keep it; the launched pair
    must carry zeta's own inheritance instead."""
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    providers["zeta"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-medium", "effort": "low",
        "models": {"zeta": "gem-plain"},       # implies nothing
    })
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["defer"]}})
    _budgets(monkeypatch, acme=1.0, zeta=1.0)
    _fail_first_claim(runner, monkeypatch)

    result = _start(runner)

    assert result.get("provider") == "zeta" and not result.get("error"), result
    argv, effort = _effort_argv(probes)
    # gem-plain implies nothing, so the inherited low survives as written —
    # an inherited acme normalisation (medium) would show here.
    assert effort == "low", argv
    events = [e for e in _events(runner) if e.get("kind") == "effort_normalised"]
    assert events == [], f"zeta's model implies no effort, yet: {events}"
    node = runner.tree.get(result["agent_id"])
    assert node.effort == "low", node


def test_an_explicit_conflict_on_the_first_candidate_still_refuses_before_the_claim(
        tmp_path, monkeypatch):
    """RM-R7 in the loop: the conflict refusal wins over the claim failure —
    the start is refused for the pair, and zeta is never tried."""
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    for name in ("acme", "zeta"):
        providers[name]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-medium",
        "models": {"zeta": {"model": "gem-high", "effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["defer"]}})
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    # acme is exhausted, so routing lands on zeta's route, whose explicitly
    # written effort contradicts gem-high. The claim would fail too — the
    # pair refusal must come first.
    _fail_first_claim(runner, monkeypatch, name="zeta")

    result = _start(runner)

    assert result.get("error"), result
    assert "effort" in result["error"] and "gem-high" in result["error"], result
    assert not _calls(probes["zeta"])
    with runner.startup._lock():
        runs = (runner.startup._read().get("zeta") or {}).get("runs") or {}
    assert not runs, f"a refused start left a startup claim behind: {runs}"
