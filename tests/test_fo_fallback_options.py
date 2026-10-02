"""Black-box contract for `context/specs/fallback-options.md` (FO-R1..FO-R4).

Everything is asserted on the command line a fake provider CLI was launched
with (`--variant`, and a second invented option `--flavour`), through
`Runner.start()`, `steer()` and `consult()`, plus `config.load()` for FO-R3.
Provider names are invented; `variant` reaches the CLI via `spawn.optional`,
the same way opencode-shaped providers do it.

Silences, stated rather than invented:
- The spec does not name the "config warning" channel. FO-R3 tests therefore
  accept the warning from any of: the `logging` module, `warnings.warn`,
  stdout/stderr, or a `warnings` attribute on the loaded Config. See the run
  report.
- FO-R3's "once per process" is keyed on (agent, provider, key); each test
  uses names no other test uses so the process-wide memory cannot leak between
  tests (or between xdist workers).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import sys
import warnings as warnings_mod
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli  # noqa: E402


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def _providers(tmp_path, *names):
    """Fake CLIs whose providers consume `variant` and `flavour` (and `effort`)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers, probes = {}, {}
    for name in names:
        providers[name], probes[name] = _fake_cli(tmp_path, name)
        providers[name]["spawn"]["optional"].update({
            "variant": ["--variant", "{variant}"],
            "flavour": ["--flavour", "{flavour}"],
        })
    return providers, probes


def _runner(tmp_path, monkeypatch, data, providers, *, chain=None, budgets=None):
    agent = AgentSpec.from_dict("worker", data)
    project = {"budget": {"fallback_chain": chain}} if chain else {}
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           agents={"worker": agent}, providers=providers,
                           project=project)
    if budgets:
        readings = {n: budget_mod.Budget(n, known=True, headroom=room)
                    for n, room in budgets.items()}
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: readings)
    return runner


def _start(runner, **kwargs):
    async def go():
        result = await runner.start("worker", "work", **kwargs)
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _steer(runner, agent_id, message="go on"):
    async def go():
        result = await runner.steer(agent_id, message)
        run = runner.runs.get(agent_id)
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _consult(runner, message="next"):
    return asyncio.run(runner.consult("worker", message, timeout=60))


def _opt(argv, flag):
    """The value after `flag`, or None when the flag is absent."""
    return argv[argv.index(flag) + 1] if flag in argv else None


def _only(probes, name):
    calls = _calls(probes[name])
    assert len(calls) == 1, f"expected exactly one launch on {name}, got {calls}"
    return calls[0]


# ---------------------------------------------------------------------------
# FO-R1: placeholder options in a `models:` entry are applied
# ---------------------------------------------------------------------------

def test_fo_r1_primary_entry_variant_reaches_the_command_line(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1",
                      "models": {"acme": {"model": "m1", "variant": "max"}}},
                     providers)

    _start(runner)

    assert _opt(_only(probes, "acme"), "--variant") == "max"


def test_fo_r1_primary_entry_variant_beats_the_top_level_variant(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"acme": {"model": "m1", "variant": "max"}}},
                     providers)

    _start(runner)

    assert _opt(_only(probes, "acme"), "--variant") == "max"


def test_fo_r1_primary_entry_empty_variant_clears_the_top_level_one(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"acme": {"model": "m1", "variant": ""}}},
                     providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert "--variant" not in argv, f"an empty variant must omit the flag: {argv}"


def test_fo_r1_primary_entry_model_beats_the_top_level_model(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "top-model",
                      "models": {"acme": {"model": "entry-model"}}},
                     providers)

    _start(runner)

    assert _opt(_only(probes, "acme"), "--model") == "entry-model"


def test_fo_r1_explicit_model_pin_beats_the_entry_model_but_not_its_options(
        tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "top-model",
                      "models": {"acme": {"model": "entry-model", "variant": "max"}}},
                     providers)

    _start(runner, model="pinned-model")

    argv = _only(probes, "acme")
    assert _opt(argv, "--model") == "pinned-model"
    assert _opt(argv, "--variant") == "max"


def test_fo_r1_entry_options_a_provider_consumes_are_applied_whatever_their_name(
        tmp_path, monkeypatch):
    # `flavour` is no dataclass field and no shipped option: only the
    # provider's own `spawn.optional` makes it one.
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1",
                      "models": {"acme": {"model": "m1", "flavour": "mint"}}},
                     providers)

    _start(runner)

    assert _opt(_only(probes, "acme"), "--flavour") == "mint"


def test_fo_r1_fallback_entry_variant_reaches_the_command_line(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1",
                      "models": {"zeta": {"model": "z1", "variant": "max"}}},
                     providers, chain=["zeta"], budgets={"acme": 0.0, "zeta": 1.0})

    _start(runner)

    assert not _calls(probes["acme"]), "the exhausted provider ran"
    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == "max"


def test_fo_r1_fallback_entry_empty_variant_clears_the_top_level_one(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"zeta": {"model": "z1", "variant": ""}}},
                     providers, chain=["zeta"], budgets={"acme": 0.0, "zeta": 1.0})

    _start(runner)

    argv = _only(probes, "zeta")
    assert "--variant" not in argv, argv


def test_fo_r1_an_entry_for_one_provider_does_not_bleed_into_another(tmp_path, monkeypatch):
    # Two routes with different variants; each run gets its own destination's.
    for exhausted, expected in (("acme", "zv"), ("zeta", "av")):
        sub = tmp_path / exhausted
        providers, probes = _providers(sub, "acme", "zeta", "yotta")
        runner = _runner(sub, monkeypatch,
                         {"provider": "yotta", "model": "y1", "variant": "top",
                          "models": {"acme": {"model": "a1", "variant": "av"},
                                     "zeta": {"model": "z1", "variant": "zv"}}},
                         providers, chain=["acme", "zeta"],
                         budgets={"yotta": 0.0, "acme": 0.0 if exhausted == "acme" else 1.0,
                                  "zeta": 0.0 if exhausted == "zeta" else 1.0})

        _start(runner)

        survivor = "zeta" if exhausted == "acme" else "acme"
        assert _opt(_only(probes, survivor), "--variant") == expected


def test_fo_r1_steer_applies_the_entry_options(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"acme": {"model": "m1", "variant": "max"}}},
                     providers)
    started = _start(runner)

    _steer(runner, started["agent_id"])

    calls = _calls(probes["acme"])
    assert len(calls) == 2, calls
    assert [_opt(c, "--variant") for c in calls] == ["max", "max"], calls


def test_fo_r1_steer_with_an_empty_entry_variant_omits_the_flag(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"acme": {"model": "m1", "variant": ""}}},
                     providers)
    started = _start(runner)

    _steer(runner, started["agent_id"])

    calls = _calls(probes["acme"])
    assert len(calls) == 2, calls
    assert all("--variant" not in c for c in calls), calls


def test_fo_r1_consult_applies_the_entry_options_on_every_turn(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "conversational": True,
                      "models": {"acme": {"model": "m1", "variant": "max"}}},
                     providers)

    first = _consult(runner, "one")
    second = _consult(runner, "two")

    assert not first.get("error") and not second.get("error"), (first, second)
    calls = _calls(probes["acme"])
    assert len(calls) == 2, calls
    assert [_opt(c, "--variant") for c in calls] == ["max", "max"], calls


def test_fo_r1_consult_resuming_on_a_fallback_provider_applies_that_entry(
        tmp_path, monkeypatch):
    # The standing conversation lives on `acme`, which is not the agent's
    # primary (`zeta`) but is a `models:` route: the resume runs there.
    from multiagents.tree import Node
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "zeta", "model": "z1", "conversational": True,
                      "variant": "high",
                      "models": {"acme": {"model": "a1", "variant": "max"}}},
                     providers)
    worktree = runner.paths.worktree("ag-f0a0c1")
    worktree.mkdir(parents=True)
    runner.tree.add(Node(id="ag-f0a0c1", agent="worker", provider="acme", model="a1",
                         parent=None, depth=1, status="idle", session_id="sess-old",
                         worktree=str(worktree), conversation=True, turns=1))

    result = _consult(runner)

    assert not result.get("error"), result
    assert not _calls(probes["zeta"])
    assert _opt(_only(probes, "acme"), "--variant") == "max"


def test_fo_r1_the_merge_never_mutates_the_shared_spec(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    data = {"provider": "acme", "model": "m1", "variant": "high",
            "models": {"acme": {"model": "m1-entry", "variant": "max"},
                       "zeta": {"model": "z1", "variant": "zmax"}}}
    runner = _runner(tmp_path, monkeypatch, data, providers)
    spec = runner.config.agent("worker")
    before = (spec.model, spec.provider, dict(spec.extra),
              getattr(spec, "variant", None), copy.deepcopy(spec.models))

    _start(runner)
    _start(runner)

    spec = runner.config.agent("worker")
    after = (spec.model, spec.provider, dict(spec.extra),
             getattr(spec, "variant", None), copy.deepcopy(spec.models))
    assert after == before
    # and a second run is not poisoned by the first: both saw the entry
    assert [_opt(c, "--variant") for c in _calls(probes["acme"])] == ["max", "max"]


def test_fo_r1_the_entry_applies_only_to_its_own_provider_run(tmp_path, monkeypatch):
    # A run on the primary uses the primary's entry, not a sibling entry's.
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"zeta": {"model": "z1", "variant": "max"}}},
                     providers)

    _start(runner)

    assert _opt(_only(probes, "acme"), "--variant") == "high"


# ---------------------------------------------------------------------------
# FO-R2: a bare-string entry keeps today's behaviour
# ---------------------------------------------------------------------------

def test_fo_r2_bare_string_fallback_entry_keeps_the_top_level_variant(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"zeta": "z1"}},
                     providers, chain=["zeta"], budgets={"acme": 0.0, "zeta": 1.0})

    _start(runner)

    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == "high"


def test_fo_r2_bare_string_primary_entry_changes_only_the_model(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"acme": "m-entry"}},
                     providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert _opt(argv, "--variant") == "high"
    assert _opt(argv, "--model") in ("m1", "m-entry")  # spec is silent on the model here


# ---------------------------------------------------------------------------
# FO-R3: unknown keys are not silently swallowed
# ---------------------------------------------------------------------------

class _Capture:
    """Everything a load could say: logging, `warnings.warn`, stdout/stderr, and
    a `warnings` attribute on the Config."""

    def __init__(self, caplog, capsys):
        self.caplog, self.capsys = caplog, capsys

    def text(self, config=None) -> str:
        out = self.capsys.readouterr()
        parts = [r.getMessage() for r in self.caplog.records] + [out.out, out.err]
        extra = getattr(config, "warnings", None) or []
        parts += [str(w) for w in extra]
        return "\n".join(parts)


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


@pytest.fixture
def cap(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    with warnings_mod.catch_warnings(record=True) as recorded:
        warnings_mod.simplefilter("always")
        capture = _Capture(caplog, capsys)
        capture.recorded = recorded
        yield capture


def _all_text(cap, config=None):
    return cap.text(config) + "\n" + "\n".join(str(w.message) for w in cap.recorded)


def test_fo_r3_an_unconsumed_key_is_reported_naming_agent_provider_and_key(tmp_path, cap):
    config = _load(tmp_path, {"fo3-agent-a": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_fo3_key": 1}}}})

    text = _all_text(cap, config)

    for needle in ("fo3-agent-a", "opencode-zai", "bogus_fo3_key"):
        assert needle in text, f"warning must name {needle!r}; saw: {text!r}"


def test_fo_r3_an_unknown_key_does_not_fail_the_load_and_the_agent_is_usable(tmp_path, cap):
    config = _load(tmp_path, {"fo3-agent-b": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_fo3_key_b": 1}}}})

    spec = config.agents["fo3-agent-b"]
    assert spec.fallback_for("opencode-zai")[0] == "zai-coding-plan/glm"
    assert spec.model == "opencode/x"


def test_fo_r3_the_warning_is_emitted_once_per_agent_provider_key(tmp_path, cap):
    agents = {"fo3-agent-c": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_fo3_key_c": 1}}}}

    first = _all_text(cap, _load(tmp_path / "a", agents))
    second = _all_text(cap, _load(tmp_path / "b", agents))

    assert first.count("bogus_fo3_key_c") >= 1, "no warning on the first load"
    assert "bogus_fo3_key_c" not in second, (
        f"the same (agent, provider, key) was reported again: {second!r}")


def test_fo_r3_a_distinct_key_is_reported_in_its_own_right(tmp_path, cap):
    base = {"provider": "opencode", "model": "opencode/x"}
    _all_text(cap, _load(tmp_path / "a", {"fo3-agent-d": {
        **base, "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_d1": 1}}}}))

    text = _all_text(cap, _load(tmp_path / "b", {"fo3-agent-d": {
        **base, "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_d2": 1}}}}))

    assert "bogus_d2" in text


def test_fo_r3_the_same_key_on_another_provider_is_reported_again(tmp_path, cap):
    base = {"provider": "opencode", "model": "opencode/x"}
    _all_text(cap, _load(tmp_path / "a", {"fo3-agent-e": {
        **base, "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "bogus_e": 1}}}}))

    text = _all_text(cap, _load(tmp_path / "b", {"fo3-agent-e": {
        **base, "models": {"claude": {"model": "sonnet", "bogus_e": 1}}}}))

    assert "bogus_e" in text and "claude" in text


@pytest.mark.parametrize("provider,model,key,value", [
    # Each key is paired with a destination whose own resolved spawn.optional
    # consumes it (FO-R3a): validity is the destination provider's alone.
    ("opencode-zai", "zai-coding-plan/glm", "variant", "max"),
    ("codex", "gpt-5", "effort", "high"),
    ("claude", "sonnet", "effort", "high"),
    ("claude", "sonnet", "max_budget_usd", 5),
    ("claude", "sonnet", "autocompact", True),
    ("opencode-zai", "zai-coding-plan/glm", "permission", "full"),
    ("opencode-zai", "zai-coding-plan/glm", "id", "zai-coding-plan/glm"),
    ("opencode-zai", "zai-coding-plan/glm", "model", "zai-coding-plan/glm")])
def test_fo_r3_valid_keys_are_not_reported(tmp_path, cap, provider, model, key, value):
    entry = {"model": model, key: value}
    agent = f"fo3-ok-{provider}-{key}"
    config = _load(tmp_path, {agent: {
        "provider": "opencode", "model": "opencode/x", "models": {provider: entry}}})

    text = _all_text(cap, config)

    assert agent not in text, f"valid key {key!r} under {provider} was reported: {text!r}"


@pytest.mark.parametrize("provider,model,key,value", [
    # FO-R3a: consumed by another shipped provider, not by the destination.
    ("opencode-zai", "zai-coding-plan/glm", "max_budget_usd", 5),
    ("opencode-zai", "zai-coding-plan/glm", "autocompact", True),
    ("codex", "gpt-5", "variant", "max"),
    ("claude", "sonnet", "variant", "max")])
def test_fo_r3a_a_key_only_another_shipped_provider_consumes_is_reported(
        tmp_path, cap, provider, model, key, value):
    agent = "fo3a-agent"
    config = _load(tmp_path, {agent: {
        "provider": "opencode", "model": "opencode/x",
        "models": {provider: {"model": model, key: value}}}})

    text = _all_text(cap, config)

    for needle in (agent, provider, key):
        assert needle in text, (needle, text)


def test_fo_r3_a_key_consumed_by_a_configured_provider_is_not_reported(tmp_path, cap):
    providers = {"fo3prov": {"extends": "opencode", "bin": "fo3prov",
                             "models_include": ["fo3/*"],
                             "spawn": {"optional": {"fo3_flavour": ["--flavour", "{fo3_flavour}"]}}}}
    config = _load(tmp_path, {"fo3-agent-f": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"fo3prov": {"model": "fo3/m", "fo3_flavour": "mint"}}}},
        providers=providers)

    assert "fo3_flavour" not in _all_text(cap, config)


def test_fo_r3_a_key_only_a_different_provider_consumes_is_reported(tmp_path, cap):
    # The key is valid for the DESTINATION provider only: `fo3_flavour` is
    # consumed by fo3prov, but the entry is for opencode-zai.
    providers = {"fo3prov": {"extends": "opencode", "bin": "fo3prov",
                             "models_include": ["fo3/*"],
                             "spawn": {"optional": {"fo3_flavour": ["--flavour", "{fo3_flavour}"]}}}}
    config = _load(tmp_path, {"fo3-agent-g": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm", "fo3_flavour": "mint"}}}},
        providers=providers)

    text = _all_text(cap, config)

    for needle in ("fo3-agent-g", "opencode-zai", "fo3_flavour"):
        assert needle in text, (needle, text)


def test_fo_r3_bare_string_entries_produce_no_warning(tmp_path, cap):
    config = _load(tmp_path, {"fo3-agent-h": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": "zai-coding-plan/glm"}}})

    assert "fo3-agent-h" not in _all_text(cap, config)


# ---------------------------------------------------------------------------
# FO-R4: no regression
# ---------------------------------------------------------------------------

def test_fo_r4_agent_without_models_runs_with_its_top_level_variant(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high"}, providers)

    _start(runner)

    argv = _only(probes, "acme")
    assert _opt(argv, "--model") == "m1"
    assert _opt(argv, "--variant") == "high"


def test_fo_r4_dataclass_only_override_still_applies_on_the_fallback(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "effort": "low", "variant": "high",
                      "models": {"zeta": {"model": "z1", "effort": "high"}}},
                     providers, chain=["zeta"], budgets={"acme": 0.0, "zeta": 1.0})

    _start(runner)

    argv = _only(probes, "zeta")
    assert _opt(argv, "--effort") == "high"
    assert _opt(argv, "--variant") == "high", "options the entry does not name travel unchanged"


def test_fo_r4_fallback_for_still_reports_model_and_dataclass_overrides():
    spec = AgentSpec.from_dict("w", {"provider": "a", "model": "m", "models": {
        "z": {"model": "zm", "effort": "high"}, "y": "ym"}})

    assert spec.fallback_for("z") == ("zm", {"effort": "high"})
    assert spec.fallback_for("y") == ("ym", {})
    assert spec.fallback_for("nobody") == ("", {})
