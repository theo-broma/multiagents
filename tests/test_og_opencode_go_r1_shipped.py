"""Black-box contract for OG-R1 of context/specs/opencode-go-rename.md.

OG-R1  the shipped providers: `opencode` is the CLI base only (no route, no
       models_include, no budget, no family of its own); `opencode-go` is the
       Go subscription (extends opencode, family opencode-go, models_include
       ["opencode-go/*"], ships enabled); zen / zai / deepinfra keep extending
       `opencode` and nothing else changes for them.

Read from the shipped files and the loaded config, never from a private name.
The base's own settings are asserted on the RAW shipped block (a key that is
absent) because the resolved object may default `family` to the block's name.
"""
from __future__ import annotations

import copy
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import og_support as og  # noqa: E402
from og_support import NEW, OLD, Project, deprecations, load  # noqa: E402

from multiagents.paths import shipped_defaults_dir  # noqa: E402

DEFAULTS = shipped_defaults_dir()
CHILDREN = {"opencode-zen": "opencode/*", "opencode-zai": "zai-coding-plan/*",
            "opencode-deepinfra": "deepinfra/*"}
ROUTE_KEYS = {"models_include", "models_exclude", "family", "spend_cap", "max_concurrent",
              "billing", "budget_profile_env", "models_static"}


def _raw() -> dict:
    return yaml.safe_load((DEFAULTS / "providers.yaml").read_text())["providers"]


def _load(raw):
    from multiagents.providers import load_providers
    return load_providers(copy.deepcopy(raw))


def test_og_r1_opencode_go_is_the_go_subscription():
    go = _load(_raw()).get(NEW)
    assert go is not None, "no shipped `opencode-go` provider"
    assert go.extends == OLD
    assert go.family == NEW
    assert list(go.models_include) == ["opencode-go/*"]
    assert go.enabled is True


def test_og_r1_opencode_go_inherits_the_cli_base_unchanged():
    ps = _load(_raw())
    go, base = ps[NEW], ps[OLD]
    assert go.spawn == base.spawn and go.stream == base.stream
    assert go.auth == base.auth and go.bin == base.bin
    assert go.usage_mode == base.usage_mode and go.models_cmd == base.models_cmd


def test_og_r1_the_base_has_no_route_settings_of_its_own():
    block = _raw()[OLD]
    assert not (ROUTE_KEYS & set(block)), (
        f"the CLI base must not carry route settings: {sorted(ROUTE_KEYS & set(block))}")
    assert list(_load(_raw())[OLD].models_include) == []


@pytest.mark.parametrize("child, namespace", sorted(CHILDREN.items()))
def test_og_r1_zen_zai_deepinfra_keep_extending_the_base(child, namespace):
    raw = _raw()
    assert raw[child]["extends"] == OLD
    p = _load(raw)[child]
    assert p.extends == OLD and p.family == child
    assert list(p.models_include) == [namespace]
    assert p.enabled is False                      # all three ship disabled


def test_og_r1_plan_variables_of_the_children_are_unchanged():
    ps = _load(_raw())
    assert ps["opencode-zen"].env["MULTIAGENTS_OPENCODE_PLAN"] == "zen"
    assert ps["opencode-zai"].env["MULTIAGENTS_OPENCODE_PLAN"] == "zai-coding-plan"
    assert ps["opencode-deepinfra"].env["MULTIAGENTS_OPENCODE_PLAN"] == "deepinfra"
    assert "MULTIAGENTS_OPENCODE_PLAN" not in (ps[NEW].env or {})
    assert ps["opencode-deepinfra"].billing == "metered"


def test_og_r1_families_of_the_shipped_set():
    families = {n: p.family for n, p in _load(_raw()).items() if n.startswith("opencode-")}
    assert families == {NEW: NEW, "opencode-zen": "opencode-zen",
                        "opencode-zai": "opencode-zai",
                        "opencode-deepinfra": "opencode-deepinfra"}


def test_og_r1_no_shipped_agent_names_the_base_as_a_route():
    roster = yaml.safe_load((DEFAULTS / "agents.yaml").read_text())["agents"]
    for name, spec in roster.items():
        assert (spec or {}).get("provider") != OLD, f"agent {name} still on `{OLD}`"
        assert OLD not in ((spec or {}).get("models") or {}), (
            f"agent {name}: `{OLD}` in its models: chain")
    on_go = [n for n, s in roster.items() if (s or {}).get("provider") == NEW]
    assert on_go, "no shipped agent runs on opencode-go any more"


def test_og_r1_the_shipped_fallback_chain_selects_opencode_go_not_the_base():
    project = yaml.safe_load((DEFAULTS / "project.yaml").read_text())
    chain = (project.get("budget") or {}).get("fallback_chain") or []
    assert OLD not in chain and NEW in chain, chain


def test_og_r1_shipped_config_loads_with_no_deprecation_warning(tmp_path, capsys, caplog):
    cfg, text = load(Project(tmp_path), capsys, caplog)
    assert deprecations(text) == [], text
    for name, spec in cfg.agents.items():
        assert spec.provider != OLD, name
        assert OLD not in spec.models, name


def test_og_r1_refresh_models_lists_only_go_ids_under_opencode_go_and_nothing_under_the_base(
        tmp_path, monkeypatch):
    from multiagents.models import refresh_models
    raw = _raw()
    raw = {k: raw[k] for k in (OLD, NEW, "opencode-zen")}
    raw["opencode-zen"]["enabled"] = True
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text("#!/bin/sh\n[ \"$1\" = models ] && printf '%s\\n' "
                    "opencode/big-pickle opencode-go/glm-5.1 opencode-go/kimi-k2 "
                    "deepinfra/other\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "models.yaml"
    refresh_models(_load(raw), target, config_dir=tmp_path / "cfg")
    models = yaml.safe_load(target.read_text())["models"]
    ids = lambda n: sorted(m["id"] for m in models.get(n) or [])   # noqa: E731
    assert ids(NEW) == ["opencode-go/glm-5.1", "opencode-go/kimi-k2"], models
    assert ids("opencode-zen") == ["opencode/big-pickle"], models
    assert OLD not in models, f"the CLI base is not a route and lists no models: {models}"
