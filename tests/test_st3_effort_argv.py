"""ST3-R1/R2/R3: effort transport is declared by provider configuration.

Contract: stabilization.md at 6d9f23de, with the effort_as_variant schema
specified by Decision 8 at 0f71fbc9. Assert only the rendered public argv.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from multiagents.paths import shipped_defaults_dir
from multiagents.providers import load_providers


def providers():
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return load_providers(raw["providers"])


def argv(provider, effort):
    return provider.build_command(
        prompt="work", model="fixture-model", workdir="/fixture/work",
        options={} if effort is None else {"effort": effort})


def configured_provider(**fields):
    # Use a third-party provider and its own variant flag so neither a
    # provider name nor opencode's spelling can substitute for configuration.
    raw = {
        "fixture-independent-cli": {
            "bin": "fixture-cli",
            "spawn": {
                "args": ["run", "{prompt}"],
                "optional": {"variant": ["--reasoning-mode", "{variant}"]},
            },
            **fields,
        }
    }
    return load_providers(yaml.safe_load(yaml.safe_dump(raw)))["fixture-independent-cli"]


@pytest.mark.parametrize("name", ["opencode-zai", "opencode-go"])
@pytest.mark.parametrize("effort", ["high", "low", None, ""])
def test_st3_r1_shipped_opencode_effort_reaches_variant(name, effort):
    command = argv(providers()[name], effort)
    if effort:
        assert "--variant" in command, command
        index = command.index("--variant")
        assert command[index + 1] == effort, command
        assert command.count("--variant") == 1, command
    else:
        assert "--variant" not in command, command
    assert "--effort" not in command, command


@pytest.mark.parametrize("effort", ["high", "low", None])
def test_st3_r1_the_configuration_works_under_an_unrelated_provider_name(effort):
    # A loaded public configuration can be reused by a differently named
    # integration. Provider-name special cases would break this identical argv.
    configured = copy.deepcopy(providers()["opencode-go"])
    configured.name = "fixture-independent-cli"
    command = argv(configured, effort)
    if effort:
        assert "--variant" in command, command
        assert command[command.index("--variant") + 1] == effort, command
    else:
        assert "--variant" not in command, command


@pytest.mark.parametrize("effort, expected", [
    ("high", "deep-reasoning"),
    ("low", "quick-response"),
    ("medium", "medium"),
    ("future-effort", "future-effort"),
])
def test_st3_r2_configured_mapping_applies_and_unmapped_effort_passes_through(effort, expected):
    provider = configured_provider(effort_as_variant={
        "high": "deep-reasoning", "low": "quick-response"})
    command = argv(provider, effort)
    assert command == ["fixture-cli", "run", "work", "--reasoning-mode", expected], command


@pytest.mark.parametrize("configuration", [True, {}], ids=["true", "empty-mapping"])
@pytest.mark.parametrize("effort", ["high", "future-effort"])
def test_st3_r2_true_and_empty_mapping_pass_effort_through(configuration, effort):
    command = argv(configured_provider(effort_as_variant=configuration), effort)
    assert command == ["fixture-cli", "run", "work", "--reasoning-mode", effort], command


@pytest.mark.parametrize("configuration", [True, {"high": "deep-reasoning"}])
@pytest.mark.parametrize("effort", [None, ""])
def test_st3_r1_configured_mapping_without_effort_omits_variant(configuration, effort):
    command = argv(configured_provider(effort_as_variant=configuration), effort)
    assert command == ["fixture-cli", "run", "work"], command


def test_st3_r1_absent_mapping_keeps_effort_and_explicit_variant_independent():
    provider = configured_provider()
    assert argv(provider, "high") == ["fixture-cli", "run", "work"]
    command = provider.build_command(
        prompt="work", model="fixture-model", workdir="/fixture/work",
        options={"effort": "high", "variant": "explicit-mode"})
    assert command == ["fixture-cli", "run", "work", "--reasoning-mode", "explicit-mode"], command


@pytest.mark.parametrize("name", ["codex", "claude"])
@pytest.mark.parametrize("effort", ["high", "low", None, ""])
def test_st3_r3_codex_and_claude_keep_their_effort_flag(name, effort):
    command = argv(providers()[name], effort)
    if effort:
        assert "--effort" in command, command
        assert command[command.index("--effort") + 1] == effort, command
        assert command.count("--effort") == 1, command
    else:
        assert "--effort" not in command, command
    assert "--variant" not in command, command
