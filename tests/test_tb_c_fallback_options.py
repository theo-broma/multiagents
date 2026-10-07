"""TB-R6 — provider options in a fallback entry apply on every fallback path
(context/specs/tooling-batch-2026-10.md, package C).

Contract: options declared for a provider in a fallback map (`variant`, or any
key the provider's `spawn.optional` consumes) are applied whenever that
fallback is used: automatic fallback selection and an explicitly chosen
fallback model alike. An empty value in the entry clears the option.

Observed on the command line the fake provider CLI was launched with, through
`Runner.start()` and `steer()`. The fake providers and seams are those of
`test_fo_fallback_options.py`.

Silence, raised rather than invented: the spec says "an explicit pin on the
run takes precedence over the fallback entry's option", but a run's only pin
(`start(model=...)`) pins a MODEL, and `test_fo_fallback_options.py`
(FO-R1, `..._beats_the_entry_model_but_not_its_options`) already rules that a
model pin does not displace the entry's options. There is no run-level option
pin on the surface, so the sentence cannot be tested without inventing one.
Tested here only: a model pin is the pin that names a fallback (path 2), and
the pinned model, not the entry's, is what runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fo_fallback_options import (  # noqa: E402
    _calls, _only, _opt, _providers, _runner, _start, _steer)

AUTOMATIC = "automatic"
EXPLICIT = "explicit"


def _run_on_fallback(path, tmp_path, monkeypatch, entry, *, top=None, **data_extra):
    """Run the agent so that it lands on `zeta` by `path`; returns
    (runner, probes, start_result)."""
    providers, probes = _providers(tmp_path, "acme", "zeta")
    data = {"provider": "acme", "model": "m1", "models": {"zeta": entry}, **data_extra}
    if top:
        data.update(top)
    if path == AUTOMATIC:
        runner = _runner(tmp_path, monkeypatch, data, providers, chain=["zeta"],
                         budgets={"acme": 0.0, "zeta": 1.0})
        started = _start(runner)
    else:
        runner = _runner(tmp_path, monkeypatch, data, providers)
        started = _start(runner, model=entry["model"])
    assert not _calls(probes["acme"]), f"the primary ran on the {path} path"
    return runner, probes, started


PATHS = pytest.mark.parametrize("path", [AUTOMATIC, EXPLICIT])


@PATHS
def test_tb_r6_a_fallback_entry_variant_reaches_the_command_line(path, tmp_path, monkeypatch):
    _, probes, _ = _run_on_fallback(path, tmp_path, monkeypatch,
                                    {"model": "z1", "variant": "max"})

    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == "max"


@PATHS
def test_tb_r6_the_entry_variant_beats_the_agents_top_level_variant(path, tmp_path, monkeypatch):
    _, probes, _ = _run_on_fallback(path, tmp_path, monkeypatch,
                                    {"model": "z1", "variant": "max"},
                                    top={"variant": "high"})

    assert _opt(_only(probes, "zeta"), "--variant") == "max"


@PATHS
def test_tb_r6_an_empty_entry_variant_clears_the_top_level_option(path, tmp_path, monkeypatch):
    _, probes, _ = _run_on_fallback(path, tmp_path, monkeypatch,
                                    {"model": "z1", "variant": ""},
                                    top={"variant": "high"})

    argv = _only(probes, "zeta")
    assert "--variant" not in argv, f"an empty value must clear the option: {argv}"


@PATHS
def test_tb_r6_an_entry_without_the_option_leaves_the_top_level_one_alone(
        path, tmp_path, monkeypatch):
    _, probes, _ = _run_on_fallback(path, tmp_path, monkeypatch,
                                    {"model": "z1", "effort": "low"},
                                    top={"variant": "high"})

    argv = _only(probes, "zeta")
    assert _opt(argv, "--variant") == "high"
    assert _opt(argv, "--effort") == "low"


@PATHS
def test_tb_r6_any_option_the_provider_consumes_applies_not_only_variant(
        path, tmp_path, monkeypatch):
    _, probes, _ = _run_on_fallback(path, tmp_path, monkeypatch,
                                    {"model": "z1", "flavour": "mint", "variant": "max"})

    argv = _only(probes, "zeta")
    assert _opt(argv, "--flavour") == "mint"
    assert _opt(argv, "--variant") == "max"


@PATHS
def test_tb_r6_an_option_of_another_providers_entry_does_not_bleed(path, tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta", "yotta")
    data = {"provider": "acme", "model": "m1",
            "models": {"zeta": {"model": "z1", "variant": "zv"},
                       "yotta": {"model": "y1", "variant": "yv"}}}
    if path == AUTOMATIC:
        runner = _runner(tmp_path, monkeypatch, data, providers, chain=["zeta", "yotta"],
                         budgets={"acme": 0.0, "zeta": 1.0, "yotta": 1.0})
        _start(runner)
        landed = [n for n in ("zeta", "yotta") if _calls(probes[n])]
        assert len(landed) == 1, landed
        destination = landed[0]
    else:
        runner = _runner(tmp_path, monkeypatch, data, providers)
        _start(runner, model="y1")
        destination = "yotta"
        assert not _calls(probes["zeta"])

    expected = {"zeta": "zv", "yotta": "yv"}[destination]
    assert _opt(_only(probes, destination), "--variant") == expected


@PATHS
def test_tb_r6_a_steer_on_the_fallback_keeps_applying_the_entry_option(
        path, tmp_path, monkeypatch):
    runner, probes, started = _run_on_fallback(path, tmp_path, monkeypatch,
                                               {"model": "z1", "variant": "max"},
                                               top={"variant": "high"})

    _steer(runner, started["agent_id"])

    calls = _calls(probes["zeta"])
    assert len(calls) == 2, calls
    assert [_opt(c, "--variant") for c in calls] == ["max", "max"], calls
    assert not _calls(probes["acme"])


@PATHS
def test_tb_r6_a_steer_on_the_fallback_keeps_an_empty_entry_variant_cleared(
        path, tmp_path, monkeypatch):
    runner, probes, started = _run_on_fallback(path, tmp_path, monkeypatch,
                                               {"model": "z1", "variant": ""},
                                               top={"variant": "high"})

    _steer(runner, started["agent_id"])

    calls = _calls(probes["zeta"])
    assert len(calls) == 2, calls
    assert all("--variant" not in c for c in calls), calls


def test_tb_r6_naming_the_fallbacks_model_runs_there_and_the_pinned_model_is_what_runs(
        tmp_path, monkeypatch):
    # The pin is the pin: its model, the entry's options.
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"zeta": {"model": "z1", "variant": "max"}}},
                     providers)

    _start(runner, model="z1")

    assert not _calls(probes["acme"])
    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == "max"


def test_tb_r6_the_two_paths_render_the_same_options(tmp_path, monkeypatch):
    # The defect is a divergence: whatever one path renders, the other must too.
    entry = {"model": "z1", "variant": "max", "flavour": "mint", "effort": "low"}
    rendered = {}
    for path in (AUTOMATIC, EXPLICIT):
        sub = tmp_path / path
        _, probes, _ = _run_on_fallback(path, sub, monkeypatch, dict(entry),
                                        top={"variant": "high"})
        argv = _only(probes, "zeta")
        rendered[path] = {f: _opt(argv, f)
                          for f in ("--model", "--variant", "--flavour", "--effort")}

    assert rendered[AUTOMATIC] == rendered[EXPLICIT]
    assert rendered[EXPLICIT] == {"--model": "z1", "--variant": "max",
                                  "--flavour": "mint", "--effort": "low"}


@pytest.mark.parametrize("entry_variant,expected", [("max", "max"), ("", None)])
def test_tb_r6_an_explicit_provider_steer_onto_the_fallback_applies_its_entry(
        entry_variant, expected, tmp_path, monkeypatch):
    # A third explicit route to a fallback: steer(provider=...) moves a run
    # that began on the primary. The entry's option (or its clearing) applies.
    import asyncio
    providers, probes = _providers(tmp_path, "acme", "zeta")
    runner = _runner(tmp_path, monkeypatch,
                     {"provider": "acme", "model": "m1", "variant": "high",
                      "models": {"zeta": {"model": "z1", "variant": entry_variant}}},
                     providers)
    started = _start(runner)

    async def go():
        result = await runner.steer(started["agent_id"], "go on", provider="zeta")
        run = runner.runs.get(started["agent_id"])
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    result = asyncio.run(go())

    assert not result.get("error"), result
    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == expected


@PATHS
def test_tb_r6_an_entry_naming_its_model_by_id_applies_its_options_too(
        path, tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    data = {"provider": "acme", "model": "m1", "variant": "high",
            "models": {"zeta": {"id": "z1", "variant": "max"}}}
    if path == AUTOMATIC:
        runner = _runner(tmp_path, monkeypatch, data, providers, chain=["zeta"],
                         budgets={"acme": 0.0, "zeta": 1.0})
        _start(runner)
    else:
        runner = _runner(tmp_path, monkeypatch, data, providers)
        _start(runner, model="z1")

    argv = _only(probes, "zeta")
    assert _opt(argv, "--model") == "z1"
    assert _opt(argv, "--variant") == "max"
