"""Black-box contract for `context/specs/fallback-scope.md` (FS-R1..R6, as
revised after the advisor's check), ticket bug-ac396a.

An agent falls back only where it was told it may: its own `provider` plus the
keys of its `models:` map. Neither the project `fallback_chain` nor a shared
provider family adds a candidate.

Everything goes through `Runner.start()` and `Runner.steer()` over real
fake-CLI subprocesses, with budget readings injected by replacing
`budget.read_all` (the seam the RM/H4 suites use). Provider names are invented.

The family config. "bravo" is a second account on the same CLI as "acme": its
`family` is "acme". The family name differs from the sibling's name on purpose,
so a message that names "acme" cannot be mistaken for one naming "bravo". The
same goes for "zeta"/"zeta-two" (family "zeta"). Chain-only providers are
"xenon" and "yotta".

Which tests are green on main (hardening already true today) and which are red
(family widening still present) is listed in the run report; every red one is
named `..._family_...` or `..._sibling_...`.

Assumptions where the spec is silent:
- Wording of `routing` / the deferral reason is not fixed; only which provider
  names occur in it.
- A legacy session steered on its recorded sibling runs the model recorded on
  the node.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402

ALL = ("acme", "bravo", "zeta", "zeta-two", "xenon", "yotta")
FAMILIES = {"bravo": "acme", "acme": "acme", "zeta": "zeta", "zeta-two": "zeta"}
SESSION = "sess-legacy-1"
LEGACY = "ag-1e9ac1"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def _budgets(monkeypatch, **headroom):
    """`name=0.5` is a known reading; `name=0.0` is exhausted."""
    readings = {name: budget_mod.Budget(name, known=True, headroom=room)
                for name, room in headroom.items()}
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: readings)


def _setup(tmp_path, monkeypatch, models, chain, *, names=ALL, preferred="acme",
           model="m1", families=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers, probes = {}, {}
    for name in names:
        providers[name], probes[name] = _fake_cli(tmp_path, name)
        if families and name in families:
            providers[name]["family"] = families[name]
    agent = AgentSpec.from_dict("worker", {"provider": preferred, "model": model,
                                           "models": models})
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"worker": agent}, providers=providers,
                           project={"budget": {"fallback_chain": chain}})
    return runner, probes


def _start(runner, **kwargs):
    async def go():
        try:
            result = await runner.start("worker", "work", **kwargs)
        except Exception as exc:  # a refusal may be raised or returned
            return {"raised": exc, "error": str(exc)}
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _ran(probes, *names):
    return [name for name in names if _calls(probes[name])]


def _text(result) -> str:
    """Everything a caller is told about why routing went the way it did."""
    return " ".join(str(result.get(key) or "")
                    for key in ("routing", "reason", "error", "note")).lower()


def _deferred(result) -> bool:
    return bool(result.get("deferred")) and not result.get("agent_id")


def _events(runner):
    path = runner.paths.events_file
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip().startswith("{")] if path.exists() else []


# ---------------------------------------------------------------------------
# FS-R1: candidates are the agent's provider plus its `models:` keys
# ---------------------------------------------------------------------------

def test_fs_r1_models_null_with_exhausted_provider_is_deferred_never_routed(
        tmp_path, monkeypatch):
    # The verified-by case: provider A, models null, chain lists B and C.
    runner, probes = _setup(tmp_path, monkeypatch, None, ["xenon", "yotta", "defer"])
    _budgets(monkeypatch, acme=0.0, xenon=1.0, yotta=1.0, zeta=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL), "an agent that named no fallback ran somewhere"


def test_fs_r1_models_null_defers_even_when_the_chain_omits_defer(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["xenon", "yotta"])
    _budgets(monkeypatch, acme=0.0, xenon=1.0, yotta=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL)


def test_fs_r1_models_null_defers_with_no_chain_at_all(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, [])
    _budgets(monkeypatch, acme=0.0, xenon=1.0)

    assert _deferred(_start(runner))
    assert not _ran(probes, *ALL)


def test_fs_r1_chain_adds_no_provider_to_an_agent_with_models(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"},
                            ["xenon", "yotta", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, xenon=1.0, yotta=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL)


def test_fs_r1_all_candidates_exhausted_defers_when_chain_omits_defer(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["xenon"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, xenon=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL)


def test_fs_r1_a_listed_provider_is_still_a_candidate_whatever_the_chain_says(
        tmp_path, monkeypatch):
    # The chain only supplies `defer` here; the agent's own route must speak.
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, xenon=1.0)

    result = _start(runner)

    assert result.get("provider") == "zeta", result
    assert _flag(_calls(probes["zeta"])[0], "--model") == "z1"
    assert _ran(probes, *ALL) == ["zeta"]


def test_fs_r1_a_listed_provider_routes_with_no_chain_and_with_a_chain_not_naming_it(
        tmp_path, monkeypatch):
    for chain in ([], ["xenon", "yotta"]):
        sub = tmp_path / f"chain{len(chain)}"
        runner, probes = _setup(sub, monkeypatch, {"zeta": "z1"}, chain)
        _budgets(monkeypatch, acme=0.0, zeta=1.0, xenon=1.0, yotta=1.0)

        result = _start(runner)

        assert result.get("provider") == "zeta", (chain, result)
        assert _ran(probes, *ALL) == ["zeta"]


def test_fs_r1_order_is_preferred_then_models_order_not_chain_order(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"},
                            ["yotta", "zeta", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, yotta=1.0)

    assert _start(runner).get("provider") == "zeta"


def test_fs_r1_preferred_provider_wins_while_it_has_room(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["xenon", "defer"])
    _budgets(monkeypatch, acme=0.9, zeta=1.0, xenon=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    assert _ran(probes, *ALL) == ["acme"]


def test_fs_r1_a_later_models_route_is_used_when_the_first_has_no_room(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"},
                            ["xenon", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, yotta=1.0, xenon=1.0)

    result = _start(runner)

    assert result.get("provider") == "yotta", result
    assert _flag(_calls(probes["yotta"])[0], "--model") == "y1"


def test_fs_r1_a_models_key_with_an_empty_model_is_not_a_candidate(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": ""}, ["zeta", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    assert _deferred(result) or result.get("error"), result
    assert not _ran(probes, *ALL)


def test_fs_r1_a_deferred_start_runs_nothing_and_creates_exactly_one_entry(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["xenon", "defer"])
    _budgets(monkeypatch, acme=0.0, xenon=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert len(runner.tree.read()["deferred"]) == 1
    assert not _ran(probes, *ALL)


# ---------------------------------------------------------------------------
# FS-R2: a provider family widens nothing
# ---------------------------------------------------------------------------

def test_fs_r2_family_sibling_of_the_provider_is_not_a_fallback(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0)

    result = _start(runner)

    assert _deferred(result), f"routed to an unlisted family sibling: {result}"
    assert not _ran(probes, *ALL)


def test_fs_r2_family_sibling_is_not_a_fallback_with_a_chain_that_lists_it(
        tmp_path, monkeypatch):
    # Listing it in the project chain is not listing it in `models:`.
    runner, probes = _setup(tmp_path, monkeypatch, None, ["bravo", "defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL)


def test_fs_r2_sibling_of_a_listed_models_route_is_not_a_fallback(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, zeta=0.0, **{"zeta-two": 1.0}, bravo=1.0)

    result = _start(runner)

    assert _deferred(result), f"routed to an unlisted sibling of a listed route: {result}"
    assert not _ran(probes, *ALL)


def test_fs_r2_a_healthy_unlisted_sibling_does_not_take_work_from_a_roomy_preferred(
        tmp_path, monkeypatch):
    # No load-balancing across accounts the agent never named.
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.4, bravo=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    assert _ran(probes, *ALL) == ["acme"]


def test_fs_r2_a_sibling_that_is_listed_in_models_is_a_candidate(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"bravo": "b1"}, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0)

    result = _start(runner)

    assert result.get("provider") == "bravo", result
    assert _flag(_calls(probes["bravo"])[0], "--model") == "b1"
    assert _ran(probes, *ALL) == ["bravo"]


def test_fs_r2_a_listed_sibling_runs_its_own_listed_model_not_the_preferreds(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"bravo": "b1"}, [], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0)

    _start(runner)

    assert _flag(_calls(probes["bravo"])[0], "--model") == "b1"


def test_fs_r2_listing_one_sibling_does_not_admit_another_of_the_family(
        tmp_path, monkeypatch):
    names = ("acme", "bravo", "charlie")
    runner, probes = _setup(tmp_path, monkeypatch, {"bravo": "b1"}, ["defer"], names=names, families=FAMILIES)
    runner.providers["charlie"].family = "acme"
    _budgets(monkeypatch, acme=0.0, bravo=0.0, charlie=1.0)

    result = _start(runner)

    assert _deferred(result), f"routed to unlisted family member charlie: {result}"
    assert not _ran(probes, *names)


def test_fs_r2_an_agent_on_a_sibling_does_not_reach_the_rest_of_the_family(
        tmp_path, monkeypatch):
    # The agent's own provider is "bravo"; "acme" is a sibling it never named.
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], preferred="bravo", families=FAMILIES)
    _budgets(monkeypatch, acme=1.0, bravo=0.0)

    result = _start(runner)

    assert _deferred(result), result
    assert not _ran(probes, *ALL)


def test_fs_r2_family_does_not_widen_a_pinned_start(tmp_path, monkeypatch):
    # FS-R3: a pin keeps today's semantics, no fallback.
    runner, probes = _setup(tmp_path, monkeypatch, {"bravo": "b1"}, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0, zeta=1.0)

    result = _start(runner, model="pinned-model")

    assert result.get("provider") not in ("bravo", "zeta"), result
    assert not _ran(probes, "bravo", "zeta", "xenon", "yotta", "zeta-two")


# ---------------------------------------------------------------------------
# FS-R2, legacy sessions: a session recorded on an unlisted sibling resumes
# there when steered, and is bound there.
# ---------------------------------------------------------------------------

def _legacy(runner, provider="bravo", model="m1"):
    worktree = runner.paths.worktree(LEGACY)
    worktree.mkdir(parents=True)
    runner.tree.add(Node(
        id=LEGACY, agent="worker", provider=provider, model=model, parent=None,
        depth=1, status="done", session_id=SESSION, worktree=str(worktree)))


def _steer(runner):
    async def go():
        try:
            result = await runner.steer(LEGACY, "continue")
        except Exception as exc:
            return {"raised": exc, "error": str(exc)}
        run = runner.runs.get(LEGACY)
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def test_fs_r2_legacy_session_on_an_unlisted_sibling_resumes_there_when_steered(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=1.0, bravo=1.0)
    _legacy(runner)

    result = _steer(runner)

    assert not result.get("error"), result
    calls = _calls(probes["bravo"])
    assert len(calls) == 1, f"the session was not resumed on its recorded provider: {result}"
    assert _flag(calls[0], "--resume") == SESSION
    assert _flag(calls[0], "--model") == "m1"
    assert not _ran(probes, "acme", "zeta", "xenon", "yotta", "zeta-two")


def test_fs_r2_legacy_session_stays_bound_even_when_its_provider_is_exhausted_and_the_agents_own_is_not(
        tmp_path, monkeypatch):
    # We never move a bound session: no steer lands on the agent's own provider.
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=1.0, bravo=0.0)
    _legacy(runner)

    _steer(runner)

    assert not _calls(probes["acme"]), "a bound session was moved to another provider"


def test_fs_r2_a_legacy_session_is_never_chosen_for_new_work(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=1.0)
    _legacy(runner)

    result = _start(runner)

    assert _deferred(result), result
    assert not _calls(probes["bravo"])


def test_fs_r2_steering_a_session_on_a_provider_the_agent_never_named_is_refused(
        tmp_path, monkeypatch):
    # Not a family sibling, not listed: the session cannot be resumed (RT-R2).
    runner, probes = _setup(tmp_path, monkeypatch, None, ["defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=1.0, yotta=1.0)
    _legacy(runner, provider="yotta")

    result = _steer(runner)

    assert result.get("error"), result
    assert not _ran(probes, *ALL)


# ---------------------------------------------------------------------------
# FS-R3: a pin is unchanged
# ---------------------------------------------------------------------------

def test_fs_r3_a_pinned_model_on_an_exhausted_provider_is_not_moved(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["xenon", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, xenon=1.0)

    result = _start(runner, model="pinned-model")

    assert not _ran(probes, *ALL), result
    assert result.get("error") or result.get("deferred") or result.get("reason"), result


def test_fs_r3_a_pinned_model_on_a_roomy_provider_still_runs_there(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"])
    _budgets(monkeypatch, acme=1.0, zeta=1.0)

    result = _start(runner, model="pinned-model")

    assert result.get("provider") == "acme", result
    assert _flag(_calls(probes["acme"])[0], "--model") == "pinned-model"


# ---------------------------------------------------------------------------
# FS-R4: messages name only real candidates
# ---------------------------------------------------------------------------

def test_fs_r4_routing_names_the_fallback_it_used_and_no_provider_outside_the_agent(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"},
                            ["xenon", "yotta", "defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, zeta=1.0, xenon=1.0, yotta=1.0, bravo=1.0,
             **{"zeta-two": 1.0})

    result = _start(runner)

    text = _text(result)
    assert result.get("provider") == "zeta", result
    assert "zeta" in text, result
    for outsider in ("xenon", "yotta", "bravo", "zeta-two"):
        assert outsider not in text, f"routing names {outsider}: {result}"


def test_fs_r4_routing_of_a_start_on_the_preferred_provider_names_no_outsider(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["xenon", "yotta", "defer"])
    _budgets(monkeypatch, acme=1.0, xenon=1.0, yotta=1.0, bravo=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    text = _text(result)
    for outsider in ("xenon", "yotta", "bravo"):
        assert outsider not in text, f"routing names {outsider}: {result}"


def test_fs_r4_deferral_reason_lists_the_candidates_that_were_tried(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"},
                            ["xenon", "defer"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, yotta=0.0, xenon=1.0, bravo=1.0)

    result = _start(runner)

    assert _deferred(result), result
    reason = _text(result)
    for tried in ("acme", "zeta", "yotta"):
        assert tried in reason, f"deferral reason omits tried candidate {tried}: {result}"


def test_fs_r4_deferral_reason_names_no_provider_the_agent_never_had(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"},
                            ["xenon", "yotta", "defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, zeta=0.0, xenon=0.0, yotta=0.0, bravo=0.0,
             **{"zeta-two": 0.0})

    result = _start(runner)

    assert _deferred(result), result
    reason = _text(result)
    for outsider in ("xenon", "yotta", "bravo", "zeta-two"):
        assert outsider not in reason, f"deferral names {outsider}: {result}"


def test_fs_r4_deferral_reason_for_models_null_names_only_its_provider(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, None, ["xenon", "yotta", "defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, xenon=0.0, yotta=0.0, bravo=0.0)

    result = _start(runner)

    assert _deferred(result), result
    reason = _text(result)
    assert "acme" in reason, result
    for outsider in ("xenon", "yotta", "bravo", "zeta"):
        assert outsider not in reason, f"deferral names {outsider}: {result}"


def test_fs_r4_a_listed_sibling_may_be_named_because_it_is_a_candidate(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"bravo": "b1"}, ["xenon", "defer"], families=FAMILIES)
    _budgets(monkeypatch, acme=0.0, bravo=0.0, xenon=1.0)

    result = _start(runner)

    assert _deferred(result), result
    reason = _text(result)
    assert "acme" in reason and "bravo" in reason, result
    assert "xenon" not in reason, result


# ---------------------------------------------------------------------------
# FS-R6: no regression for agents that list their fallbacks
# ---------------------------------------------------------------------------

def test_fs_r6_models_order_is_followed_across_three_routes(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch,
                            {"yotta": "y1", "zeta": "z1", "xenon": "x1"}, ["defer"])
    _budgets(monkeypatch, acme=0.0, yotta=0.0, zeta=1.0, xenon=1.0)

    result = _start(runner)

    assert result.get("provider") == "zeta", result
    assert _flag(_calls(probes["zeta"])[0], "--model") == "z1"


def test_fs_r6_a_start_with_headroom_everywhere_runs_on_the_preferred_provider_with_its_model(
        tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"])
    _budgets(monkeypatch, acme=1.0, zeta=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    assert _flag(_calls(probes["acme"])[0], "--model") == "m1"


def test_fs_r6_a_listed_route_that_is_disabled_is_still_skipped(tmp_path, monkeypatch):
    runner, probes = _setup(tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"},
                            ["defer"])
    runner.providers["zeta"].enabled = False
    _budgets(monkeypatch, acme=0.0, zeta=1.0, yotta=1.0)

    result = _start(runner)

    assert result.get("provider") == "yotta", result
