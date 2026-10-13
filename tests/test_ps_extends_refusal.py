"""PS2 / F133: unknown inheritance links refuse loading, never form families."""
import copy

import pytest
import yaml

from multiagents.paths import shipped_defaults_dir
from multiagents.providers import families, load_providers, resolve_inheritance


@pytest.mark.parametrize("surface", [load_providers, resolve_inheritance])
@pytest.mark.parametrize("depth", [0, 1, 4])
def test_ps2_missing_parent_at_any_depth_refuses_without_rewriting(surface, depth):
    raw = {"child": {"extends": "ghost", "bin": "sh", "env": {"KEEP": "value"}}}
    for level in reversed(range(depth)):
        name = f"ancestor-{level}"
        raw[name] = {"extends": raw["child"]["extends"]}
        raw["child"]["extends"] = name
    before = copy.deepcopy(raw)
    with pytest.raises(ValueError) as caught:
        surface(raw)
    assert str(caught.value) == "provider 'child': extends: no provider named 'ghost'"
    assert raw == before


def test_ps2_defined_disabled_parent_is_accepted_and_forms_real_family():
    providers = load_providers({"parent": {"bin": "sh", "enabled": False, "script": "parent.sh"}, "child": {"extends": "parent", "enabled": True}})
    assert not providers["parent"].enabled and providers["child"].enabled
    assert providers["child"].script_name == "parent.sh"
    assert providers["child"].name == "child"
    assert families(providers) == {"parent": ["child", "parent"]}


def test_ps2_shipped_providers_load_with_only_defined_inheritance_links():
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())["providers"]
    providers = load_providers(raw)
    assert set(providers) == set(raw)
    for name, provider in providers.items():
        assert provider.name == name
        assert not raw[name].get("extends") or raw[name]["extends"] in providers
