"""ST3: explicit variant precedence and provider configuration validation."""
import pytest

from multiagents.providers import load_providers


def provider(**fields):
    return load_providers({"review-cli": {
        "bin": "review-cli",
        "spawn": {
            "args": ["run", "{prompt}"],
            "optional": {"variant": ["--variant", "{variant}"]},
        },
        **fields,
    }})["review-cli"]


@pytest.mark.parametrize("configuration", [True, {}, {"high": "deep"}])
@pytest.mark.parametrize("effort", [None, "", "high", "future"])
def test_explicit_variant_wins_over_effort(configuration, effort):
    options = {"variant": "custom", "effort": effort}
    command = provider(effort_as_variant=configuration).build_command(
        prompt="work", model="model", workdir="/work", options=options)
    assert command == ["review-cli", "run", "work", "--variant", "custom"]
    assert options == {"variant": "custom", "effort": effort}


@pytest.mark.parametrize("configuration", [
    None, 0, 1, "true", "false", [], ["high"],
    {"": "deep"}, {"high": ""}, {1: "deep"}, {"high": 1},
    {"high": None}, {"high": True}, {"high": ["deep"]},
])
def test_invalid_effort_as_variant_is_rejected_at_load(configuration):
    with pytest.raises(ValueError, match="review-cli.*effort_as_variant"):
        provider(effort_as_variant=configuration)


@pytest.mark.parametrize("fields", [
    {}, {"effort_as_variant": False}, {"effort_as_variant": True},
    {"effort_as_variant": {}}, {"effort_as_variant": {"high": "deep"}},
])
def test_valid_effort_as_variant_loads(fields):
    configured = provider(**fields)
    command = configured.build_command(
        prompt="work", model="model", workdir="/work", options={})
    assert command == ["review-cli", "run", "work"]
