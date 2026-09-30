"""Black-box contract for `context/specs/m-routing-fixes.md` (RM-R1..R6a).

Read with the amendments: RM-R2a refines RM-R2, RM-R3a refines RM-R3, RM-R4b
replaces RM-R4, RM-R5a replaces RM-R5. There is no test for RM-R4 or RM-R5
proper; both are withdrawn.

Everything goes through the public surface: `Runner.start()` and
`Runner.consult()` over real fake-CLI subprocesses, with provider budget
readings injected either by replacing `budget.read_all` (the seam the H4/RT
suites use) or through a real provider budget script (for RM-R4b, whose whole
subject is how a script's reading is read). Provider names are invented.

Where the spec is silent the assumption is stated next to the test, and again
in the run report.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.paths import global_config_dir  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402

OLD_SESSION = "sess-acme-standing"
STANDING = "ag-5ta0d1"


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def _budgets(monkeypatch, **headroom):
    """`name=0.5` is a known reading; `name=None` is an unknown one."""
    readings = {name: (budget_mod.Budget(name, known=True, headroom=room)
                       if room is not None else budget_mod.Budget(name, known=False))
                for name, room in headroom.items()}
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: readings)


def _events(runner):
    path = runner.paths.events_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _values(entry) -> list:
    out = []
    for value in entry.values():
        out.extend(_values(value) if isinstance(value, dict) else [value])
    return out


def _start(runner, name="worker", **kwargs):
    """The result of `start`, or `{"raised": <exception>, "error": <text>}`."""
    async def go():
        try:
            result = await runner.start(name, "work", **kwargs)
        except Exception as exc:  # a refusal may be raised or returned
            return {"raised": exc, "error": str(exc)}
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _consult(runner, name="advisor", message="next question"):
    async def go():
        try:
            return await runner.consult(name, message, timeout=60)
        except Exception as exc:
            return {"raised": exc, "error": str(exc)}
    return asyncio.run(go())


def _project(tmp_path, monkeypatch, agent, providers, project=None):
    return h.make_runner(tmp_path / "project", monkeypatch,
                         agents={agent.name: agent}, providers=providers,
                         project=project or {})


def _fakes(tmp_path, *names, families=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers, probes = {}, {}
    for name in names:
        providers[name], probes[name] = _fake_cli(tmp_path, name)
        if families and name in families:
            providers[name]["family"] = families[name]
    return providers, probes


def _ran(probes, name):
    return bool(_calls(probes[name]))


# ---------------------------------------------------------------------------
# RM-R1: a resumed consult respects max_concurrent
# ---------------------------------------------------------------------------

class Standing:
    """An `advisor` conversation, idle after one turn on `acme`, in a tree
    that allows `cap` running agents and already has `running` of them."""

    def __init__(self, tmp_path, monkeypatch, *, cap, running, standing=True):
        self.providers, self.probes = _fakes(tmp_path, "acme")
        advisor = AgentSpec("advisor", "acme", "acme-large", conversational=True)
        worker = AgentSpec("worker", "acme", "acme-small")
        self.runner = h.make_runner(
            tmp_path / "proj", monkeypatch,
            agents={"advisor": advisor, "worker": worker},
            providers=self.providers,
            project={"limits": {"max_concurrent": cap}})
        if standing:
            worktree = self.runner.paths.worktree(STANDING)
            worktree.mkdir(parents=True)
            self.runner.tree.add(Node(
                id=STANDING, agent="advisor", provider="acme", model="acme-large",
                parent=None, depth=1, status="idle", session_id=OLD_SESSION,
                worktree=str(worktree), conversation=True, turns=1))
        self.fillers = []
        for i in range(running):
            node = Node(id=f"ag-f111e{i}", agent="worker", provider="acme",
                        model="acme-small", parent=None, depth=1, status="running")
            self.runner.tree.add(node)
            self.fillers.append(node.id)

    def free_one(self):
        self.runner.tree.set_status(self.fillers.pop(), "done", "finished")

    def standing_node(self):
        return self.runner.tree.get(STANDING)


def _refused(result):
    return bool(result.get("error")) and not result.get("reply")


def test_rm_r1_resumed_consult_is_refused_when_the_tree_is_full(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2)

    result = _consult(s.runner)

    assert _refused(result), f"a full tree admitted a resumed consult: {result}"
    assert not _calls(s.probes["acme"]), "the CLI ran although the turn was refused"


def test_rm_r1_refusal_has_the_start_refusal_text(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2)
    started = _start(s.runner, "worker")
    assert "max_concurrent=2" in started.get("error", ""), started  # the reference text

    result = _consult(s.runner)

    reference = started["error"].splitlines()[0]
    assert reference in result.get("error", ""), (
        f"expected start_agent's refusal text {reference!r} in {result}")


def test_rm_r1_refused_conversation_stays_idle_and_resumable(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2)
    before = s.standing_node()

    _consult(s.runner)
    _consult(s.runner)      # a repeat is refused the same way and changes nothing

    after = s.standing_node()
    assert after.status == "idle"
    assert (after.session_id, after.turns) == (before.session_id, before.turns)


def test_rm_r1_same_consult_succeeds_and_resumes_once_a_slot_frees(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2)
    assert _refused(_consult(s.runner))

    s.free_one()
    result = _consult(s.runner)

    assert not result.get("error"), f"the consult still failed with a free slot: {result}"
    calls = _calls(s.probes["acme"])
    assert len(calls) == 1, calls
    assert _flag(calls[0], "--resume") == OLD_SESSION
    assert result.get("agent_id") == STANDING, "a new conversation replaced the standing one"
    assert s.standing_node().turns == 2


def test_rm_r1_boundary_one_slot_left_admits_the_resume(tmp_path, monkeypatch):
    # The idle conversation itself holds no slot: cap-1 others running is room.
    s = Standing(tmp_path, monkeypatch, cap=2, running=1)

    result = _consult(s.runner)

    assert not result.get("error"), result
    assert _flag(_calls(s.probes["acme"])[0], "--resume") == OLD_SESSION


def test_rm_r1_boundary_a_cap_of_one_with_nothing_else_running_admits(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=1, running=0)

    result = _consult(s.runner)

    assert not result.get("error"), result
    assert len(_calls(s.probes["acme"])) == 1


def test_rm_r1_boundary_a_cap_of_one_that_is_taken_refuses(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=1, running=1)

    assert _refused(_consult(s.runner))
    assert not _calls(s.probes["acme"])


# ---------------------------------------------------------------------------
# RM-R2 / RM-R2a: the agent's own routes come before the project chain
# ---------------------------------------------------------------------------

def _routing_setup(tmp_path, monkeypatch, models, chain, names, *, families=None,
                   preferred="acme"):
    providers, probes = _fakes(tmp_path, *names, families=families)
    agent = AgentSpec.from_dict("worker", {"provider": preferred, "model": "m1",
                                           "models": models})
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": chain}})
    return runner, probes


def test_rm_r2_models_route_beats_a_chain_of_providers_the_agent_never_named(
        tmp_path, monkeypatch):
    # The verified-by case: models {zeta}, chain [x, y, defer], all usable.
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["xi", "yotta", "defer"],
        ["acme", "zeta", "xi", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, xi=1.0, yotta=1.0)

    result = _start(runner)

    assert result.get("provider") == "zeta", result
    assert _flag(_calls(probes["zeta"])[0], "--model") == "z1"
    assert not (_ran(probes, "xi") or _ran(probes, "yotta") or _ran(probes, "acme"))


def test_rm_r2_models_route_is_tried_even_when_the_chain_is_only_defer(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    assert result.get("provider") == "zeta", result
    assert not result.get("deferred")


def test_rm_r2_models_route_is_tried_when_the_project_has_no_chain_at_all(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, [], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    assert _start(runner).get("provider") == "zeta"


def test_rm_r2_models_routes_are_tried_in_the_order_written_not_chain_order(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["yotta", "zeta"],
        ["acme", "zeta", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, yotta=1.0)

    assert _start(runner).get("provider") == "zeta"


def test_rm_r2_a_later_models_route_is_used_when_the_first_has_no_room(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["defer"],
        ["acme", "zeta", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, yotta=1.0)

    result = _start(runner)

    assert result.get("provider") == "yotta", result
    assert _flag(_calls(probes["yotta"])[0], "--model") == "y1"


def test_rm_r2_everything_exhausted_still_defers(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0)

    result = _start(runner)

    assert result.get("deferred"), result
    assert not (_ran(probes, "acme") or _ran(probes, "zeta"))


def test_rm_r2_routing_message_names_the_provider_and_says_it_is_the_agents_own(
        tmp_path, monkeypatch):
    # Assumption: the wording is not fixed. The message must contain the
    # chosen provider's name and the word "models" (the agent's own list) or
    # "own".
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["xi", "defer"], ["acme", "zeta", "xi"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0, xi=1.0)

    result = _start(runner)

    text = (result.get("routing") or "").lower()
    assert result.get("provider") == "zeta", result
    assert "zeta" in text, result
    assert "models" in text or "own" in text, (
        f"the routing message does not say the route came from the agent's own list: {text!r}")
    routed = [e for e in _events(runner) if e.get("kind") == "routed"]
    assert routed and "zeta" in json.dumps(routed[-1]).lower()


# RM-R2a, Tier A: the preferred provider and its siblings are one pool, and
# it comes before the agent's models routes.
def test_rm_r2a_tier_a_sibling_comes_before_a_models_route(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"],
        ["acme", "acme2", "zeta"], families={"acme2": "acme"})
    runner.providers["acme"].family = "acme"
    _budgets(monkeypatch, acme=0.0, acme2=1.0, zeta=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme2", result
    assert _flag(_calls(probes["acme2"])[0], "--model") == "m1"


def test_rm_r2a_tier_a_preferred_still_wins_when_it_has_room(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.9, zeta=1.0)

    assert _start(runner).get("provider") == "acme"


# RM-R2a, Tier B: a listed route's family siblings join right after it.
def test_rm_r2a_tier_b_sibling_of_a_listed_route_comes_before_the_next_route(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["defer"],
        ["acme", "zeta", "zeta2", "yotta"], families={"zeta2": "zeta", "zeta": "zeta"})
    _budgets(monkeypatch, acme=0.0, zeta=0.0, zeta2=1.0, yotta=1.0)

    result = _start(runner)

    assert result.get("provider") == "zeta2", result
    assert _flag(_calls(probes["zeta2"])[0], "--model") == "z1"


def test_rm_r2a_a_cross_family_chain_entry_with_no_model_still_cannot_run(
        tmp_path, monkeypatch):
    # "Eligible" is the existing usable/_usable_spec rule: no model, no route.
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {}, ["zeta", "defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    assert result.get("deferred") or result.get("error"), result
    assert not (_ran(probes, "zeta") or _ran(probes, "acme"))


def test_rm_r2a_a_models_route_with_an_empty_model_is_not_a_route(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": ""}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    assert result.get("deferred") or result.get("error"), result
    assert not _ran(probes, "zeta")


# ---------------------------------------------------------------------------
# RM-R3 / RM-R3a: unknown headroom ranks below known-good, within a tier
# ---------------------------------------------------------------------------

def test_rm_r3a_tier_b_known_route_is_tried_before_an_unknown_one(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["defer"],
        ["acme", "zeta", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=None, yotta=0.5)

    assert _start(runner).get("provider") == "yotta"


def test_rm_r3a_tier_b_unknown_route_is_still_eligible_when_it_is_the_only_one(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=None)

    assert _start(runner).get("provider") == "zeta"


def test_rm_r3a_tier_b_unknown_route_is_used_when_the_known_ones_are_exhausted(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["defer"],
        ["acme", "zeta", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=0.0, yotta=None)

    assert _start(runner).get("provider") == "yotta"


def test_rm_r3a_tier_b_written_order_holds_between_two_known_routes(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1", "yotta": "y1"}, ["defer"],
        ["acme", "zeta", "yotta"])
    _budgets(monkeypatch, acme=0.0, zeta=0.3, yotta=0.9)

    assert _start(runner).get("provider") == "zeta"     # order, not headroom


def test_rm_r3a_tier_a_pool_prefers_a_known_reading_over_an_unknown_one(
        tmp_path, monkeypatch):
    # Assumption: with equal load the known reading wins inside the pool.
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {}, [], ["acme", "acme2"], families={"acme2": "acme"})
    runner.providers["acme"].family = "acme"
    _budgets(monkeypatch, acme=None, acme2=0.5)

    result = _start(runner)

    assert result.get("provider") == "acme2", result


def test_rm_r3a_tier_a_pool_unknown_is_still_eligible(tmp_path, monkeypatch):
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {}, [], ["acme", "acme2"], families={"acme2": "acme"})
    runner.providers["acme"].family = "acme"
    _budgets(monkeypatch, acme=None, acme2=0.0)

    assert _start(runner).get("provider") == "acme"


def test_rm_r3a_never_moves_a_candidate_across_tiers(tmp_path, monkeypatch):
    # Unknown first-tier candidate is chosen over a known second-tier one.
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=None, zeta=0.9)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    assert not _ran(probes, "zeta")


def test_rm_r3a_unknown_sibling_pool_does_not_leapfrog_a_known_tier_b_route(
        tmp_path, monkeypatch):
    # Both Tier A members exhausted: tier B's known route is used; an unknown
    # reading elsewhere in Tier A never matters because Tier A is exhausted.
    runner, probes = _routing_setup(
        tmp_path, monkeypatch, {"zeta": "z1"}, ["defer"],
        ["acme", "acme2", "zeta"], families={"acme2": "acme"})
    runner.providers["acme"].family = "acme"
    _budgets(monkeypatch, acme=0.0, acme2=0.0, zeta=0.9)

    assert _start(runner).get("provider") == "zeta"


# ---------------------------------------------------------------------------
# RM-R4b: a reading older than budget.max_reading_age_seconds routes as unknown
# ---------------------------------------------------------------------------

def _script(name, reading: dict):
    path = global_config_dir() / "providers" / f"{name}.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncase \"$1\" in budget) cat <<'EOF'\n"
                    f"{json.dumps(reading)}\nEOF\n;; *) exit 64;; esac\n")
    path.chmod(0o755)


def _iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


class Aged:
    """`acme` (preferred) reports through a real budget script; `zeta` is the
    agent's own fallback and the project chain, with no reading at all."""

    def __init__(self, tmp_path, monkeypatch, reading, *, max_age=None):
        budget_mod.invalidate_cache()
        self.providers, self.probes = _fakes(tmp_path, "acme", "zeta")
        _script("acme", reading)
        budget = {"fallback_chain": ["zeta"]}
        if max_age is not None:
            budget["max_reading_age_seconds"] = max_age
        agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                               "models": {"zeta": "z1"}})
        self.runner = _project(tmp_path, monkeypatch, agent, self.providers,
                               {"budget": budget})

    def provider(self):
        return _start(self.runner).get("provider")


FULL = {"known": True, "headroom": 0.0, "source": "script"}


def test_rm_r4b_codex_shaped_full_reading_13000s_old_routes_as_unknown(
        tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 13000})
    assert a.provider() == "acme"        # unknown is usable


def test_rm_r4b_the_same_reading_60s_old_is_unusable(tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 60})
    assert a.provider() == "zeta"


def test_rm_r4b_reading_with_no_age_information_is_unchanged(tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, FULL)
    assert a.provider() == "zeta"


def test_rm_r4b_default_bound_is_3600_boundaries(tmp_path, monkeypatch):
    # Assumption: "exceeds" is strict; one second either side of the bound.
    assert Aged(tmp_path / "a", monkeypatch,
                {**FULL, "stale_seconds": 3540}).provider() == "zeta"
    assert Aged(tmp_path / "b", monkeypatch,
                {**FULL, "stale_seconds": 3700}).provider() == "acme"


def test_rm_r4b_the_bound_is_configurable(tmp_path, monkeypatch):
    assert Aged(tmp_path / "a", monkeypatch, {**FULL, "stale_seconds": 99},
                max_age=100).provider() == "zeta"
    assert Aged(tmp_path / "b", monkeypatch, {**FULL, "stale_seconds": 101},
                max_age=100).provider() == "acme"


def test_rm_r4b_a_raised_bound_keeps_an_old_reading_authoritative(tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 13000}, max_age=20000)
    assert a.provider() == "zeta"


def test_rm_r4b_read_at_is_used_when_the_script_gives_no_stale_seconds(
        tmp_path, monkeypatch):
    old = Aged(tmp_path / "a", monkeypatch, {**FULL, "read_at": time.time() - 13000})
    assert old.provider() == "acme"
    fresh = Aged(tmp_path / "b", monkeypatch, {**FULL, "read_at": time.time() - 10})
    assert fresh.provider() == "zeta"


def test_rm_r4b_stale_seconds_wins_over_read_at(tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch,
             {**FULL, "stale_seconds": 60, "read_at": time.time() - 13000})
    assert a.provider() == "zeta"


def test_rm_r4b_a_partly_stale_reading_still_usable_when_known_below_full(
        tmp_path, monkeypatch):
    # Demotion never makes a routable reading worse: a stale 50% is usable.
    a = Aged(tmp_path, monkeypatch,
             {"known": True, "headroom": 0.5, "stale_seconds": 13000})
    assert a.provider() == "acme"


def test_rm_r4b_a_stale_reading_ranks_as_unknown_inside_its_tier(tmp_path, monkeypatch):
    # Tier B: zeta's full reading is stale (unknown); yotta is known at 50%.
    budget_mod.invalidate_cache()
    providers, probes = _fakes(tmp_path, "acme", "zeta", "yotta")
    _script("zeta", {**FULL, "stale_seconds": 13000})
    _script("yotta", {"known": True, "headroom": 0.5})
    _script("acme", FULL)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "models": {"zeta": "z1", "yotta": "y1"}})
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["defer"]}})

    assert _start(runner).get("provider") == "yotta"


def test_rm_r4b_the_cached_reading_ages_across_cache_hits(tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 3590})
    assert a.provider() == "zeta"           # 3590 s old: still authoritative

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 30)   # inside the cache TTL

    assert a.provider() == "acme", "the cached reading did not grow older with the cache"


def test_rm_r4b_the_raw_reading_stays_available_for_display(tmp_path, monkeypatch):
    # Assumption: display goes through budget_status; the spec does not name
    # the "marked stale" field, so only the raw values and the age are asserted.
    from multiagents import server
    from test_codex_engine_models_budget import NAME, Project, _executable

    p = Project(tmp_path, monkeypatch, {NAME: {"bin": NAME, "spawn": {"args": ["x"]}}})
    _executable(p.scripts / f"{NAME}.sh",
                "#!/bin/sh\ncase \"$1\" in budget) echo '{\"known\": true, \"headroom\": 0.0, "
                "\"stale_seconds\": 13000}' ;; *) exit 64;; esac\n")
    h.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    budget_mod.invalidate_cache()
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    if isinstance(status, str):
        status = json.loads(status)
    entry = (status.get("providers") or {}).get(NAME) or {}
    assert entry.get("headroom") == 0.0, entry
    assert entry.get("stale_seconds") == 13000, entry


def test_rm_r4b_an_expired_window_is_still_cleared_per_window(tmp_path, monkeypatch):
    # Existing QF-R1 rule, kept: the only window reset 10 minutes ago.
    a = Aged(tmp_path, monkeypatch,
             {"known": True, "headroom": 0.0, "resets_at": _iso(-600),
              "windows": {"weekly": {"percent": 100, "resets_at": _iso(-600)}}})
    assert a.provider() == "acme"


def test_rm_r4b_an_expired_window_does_not_hide_another_that_is_still_full(
        tmp_path, monkeypatch):
    # Why RM-R4(a) was withdrawn: demoting the whole reading would bypass the
    # weekly window that is still full. Fresh age (60 s) so only per-window
    # clearing applies.
    reading = {"known": True, "headroom": 0.0, "stale_seconds": 60,
               "windows": {"five_hour": {"percent": 100, "resets_at": _iso(-600)},
                           "weekly": {"percent": 100, "resets_at": _iso(3 * 86400)}}}
    a = Aged(tmp_path, monkeypatch, reading)
    assert a.provider() == "zeta"


def test_rm_r4b_expired_window_inside_the_margin_is_not_yet_cleared(tmp_path, monkeypatch):
    # The 120 s margin of the existing rule is unchanged.
    a = Aged(tmp_path, monkeypatch,
             {"known": True, "headroom": 0.0, "resets_at": _iso(-30),
              "windows": {"weekly": {"percent": 100, "resets_at": _iso(-30)}}})
    assert a.provider() == "zeta"


# ---------------------------------------------------------------------------
# RM-R5a: effort_suffixes
# ---------------------------------------------------------------------------

SUFFIXES = {"-low": "low", "-medium": "medium", "-high": "high"}


def _effort_setup(tmp_path, monkeypatch, *, model, effort, route=None, suffixes=SUFFIXES,
                  conversational=False, route_provider_suffixes=True):
    """`acme` is exhausted, so the agent runs on `zeta`, whose provider
    declares `effort_suffixes` (unless `suffixes` is None). `route` is the
    agent's `models.zeta` entry."""
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    if suffixes is not None:
        providers["zeta"]["effort_suffixes"] = dict(suffixes)
    data = {"provider": "acme", "model": "m1", "models": {"zeta": route or model},
            "conversational": conversational}
    if effort is not None:
        data["effort"] = effort
    agent = AgentSpec.from_dict("worker", data)
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["zeta"]}})
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    return runner, probes


def _effort_argv(probes):
    calls = _calls(probes["zeta"])
    assert len(calls) == 1, calls
    argv = calls[0]
    return argv, (argv[argv.index("--effort") + 1] if "--effort" in argv else None)


def _tree_snapshot(runner):
    branches = subprocess.run(["git", "-C", str(runner.paths.root), "branch", "--list"],
                              capture_output=True, text=True).stdout
    return sorted(runner.tree.read()["nodes"]), branches


def test_rm_r5a_inherited_effort_is_normalised_to_the_models_implied_effort(
        tmp_path, monkeypatch):
    # The agy incident: model ...-high, inherited effort low.
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-high", effort="low")

    result = _start(runner)

    assert result.get("provider") == "zeta", result
    argv, effort = _effort_argv(probes)
    assert _flag(argv, "--model") == "gem-high"
    assert effort == "high", argv


def test_rm_r5a_the_normalisation_is_recorded_in_an_event(tmp_path, monkeypatch):
    # Assumption: the event's kind and field names are not fixed. Some event
    # must carry the old effort, the effective model and the new effort as
    # values, and a reason.
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-high", effort="low")

    _start(runner)

    hits = [e for e in _events(runner)
            if {"low", "gem-high", "high"} <= set(_values(e))]
    assert hits, f"no event records old effort, model and new effort: {_events(runner)}"
    assert any(isinstance(v, str) and len(v) > 12 and v not in ("gem-high",)
               for v in _values(hits[0])), f"the event gives no reason: {hits[0]}"


def test_rm_r5a_inherited_effort_via_a_bare_string_route_is_normalised(
        tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-medium",
                                   effort="high", route="gem-medium")
    _start(runner)
    assert _effort_argv(probes)[1] == "medium"


def test_rm_r5a_inherited_effort_via_a_route_mapping_without_effort_is_normalised(
        tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-medium",
                                   effort="high", route={"model": "gem-medium"})
    _start(runner)
    assert _effort_argv(probes)[1] == "medium"


def test_rm_r5a_inherited_effort_that_agrees_is_left_alone(tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-high", effort="high")
    _start(runner)
    assert _effort_argv(probes)[1] == "high"


def test_rm_r5a_a_model_with_no_matching_suffix_has_no_implied_effort(
        tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-flash", effort="low")
    _start(runner)
    assert _effort_argv(probes)[1] == "low"


def test_rm_r5a_the_suffix_is_anchored_at_the_end_of_the_model_id(tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-low-preview",
                                   effort="high")
    _start(runner)
    assert _effort_argv(probes)[1] == "high"


def test_rm_r5a_no_effort_at_all_adds_none(tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-high", effort=None)
    _start(runner)
    assert _effort_argv(probes)[1] is None


def test_rm_r5a_provider_without_effort_suffixes_is_unchanged(tmp_path, monkeypatch):
    runner, probes = _effort_setup(tmp_path, monkeypatch, model="gem-medium",
                                   effort="low", suffixes=None)
    _start(runner)
    assert _effort_argv(probes)[1] == "low"


def test_rm_r5a_an_explicit_route_effort_that_conflicts_is_refused(tmp_path, monkeypatch):
    runner, probes = _effort_setup(
        tmp_path, monkeypatch, model="gem-high", effort="medium",
        route={"model": "gem-high", "effort": "low"})
    before = _tree_snapshot(runner)

    result = _start(runner)

    error = result.get("error", "")
    assert error, f"an explicit conflicting effort was launched: {result}"
    assert "gem-high" in error and "low" in error and "zeta" in error, error
    assert not _calls(probes["zeta"]) and not _calls(probes["acme"])
    assert _tree_snapshot(runner) == before, "the refusal left a node or a branch behind"


def test_rm_r5a_an_explicit_route_effort_that_agrees_is_launched(tmp_path, monkeypatch):
    runner, probes = _effort_setup(
        tmp_path, monkeypatch, model="gem-high", effort="low",
        route={"model": "gem-high", "effort": "high"})
    result = _start(runner)
    assert not result.get("error"), result
    assert _effort_argv(probes)[1] == "high"


def test_rm_r5a_an_explicit_effort_on_a_model_with_no_suffix_is_launched(
        tmp_path, monkeypatch):
    runner, probes = _effort_setup(
        tmp_path, monkeypatch, model="gem-flash", effort=None,
        route={"model": "gem-flash", "effort": "low"})
    result = _start(runner)
    assert not result.get("error"), result
    assert _effort_argv(probes)[1] == "low"


def test_rm_r5a_an_empty_route_effort_still_means_drop_the_option(tmp_path, monkeypatch):
    # Guard: documented in AgentSpec.fallback_for; not an explicit conflict.
    runner, probes = _effort_setup(
        tmp_path, monkeypatch, model="gem-high", effort="low",
        route={"model": "gem-high", "effort": ""})
    result = _start(runner)
    assert not result.get("error"), result
    assert _effort_argv(probes)[1] is None


def test_rm_r5a_the_normalised_effort_is_reused_on_the_next_consult(tmp_path, monkeypatch):
    # The agy shape: the preferred provider itself declares the suffixes and
    # the agent's top-level effort (inherited) contradicts its model.
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("advisor", {
        "provider": "acme", "model": "gem-high", "effort": "low", "conversational": True})
    runner = _project(tmp_path, monkeypatch, agent, providers)

    first = _consult(runner)
    second = _consult(runner)

    assert not first.get("error") and not second.get("error"), (first, second)
    calls = _calls(probes["acme"])
    assert len(calls) == 2, calls
    assert "--resume" not in calls[0] and "--resume" in calls[1]
    for argv in calls:
        assert _flag(argv, "--model") == "gem-high"
        assert _flag(argv, "--effort") == "high", argv


def test_rm_r5a_the_incident_shape_on_the_preferred_provider_launches_normalised(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "gem-medium",
                                           "effort": "low"})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, acme=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme", result
    calls = _calls(probes["acme"])
    assert len(calls) == 1 and _flag(calls[0], "--effort") == "medium", calls
    assert [e for e in _events(runner) if {"low", "medium"} <= set(_values(e))], _events(runner)


# ---------------------------------------------------------------------------
# RM-R6a: nothing else changes (guards; green before and after)
# ---------------------------------------------------------------------------

def test_rm_r6a_agent_without_models_stays_on_its_healthy_preferred_provider(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(tmp_path, monkeypatch, {}, ["zeta"], ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.9, zeta=1.0)
    assert _start(runner).get("provider") == "acme"


def test_rm_r6a_agent_without_models_defers_rather_than_using_an_unmodelled_chain_entry(
        tmp_path, monkeypatch):
    runner, probes = _routing_setup(tmp_path, monkeypatch, {}, ["zeta", "defer"],
                                    ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    assert result.get("deferred") or result.get("error"), result
    assert not (_ran(probes, "acme") or _ran(probes, "zeta"))
    skipped = [e for e in _events(runner) if e.get("kind") == "route_skipped"]
    assert [e.get("provider") for e in skipped] == ["zeta"]


def test_rm_r6a_agent_without_models_still_uses_a_same_family_sibling(tmp_path, monkeypatch):
    runner, probes = _routing_setup(tmp_path, monkeypatch, {}, [], ["acme", "acme2"],
                                    families={"acme2": "acme"})
    runner.providers["acme"].family = "acme"
    _budgets(monkeypatch, acme=0.0, acme2=1.0)

    result = _start(runner)

    assert result.get("provider") == "acme2", result
    assert _flag(_calls(probes["acme2"])[0], "--model") == "m1"


def test_rm_r6a_a_chain_entry_the_agent_has_a_model_for_still_routes(tmp_path, monkeypatch):
    runner, probes = _routing_setup(tmp_path, monkeypatch, {"zeta": "z1"}, ["zeta"],
                                    ["acme", "zeta"])
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    assert _start(runner).get("provider") == "zeta"


def test_rm_r6a_a_consult_that_creates_a_conversation_is_still_admission_checked(
        tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2, standing=False)

    result = _consult(s.runner)

    assert _refused(result), result
    assert "max_concurrent=2" in result.get("error", "")
    assert not _calls(s.probes["acme"])


def test_rm_r6a_a_fresh_full_reading_without_age_is_still_unusable_for_routing(
        tmp_path, monkeypatch):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 0})
    assert a.provider() == "zeta"


def test_rm_r6a_start_is_still_refused_at_the_cap(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=2, running=2, standing=False)

    result = _start(s.runner, "worker")

    assert "max_concurrent=2" in result.get("error", ""), result
