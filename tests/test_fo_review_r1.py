"""Regression tests for the round-1 review of the FO implementation.

Two P2s were rejected on commit 1a511df, both about FO-R3:

- the warning was stored on `Config.warnings` and read nowhere, so `doctor`
  never showed it;
- validity was judged against the union of every shipped provider's options,
  so `models: {codex: {variant: max}}` was accepted even though codex's own
  `spawn.optional` has only `effort` and `--variant` is dropped at launch.

These tests assert the corrected behaviour. They live in their own file
because `tests/test_fo_fallback_options.py` is the tester's contract and, for
`max_budget_usd`/`autocompact`, it encodes the old union reading; the review
overrides it. Names are unique per test so the process-wide "once per
(agent, provider, key)" memory cannot leak between tests or xdist workers.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from multiagents import config as config_mod
from multiagents.config import AgentSpec
from multiagents.models import validate_agent_models


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


def _all_text(config) -> str:
    return "\n".join(getattr(config, "warnings", None) or [])


# ---------------------------------------------------------------------------
# P2 #2: validity is the DESTINATION provider's resolved spawn.optional only
# ---------------------------------------------------------------------------

def test_fo_r3_a_shipped_key_the_destination_does_not_consume_is_reported(tmp_path):
    # The reviewer's exact example: codex consumes only `effort`, so `variant`
    # is dropped at launch and must be reported, not waved through because
    # some OTHER shipped provider consumes it.
    config = _load(tmp_path, {"fo-r1-codex-variant": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"codex": {"variant": "max"}}}})

    text = _all_text(config)

    for needle in ("fo-r1-codex-variant", "codex", "variant"):
        assert needle in text, f"warning must name {needle!r}; saw: {text!r}"


def test_fo_r3_a_shipped_option_absent_from_the_route_provider_is_reported(tmp_path):
    # `max_budget_usd` is claude's; on an opencode-zai route it is dropped.
    config = _load(tmp_path, {"fo-r1-zai-budget": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm",
                                    "max_budget_usd": 5}}}})

    text = _all_text(config)

    for needle in ("fo-r1-zai-budget", "opencode-zai", "max_budget_usd"):
        assert needle in text, f"warning must name {needle!r}; saw: {text!r}"


def test_fo_r3_an_option_the_destination_consumes_is_not_reported(tmp_path):
    # Same key, a destination that actually renders it: no warning.
    config = _load(tmp_path, {"fo-r1-claude-budget": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"claude": {"model": "sonnet", "max_budget_usd": 5}}}})

    assert "fo-r1-claude-budget" not in _all_text(config)


def test_fo_r3_a_project_provider_is_validated_the_same_way_as_a_shipped_one(tmp_path):
    providers = {"fo1prov": {"extends": "opencode", "bin": "fo1prov",
                             "models_include": ["fo1/*"],
                             "spawn": {"optional": {"fo1_flag": ["--x", "{fo1_flag}"]}}}}
    ok = _load(tmp_path / "a", {"fo-r1-proj-ok": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"fo1prov": {"model": "fo1/m", "fo1_flag": "yes"}}}},
        providers=providers)
    bad = _load(tmp_path / "b", {"fo-r1-proj-bad": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"opencode-zai": {"model": "zai-coding-plan/glm",
                                    "fo1_flag": "yes"}}}},
        providers=providers)

    assert "fo1_flag" not in _all_text(ok)
    assert "fo1_flag" in _all_text(bad)


# The round-1 "once per process" assertion was reversed by FO-R3b (dedup at
# display only); the reload case is pinned in tests/test_fo_review_r2.py.


# ---------------------------------------------------------------------------
# P2 #1: the warning rides the channel `doctor` shows
# ---------------------------------------------------------------------------

def test_fo_r3_the_warning_is_surfaced_by_validate_agent_models(tmp_path):
    config = _load(tmp_path, {"fo-r1-doctor": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"codex": {"variant": "max"}}}})

    shown = "\n".join(validate_agent_models(config))

    for needle in ("fo-r1-doctor", "codex", "variant"):
        assert needle in shown, f"doctor must show {needle!r}; saw: {shown!r}"


# ---------------------------------------------------------------------------
# P3: `provider` may not be set in an entry, and is reported
# ---------------------------------------------------------------------------

def test_fo_r3_the_provider_key_is_reported(tmp_path):
    config = _load(tmp_path, {"fo-r1-provider-key": {
        "provider": "opencode", "model": "opencode/x",
        "models": {"codex": {"model": "gpt-5", "provider": "claude"}}}})

    text = _all_text(config)

    for needle in ("fo-r1-provider-key", "codex", "provider"):
        assert needle in text, f"warning must name {needle!r}; saw: {text!r}"


def test_fo_r3_the_provider_key_does_not_move_the_run(tmp_path):
    spec = AgentSpec.from_dict("w", {
        "provider": "codex", "model": "m",
        "models": {"codex": {"model": "m2", "provider": "claude"}}})

    routed = spec.routed("codex")

    assert routed.provider == "codex", "an entry may not move the run"
    assert routed.model == "m2"
    # `fallback_for` must not hand `provider` back either: the start path
    # splats its overrides into `replace(provider=..., **overrides)`.
    assert "provider" not in spec.fallback_for("codex")[1]
