"""Regression tests for the round-1 final-review decisions (FO-R3b/R1b/R1c/R4a).

Each finding is pinned by one test, red before its fix. The runner seams are
reused from `test_m_routing_fixes` (effort suffixes) and
`test_conversation_provider_change` (the fake CLI), so nothing here stubs the
production path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.models import validate_agent_models  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli  # noqa: E402
from test_m_routing_fixes import SUFFIXES, _budgets, _project, _start  # noqa: E402


def _providers(tmp_path, *names, families=None, optional=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers, probes = {}, {}
    for name in names:
        providers[name], probes[name] = _fake_cli(tmp_path, name)
        if families and name in families:
            providers[name]["family"] = families[name]
        if optional:
            providers[name]["spawn"]["optional"].update(optional)
    return providers, probes


def _only(probes, name):
    calls = _calls(probes[name])
    assert len(calls) == 1, f"expected one launch on {name}, got {calls}"
    return calls[0]


def _opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def _load(tmp_path, agents):
    from multiagents.paths import ProjectPaths
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": agents}))
    return config_mod.load(paths)


# ---------------------------------------------------------------------------
# FO-R3b: a reloaded Config still carries its warnings
# ---------------------------------------------------------------------------

def test_fo_r3b_a_reloaded_config_still_carries_and_shows_its_warnings(tmp_path):
    agents = {"fo-r2-reload": {
        "provider": "opencode", "model": "opencode-go/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm",
                                    "bogus_r2_key": 1}}}}

    first = _load(tmp_path / "a", agents)
    # The MCP server's lifecycle: reload WITHOUT validating the first Config.
    second = _load(tmp_path / "b", agents)

    assert "fo-r2-reload" in "\n".join(validate_agent_models(first))
    shown = "\n".join(validate_agent_models(second))
    assert "fo-r2-reload" in shown and "bogus_r2_key" in shown, shown


# ---------------------------------------------------------------------------
# FO-R1b: a primary entry's effort is explicit and refused on conflict
# ---------------------------------------------------------------------------

def test_fo_r1b_a_primary_entry_effort_is_explicit_and_refused(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1",
        "models": {"acme": {"model": "gem-high", "effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, acme=1.0)

    result = _start(runner)

    assert result.get("raised") or result.get("error"), result
    assert not _calls(probes["acme"]), "the contradicted pair reached the CLI"


def test_fo_r1b_a_top_level_effort_is_still_normalised(tmp_path, monkeypatch):
    # The other half: a top-level effort keeps today's treatment.
    providers, probes = _providers(tmp_path, "acme", optional={
        "effort": ["--effort", "{effort}"]})
    providers["acme"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-high", "effort": "low"})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, acme=1.0)

    result = _start(runner)

    assert not result.get("error"), result
    argv = _only(probes, "acme")
    assert _opt(argv, "--effort") == "high", argv


# ---------------------------------------------------------------------------
# FO-R1c: an options-only entry applies on a same-family destination
# ---------------------------------------------------------------------------

def test_fo_r1c_options_only_entry_on_a_family_sibling_is_applied(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "acme-b",
                                   families={"acme": "acme", "acme-b": "acme"},
                                   optional={"variant": ["--variant", "{variant}"]})
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1", "variant": "high",
        "models": {"acme-b": {"variant": "max"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, **{"acme": 0.0, "acme-b": 1.0})

    result = _start(runner)

    assert not result.get("error"), result
    argv = _only(probes, "acme-b")
    assert _opt(argv, "--variant") == "max", argv


def test_fo_r1b_a_family_destination_entry_effort_is_explicit(tmp_path, monkeypatch):
    # P2: acme-b is itself a listed `models:` key in acme's family, and its
    # entry carries the effort. Without a `model:` in the entry the old
    # attribution missed it and normalised low -> high.
    providers, probes = _providers(tmp_path, "acme", "acme-b",
                                   families={"acme": "acme", "acme-b": "acme"})
    providers["acme-b"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-high",
        "models": {"acme-b": {"effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, **{"acme": 0.0, "acme-b": 1.0})

    result = _start(runner)

    assert result.get("raised") or result.get("error"), result
    assert not _calls(probes["acme-b"]), "the contradicted pair reached the CLI"


def test_fo_r1b_an_options_only_entry_effort_is_explicit_on_a_sibling_route(
        tmp_path, monkeypatch):
    # P3: b1 names the model, b2 carries the effort; the run lands on b2. The
    # effort is b2's own entry's, and must be refused rather than attributed
    # to b1 (which sets none).
    providers, probes = _providers(tmp_path, "acme", "b1", "b2",
                                   families={"acme": "acme", "b1": "acme",
                                             "b2": "acme"})
    providers["b2"]["effort_suffixes"] = dict(SUFFIXES)
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "gem-high",
        "models": {"b1": {"model": "gem-high"}, "b2": {"effort": "low"}}})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, **{"acme": 0.0, "b1": 0.0, "b2": 1.0})

    result = _start(runner)

    assert result.get("raised") or result.get("error"), result
    assert not _calls(probes["b2"]), "the contradicted pair reached the CLI"


# ---------------------------------------------------------------------------
# FO-R4a: a bare entry for the own provider sets the model
# ---------------------------------------------------------------------------

def test_fo_r4a_a_bare_entry_for_the_own_provider_sets_the_model(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    agent = AgentSpec.from_dict("worker", {
        "provider": "acme", "model": "m1", "models": {"acme": "m-entry"}})
    runner = _project(tmp_path, monkeypatch, agent, providers)
    _budgets(monkeypatch, acme=1.0)

    result = _start(runner)

    assert not result.get("error"), result
    argv = _only(probes, "acme")
    assert _opt(argv, "--model") == "m-entry", argv
