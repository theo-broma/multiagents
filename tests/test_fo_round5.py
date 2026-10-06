"""Regression tests for the codex round-2 findings (FO round 5).

1. FO-R1: a fractional option value (`max_budget_usd: 0.5`) must reach the
   command line; a value the launcher cannot render (a dict/list) must be
   reported, never dropped silently; a bool must not render as Python's
   "True".
2. FO-R3a: a `provider` key in a `models.P` entry is always reported, even
   when P's own `spawn.optional` declares a `provider` placeholder.
"""
from __future__ import annotations

import asyncio
import json
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


def _providers(tmp_path, *names, optional=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers, probes = {}, {}
    for name in names:
        providers[name], probes[name] = _fake_cli(tmp_path, name)
        if optional:
            providers[name]["spawn"]["optional"].update(optional)
    return providers, probes


def _runner(tmp_path, monkeypatch, data, providers):
    agent = AgentSpec.from_dict("worker", data)
    return h.make_runner(tmp_path / "project", monkeypatch,
                         agents={"worker": agent}, providers=providers,
                         project={})


def _start(runner):
    async def go():
        result = await runner.start("worker", "work")
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _only(probes, name):
    calls = _calls(probes[name])
    assert len(calls) == 1, f"expected one launch on {name}, got {calls}"
    return calls[0]


def _opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


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


def _load(tmp_path, agents, *, providers=None):
    from multiagents.paths import ProjectPaths
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": agents}))
    if providers:
        (paths.config / "providers.yaml").write_text(
            yaml.safe_dump({"providers": providers}))
    return config_mod.load(paths)


# ---------------------------------------------------------------------------
# FO-R1: fractional and unrenderable option values
# ---------------------------------------------------------------------------

def test_fo_r1_a_fractional_entry_option_reaches_the_command_line(tmp_path, monkeypatch):
    providers, probes = _providers(
        tmp_path, "acme",
        optional={"max_budget_usd": ["--max-budget-usd", "{max_budget_usd}"]})
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "max_budget_usd": 5,
                      "models": {"acme": {"max_budget_usd": 0.5}}},
                     providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert _opt(argv, "--max-budget-usd") == "0.5", argv


def test_fo_r1_an_unrenderable_option_value_is_reported_not_dropped(tmp_path, monkeypatch):
    providers, probes = _providers(
        tmp_path, "acme", optional={"flavour": ["--flavour", "{flavour}"]})
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1",
                      "models": {"acme": {"flavour": {"a": 1}}}},
                     providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert "--flavour" not in argv, argv
    events = [e for e in _events(runner) if e.get("kind") == "option_not_renderable"]
    assert any(e.get("option") == "flavour" for e in events), events


def test_fo_r1_a_bool_option_is_not_rendered_as_python_true(tmp_path, monkeypatch):
    providers, probes = _providers(
        tmp_path, "acme",
        optional={"autocompact": ["--autocompact", "{autocompact}"]})
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1",
                      "models": {"acme": {"autocompact": True}}},
                     providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert _opt(argv, "--autocompact") == "true", argv
    assert "True" not in argv, argv


# ---------------------------------------------------------------------------
# FO-R3a: `provider` is always reported, even when P declares it
# ---------------------------------------------------------------------------

def test_fo_r3a_a_provider_key_is_reported_even_when_the_provider_declares_it(tmp_path):
    providers = {"custom": {
        "extends": "opencode", "bin": "custom",
        "spawn": {"args": ["--x"],
                  "optional": {"provider": ["--provider", "{provider}"]}}}}
    config = _load(tmp_path, {"fo-r5-provider-key": {
        "provider": "opencode", "model": "opencode-go/x",
        "models": {"custom": {"model": "opencode-go/m", "provider": "other"}}}},
        providers=providers)

    text = "\n".join(config.warnings)
    for needle in ("fo-r5-provider-key", "custom", "provider"):
        assert needle in text, f"warning must name {needle!r}; saw: {text!r}"
    assert "fo-r5-provider-key" in "\n".join(validate_agent_models(config))

    # and it never moves the run.
    routed = config.agents["fo-r5-provider-key"].routed("custom")
    assert routed.provider == "opencode", routed.provider
