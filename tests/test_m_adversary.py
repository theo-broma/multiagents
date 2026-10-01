"""Adversary tests for batch M (context/specs/m-routing-fixes.md, RM-R1..R6a).

Each test states the contract clause it holds the code to. Tests marked
"guard" pass today and pin a fail-safe behaviour the review probed; the rest
fail on 28eb8a6 and demonstrate a defect.

Reuses the black-box seams of tests/test_m_routing_fixes.py: real fake-CLI
subprocesses, budget readings injected via `budget.read_all` or a real
provider budget script. Nothing here is randomised, so there is no seed.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls  # noqa: E402
from test_m_routing_fixes import (  # noqa: E402
    FULL, SUFFIXES, Aged, _budgets, _effort_argv, _events, _fakes, _project,
    _start, _tree_snapshot, _values,
)


# ---------------------------------------------------------------------------
# RM-R5a: a refusal must happen "before any side effect"
# ---------------------------------------------------------------------------

def _half_open(runner, name):
    """`name` tripped startup_down earlier; its cooldown has lapsed, so exactly
    one probe start may claim it (PS-R5)."""
    startup = runner.startup
    with startup._lock():
        records = startup._read()
        records[name] = {"generation": "g0", "count": 3, "down": True,
                         "until": time.time() - 5, "probe": None, "runs": {}}
        startup._write(records)
    assert startup.availability(name) is None     # claimable as the one probe


def test_explicit_effort_refusal_does_not_hold_the_half_open_probe(tmp_path, monkeypatch):
    # start() claims the provider's startup slot (the ONLY probe of a
    # half-open provider) and only then runs `_settle_effort`, whose refusal
    # raises outside the try that would release it. The server process that
    # owns the claim stays alive, so `_reconcile` never reaps it: the
    # provider stays startup_down for every agent until the server restarts.
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    providers["zeta"]["effort_suffixes"] = dict(SUFFIXES)
    bad = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1",
        "models": {"zeta": {"model": "gem-high", "effort": "low"}}})
    good = AgentSpec.from_dict("helper", {"provider": "zeta", "model": "gem-high"})
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"worker": bad, "helper": good},
                           providers=providers,
                           project={"budget": {"fallback_chain": ["zeta"]}})
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    _half_open(runner, "zeta")

    refused = _start(runner, "worker")
    assert refused.get("error"), refused          # the refusal itself is right

    assert runner.startup.availability("zeta") is None, (
        "the refused start left zeta's only half-open probe claimed: "
        f"{runner.startup.availability('zeta')}")
    after = _start(runner, "helper")
    assert after.get("provider") == "zeta" and not after.get("error"), after


def test_explicit_effort_refusal_leaves_no_startup_run_record(tmp_path, monkeypatch):
    # The same leak on a healthy provider: a run record naming a node that
    # never existed stays in startup.json, owned by the living server.
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    providers["zeta"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1",
        "models": {"zeta": {"model": "gem-high", "effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["zeta"]}})
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    before = _tree_snapshot(runner)

    assert _start(runner).get("error")

    assert _tree_snapshot(runner) == before
    with runner.startup._lock():
        runs = (runner.startup._read().get("zeta") or {}).get("runs") or {}
    assert not runs, f"a refused start left a startup claim behind: {runs}"


# ---------------------------------------------------------------------------
# RM-R5a: explicit route effort on a family sibling of the route
# ---------------------------------------------------------------------------

def test_explicit_route_effort_is_refused_on_the_routes_sibling_too(tmp_path, monkeypatch):
    # `models: {zeta: {model: gem-high, effort: low}}`. zeta2 is zeta's
    # family sibling, so it runs zeta's route — model AND explicit effort
    # (`_usable_spec`). On zeta the pair is refused; on zeta2 the identical,
    # explicitly written effort is silently rewritten to `high`, because
    # `_settle_effort` looks the route up under the sibling's own name and
    # finds none, so it calls the effort "inherited".
    providers, probes = _fakes(tmp_path, "acme", "zeta", "zeta2",
                               families={"zeta": "zeta", "zeta2": "zeta"})
    for name in ("zeta", "zeta2"):
        providers[name]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1",
        "models": {"zeta": {"model": "gem-high", "effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers,
                      {"budget": {"fallback_chain": ["defer"]}})
    _budgets(monkeypatch, acme=0.0, zeta=0.0, zeta2=1.0)

    result = _start(runner)

    assert result.get("error"), (
        f"an explicitly configured conflicting route effort launched on the "
        f"route's sibling instead of being refused: {result}; "
        f"argv={_calls(probes['zeta2'])}")
    assert not _calls(probes["zeta2"])


# ---------------------------------------------------------------------------
# Malformed `effort_suffixes`
# ---------------------------------------------------------------------------

def test_null_effort_in_effort_suffixes_never_reaches_the_cli(tmp_path, monkeypatch):
    # providers.yaml `effort_suffixes: {"-low": }` (a YAML null) is kept as
    # the effort string "None"; an inherited effort is then "normalised" to
    # it and the CLI is launched with `--effort None` — the launch-time
    # rejection RM-R5a exists to prevent, now manufactured by the config.
    providers, probes = _fakes(tmp_path, "acme", "zeta")
    providers["zeta"]["effort_suffixes"] = {"-low": None, "-high": "high"}
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "effort": "high",
                                           "models": {"zeta": "gem-low"}})
    try:
        runner = _project(tmp_path, monkeypatch, agent, providers,
                          {"budget": {"fallback_chain": ["zeta"]}})
    except Exception:
        return                                   # failing at config load is fine
    _budgets(monkeypatch, acme=0.0, zeta=1.0)

    result = _start(runner)

    if _calls(probes["zeta"]):
        argv, effort = _effort_argv(probes)
        assert effort not in ("None", "none", ""), f"launched with --effort {effort!r}: {argv}"
    else:
        assert result.get("error"), result


@pytest.mark.parametrize("value", [["-low", "-high"], "-low", 5])
def test_guard_non_mapping_effort_suffixes_fails_at_load_not_mid_start(
        tmp_path, monkeypatch, value):
    # guard: a non-mapping is rejected when the providers are loaded (today an
    # AttributeError/TypeError from Provider.from_dict), never inside start().
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = value
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m-low",
                                           "effort": "high"})
    try:
        runner = _project(tmp_path, monkeypatch, agent, providers)
    except Exception:
        return
    _budgets(monkeypatch, acme=1.0)
    result = _start(runner)
    assert "raised" not in result or not isinstance(
        result["raised"], (AttributeError, TypeError)), result


# ---------------------------------------------------------------------------
# Malformed `budget.max_reading_age_seconds` (guards: must fail safe)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["60", -1, 0, float("nan"), True, None, [60]])
def test_guard_malformed_reading_age_falls_back_to_the_default(tmp_path, monkeypatch, bad):
    # A fresh (60 s) full reading must stay unusable (fail safe = default
    # 3600), and a 13000 s one must still be demoted; nothing may crash.
    fresh = Aged(tmp_path / "a", monkeypatch, {**FULL, "stale_seconds": 60}, max_age=bad)
    assert fresh.provider() == "zeta"
    old = Aged(tmp_path / "b", monkeypatch, {**FULL, "stale_seconds": 13000}, max_age=bad)
    assert old.provider() == "acme"


@pytest.mark.parametrize("stale", [-13000, "13000", float("inf"), True])
def test_guard_crafted_stale_seconds_never_makes_a_full_reading_usable(
        tmp_path, monkeypatch, stale):
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": stale})
    assert a.provider() == "zeta"


# ---------------------------------------------------------------------------
# RM-R4b: a demoted reading must not block routing by another path
# ---------------------------------------------------------------------------

def test_a_stale_reading_does_not_wind_its_provider_down(tmp_path, monkeypatch):
    # RM-R4b: a reading older than the bound "routes as unknown". Its raw
    # headroom (0.0) is still sampled into the burn series by `start()`
    # (`note_headroom`), and `_wind_down` — which skips only unusable
    # readings, and a stale one is "usable" — projects a wall from it and
    # cools the provider down. The stale reading ends up blocking the very
    # provider RM-R4b says to treat as unknown.
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 13000})
    t = time.time()
    with a.runner.tree.transaction() as data:
        data.setdefault("headroom", {})["acme"] = [
            [t - 900, 0.6, 0.0], [t - 600, 0.4, 0.0], [t - 300, 0.2, 0.0]]

    assert a.provider() == "acme", (
        "a stale reading, which routes as unknown, wound its provider down")


# ---------------------------------------------------------------------------
# RM-R4b: "the age grows with the time spent in the cache. It is not frozen."
# ---------------------------------------------------------------------------

def test_cached_reading_age_is_not_frozen_by_a_backwards_clock_step(tmp_path, monkeypatch):
    # The wall clock steps back 2 h (NTP correction, VM resume) just after a
    # 3590 s-old full reading was cached. Under RM-R4f the next cache hit sees
    # `now < last_seen`, invalidates the entry and re-reads in the same call;
    # the invalidated budget is never returned. The provider's fresh answer
    # (a real reading ~6600 s old) decides, so acme is no longer vetoed.
    a = Aged(tmp_path, monkeypatch, {**FULL, "stale_seconds": 3590})
    assert a.provider() == "zeta"

    real_read = budget_mod._from_script
    reads = []

    def second_opinion(*args):
        reads.append(args)
        budget = real_read(*args)
        budget.stale_seconds = 6600      # the honest, older measurement
        return budget

    monkeypatch.setattr(budget_mod, "_from_script", second_opinion)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 7200 + 3000)

    assert a.provider() == "acme", "the cached reading's age froze after a clock step"
    assert reads, "a backward clock step did not trigger a re-read"


# ---------------------------------------------------------------------------
# RM-R1: concurrent resumed consults and the last slot
# ---------------------------------------------------------------------------

def test_two_concurrent_resumed_consults_cannot_both_take_the_last_slot(
        tmp_path, monkeypatch):
    # cap 1, nothing running, two standing idle conversations. Each resume
    # passes `_admission` while the other's node is still `idle` (it only
    # turns `running` after `await executor.start`), so both launch.
    providers, probes = _fakes(tmp_path, "acme")
    agents = {name: AgentSpec(name, "acme", "acme-large", conversational=True)
              for name in ("advisor", "critic")}
    runner = h.make_runner(tmp_path / "proj", monkeypatch, agents=agents,
                           providers=providers,
                           project={"limits": {"max_concurrent": 1}})
    for i, name in enumerate(agents):
        node_id = f"ag-5ta0d{i}"
        worktree = runner.paths.worktree(node_id)
        worktree.mkdir(parents=True)
        runner.tree.add(Node(
            id=node_id, agent=name, provider="acme", model="acme-large",
            parent=None, depth=1, status="idle", session_id=f"sess-{name}",
            worktree=str(worktree), conversation=True, turns=1))

    # A process spawn is not instant (a container exec takes seconds); the
    # node stays `idle` for as long as `executor.start` is awaited.
    executor_cls = type(runner.executor())
    real_start = executor_cls.start

    async def slow_start(self, *args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_start(self, *args, **kwargs)

    monkeypatch.setattr(executor_cls, "start", slow_start)

    async def both():
        async def one(name):
            try:
                return await runner.consult(name, "q", timeout=60)
            except Exception as exc:
                return {"error": str(exc)}
        return await asyncio.gather(one("advisor"), one("critic"))

    results = asyncio.run(both())

    launched = len(_calls(probes["acme"]))
    assert launched <= 1, (
        f"max_concurrent=1 admitted {launched} concurrent resumed turns: {results}")


# ---------------------------------------------------------------------------
# RM-R5a: event recorded for the normalisation on a NEW conversation
# ---------------------------------------------------------------------------

def test_guard_consult_normalisation_event_names_the_conversation_node(
        tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("advisor", {
        "provider": "acme", "model": "gem-high", "effort": "low",
        "conversational": True})
    runner = _project(tmp_path, monkeypatch, agent, providers)

    result = asyncio.run(runner.consult("advisor", "q", timeout=60))

    hits = [e for e in _events(runner) if {"low", "high"} <= set(_values(e))]
    assert hits and any(result.get("agent_id") in _values(e) for e in hits), hits


# ---------------------------------------------------------------------------
# RM-R5a persistence, from a process that did not launch the run (guards:
# they pass today; without them every line of the persistence path — the
# Node.effort write, the consult-resume override, the steer override — can
# be deleted with tests/test_m_routing_fixes.py staying green, because its
# consult test resumes from the same in-process Run.)
# ---------------------------------------------------------------------------

def _advisor(tmp_path, monkeypatch):
    providers, probes = _fakes(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("advisor", {
        "provider": "acme", "model": "gem-high", "effort": "low",
        "conversational": True})
    return _project(tmp_path, monkeypatch, agent, providers), probes


def test_guard_normalised_effort_survives_a_consult_resume_in_a_new_process(
        tmp_path, monkeypatch):
    from multiagents.runner import Runner

    runner, probes = _advisor(tmp_path, monkeypatch)
    first = asyncio.run(runner.consult("advisor", "q1", timeout=60))
    assert not first.get("error"), first

    fresh = Runner(runner.paths, runner.config)       # another MCP server
    second = asyncio.run(fresh.consult("advisor", "q2", timeout=60))

    assert not second.get("error"), second
    calls = _calls(probes["acme"])
    assert len(calls) == 2 and "--resume" in calls[1], calls
    assert calls[1][calls[1].index("--effort") + 1] == "high", calls[1]


def test_guard_steer_rebuilds_the_persisted_normalised_effort(tmp_path, monkeypatch):
    from multiagents.runner import Runner

    runner, probes = _advisor(tmp_path, monkeypatch)
    first = asyncio.run(runner.consult("advisor", "q1", timeout=60))
    node = runner.tree.get(first["agent_id"])
    assert node.effort == "high", node

    spec, provider = Runner(runner.paths, runner.config)._spec_of(node)

    assert (spec.model, spec.effort, provider.name) == ("gem-high", "high", "acme")
