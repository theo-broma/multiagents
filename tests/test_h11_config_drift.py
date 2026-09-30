"""H11 config-drift warning, contract context/specs/h11-config-drift.md (CD-R1..CD-R5).

Black box: `multiagents.drift.find_shadowing(paths=None)` and the `doctor`
output. The shipped-defaults location has no redirect (`shipped_defaults_dir()`
reads the package), so these tests compare temporary layers against the REAL
shipped files and derive every expected value from them at runtime.

Layers: the global dir is `MULTIAGENTS_CONFIG_DIR` (redirected per-test by
conftest); the project layer is `<root>/.multiagents/config/`. `paths` is
taken to be a `ProjectPaths` (or None for the global layer alone).
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir, shipped_defaults_dir  # noqa: E402

LIST_KEY = ("providers", "opencode", "spawn", "args")
LIST_PATH = ".".join(LIST_KEY)
SECOND_LIST = ("providers", "opencode", "stream", "rules")
SECOND_PATH = ".".join(SECOND_LIST)
MD = "agents/team/tester.md"


def _drift():
    from multiagents import drift
    return drift


def _shipped_yaml(name):
    return yaml.safe_load((shipped_defaults_dir() / name).read_text())


def _dig(data, key):
    for k in key:
        data = data[k]
    return data


def _nest(key, value):
    out = value
    for k in reversed(key):
        out = {k: out}
    return out


def _write_yaml(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def project(tmp_path):
    root = h.make_git_repo(tmp_path / "project")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    return paths


def _differing_list():
    return _dig(_shipped_yaml("providers.yaml"), LIST_KEY) + ["--extra-drift-arg"]


def _shadow_providers(layer_dir: Path, **extra):
    data = _nest(LIST_KEY, _differing_list())
    data.update(extra)
    _write_yaml(layer_dir / "providers.yaml", data)


def _find(paths=None):
    return _drift().find_shadowing(paths)


def _by_kind(items, kind):
    return [i for i in items if i.kind == kind]


def _file_endswith(item, name):
    return str(item.file).endswith(name)


def test_the_shipped_files_have_what_these_tests_assume():
    shipped = _shipped_yaml("providers.yaml")
    assert isinstance(_dig(shipped, LIST_KEY), list)
    assert isinstance(_dig(shipped, SECOND_LIST), list)
    assert (shipped_defaults_dir() / MD).is_file()


# ---- CD-R1 -----------------------------------------------------------------

def test_cd_r1_no_layers_no_drift(project):
    assert _find(project) == []
    assert _find() == []


def test_cd_r1_differing_list_in_global_layer_is_reported_with_exact_key_path():
    _shadow_providers(global_config_dir())
    items = _by_kind(_find(), "list_shadow")
    assert [i.key_path for i in items] == [LIST_PATH]
    assert _file_endswith(items[0], "providers.yaml")
    assert isinstance(items[0].detail, str) and items[0].detail


def test_cd_r1_differing_list_in_project_layer_is_reported(project):
    _shadow_providers(project.config)
    items = _by_kind(_find(project), "list_shadow")
    assert [i.key_path for i in items] == [LIST_PATH]
    assert str(project.config) in str(items[0].file)


def test_cd_r1_project_layer_is_not_consulted_without_paths(project):
    _shadow_providers(project.config)
    assert _find() == []


def test_cd_r1_same_drift_in_both_layers_is_reported_per_layer(project):
    _shadow_providers(global_config_dir())
    _shadow_providers(project.config)
    items = _by_kind(_find(project), "list_shadow")
    assert len(items) == 2
    assert len({str(i.file) for i in items}) == 2


def test_cd_r1_identical_list_is_not_reported():
    shipped = _dig(_shipped_yaml("providers.yaml"), LIST_KEY)
    _write_yaml(global_config_dir() / "providers.yaml",
                _nest(LIST_KEY, copy.deepcopy(shipped)))
    assert _find() == []


def test_cd_r1_a_full_copy_of_the_shipped_file_is_not_drift():
    layer = global_config_dir()
    layer.mkdir(parents=True)
    for name in ("providers.yaml", "agents.yaml", "project.yaml"):
        (layer / name).write_bytes((shipped_defaults_dir() / name).read_bytes())
    assert _find() == []


def test_cd_r1_differing_scalar_is_not_reported():
    shipped = _shipped_yaml("providers.yaml")
    assert isinstance(shipped["providers"]["opencode"]["bin"], str)
    _write_yaml(global_config_dir() / "providers.yaml",
                {"providers": {"opencode": {"bin": "my-own-opencode"}}})
    assert _find() == []


def test_cd_r1_mapping_that_differs_is_not_reported_as_a_list():
    _write_yaml(global_config_dir() / "providers.yaml",
                {"providers": {"opencode": {"spawn": {"resume": _dig(
                    _shipped_yaml("providers.yaml"),
                    ("providers", "opencode", "spawn", "resume"))}}}})
    _write_yaml(global_config_dir() / "project.yaml",
                {"limits": {"brand_new_key": 3}})
    assert _find() == []


def test_cd_r1_list_replaced_by_a_scalar_or_scalar_by_a_list_is_not_reported():
    # both values must be lists
    _write_yaml(global_config_dir() / "providers.yaml", _nest(LIST_KEY, "a string"))
    assert _find() == []
    _write_yaml(global_config_dir() / "providers.yaml",
                {"providers": {"opencode": {"bin": ["opencode"]}}})
    assert _find() == []


def test_cd_r1_key_absent_from_the_shipped_file_is_not_reported():
    _write_yaml(global_config_dir() / "providers.yaml",
                {"providers": {"opencode": {"my_own_list": ["a", "b"]},
                               "my_own_provider": {"spawn": {"args": ["x"]}}}})
    assert _find() == []


def test_cd_r1_empty_list_differing_from_a_nonempty_shipped_list_is_reported():
    _write_yaml(global_config_dir() / "providers.yaml", _nest(LIST_KEY, []))
    assert [i.key_path for i in _find()] == [LIST_PATH]


def test_cd_r1_two_differing_lists_are_two_items_with_their_own_key_paths():
    data = _nest(LIST_KEY, _differing_list())
    data["providers"]["opencode"]["stream"] = {"rules": []}
    _write_yaml(global_config_dir() / "providers.yaml", data)
    assert sorted(i.key_path for i in _find()) == sorted([LIST_PATH, SECOND_PATH])


def test_cd_r1_agents_yaml_is_compared():
    shipped = _shipped_yaml("agents.yaml")
    key = ("agents", "tester", "readonly_paths")
    value = _dig(shipped, key)
    assert isinstance(value, list)
    _write_yaml(global_config_dir() / "agents.yaml", _nest(key, value + ["zzz/**"]))
    items = _find()
    assert [i.key_path for i in items] == [".".join(key)]
    assert _file_endswith(items[0], "agents.yaml")


def test_cd_r1_project_yaml_is_compared(project):
    shipped = _shipped_yaml("project.yaml")
    key = ("security", "env_passthrough")
    value = _dig(shipped, key)
    assert isinstance(value, list), f"no shipped list found at {key}"
    _write_yaml(project.config / "project.yaml", _nest(key, value + ["ZZ_EXTRA"]))
    items = _find(project)
    assert [i.key_path for i in items] == [".".join(key)]
    assert _file_endswith(items[0], "project.yaml")


def test_cd_r1_find_shadowing_is_read_only_and_repeatable(project):
    _shadow_providers(project.config)
    before = (project.config / "providers.yaml").read_text()
    first = [(str(i.file), i.key_path, i.kind) for i in _find(project)]
    second = [(str(i.file), i.key_path, i.kind) for i in _find(project)]
    assert first == second and first
    assert (project.config / "providers.yaml").read_text() == before


# ---- CD-R2 -----------------------------------------------------------------

def _copy_md(layer_dir: Path, text=None):
    target = layer_dir / MD
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text is not None
                      else (shipped_defaults_dir() / MD).read_text() + "\nlocal edit\n")
    return target


def test_cd_r2_differing_copy_is_reported():
    _copy_md(global_config_dir())
    items = _by_kind(_find(), "file_shadow")
    assert len(items) == 1
    assert _file_endswith(items[0], MD)
    assert items[0].detail


def test_cd_r2_differing_copy_in_project_layer_is_reported(project):
    _copy_md(project.config)
    items = _by_kind(_find(project), "file_shadow")
    assert len(items) == 1 and str(project.config) in str(items[0].file)


def test_cd_r2_identical_copy_is_not_reported():
    _copy_md(global_config_dir(), (shipped_defaults_dir() / MD).read_text())
    assert _find() == []


def test_cd_r2_a_file_with_no_shipped_counterpart_is_not_reported():
    _copy_md(global_config_dir(), "mine\n")  # placeholder to create dir
    (global_config_dir() / MD).unlink()
    own = global_config_dir() / "agents/team/my-own-agent.md"
    own.write_text("mine\n")
    assert _find() == []


def test_cd_r2_older_copy_says_the_shipped_version_is_newer():
    shipped = shipped_defaults_dir() / MD
    old = _copy_md(global_config_dir())
    t = shipped.stat().st_mtime - 86400
    os.utime(old, (t, t))
    [item] = _by_kind(_find(), "file_shadow")
    assert "newer" in item.detail.lower()


def test_cd_r2_a_copy_newer_than_shipped_does_not_claim_shipped_is_newer():
    shipped = shipped_defaults_dir() / MD
    fresh = _copy_md(global_config_dir())
    t = shipped.stat().st_mtime + 86400
    os.utime(fresh, (t, t))
    [item] = _by_kind(_find(), "file_shadow")
    assert "newer" not in item.detail.lower()


# ---- CD-R3 -----------------------------------------------------------------

def test_cd_r3_acknowledged_list_shadow_is_silent():
    _shadow_providers(global_config_dir(), drift_acknowledged=[LIST_PATH])
    assert _find() == []


def test_cd_r3_an_acknowledgement_silences_exactly_that_item():
    data = _nest(LIST_KEY, _differing_list())
    data["providers"]["opencode"]["stream"] = {"rules": []}
    data["drift_acknowledged"] = [LIST_PATH]
    _write_yaml(global_config_dir() / "providers.yaml", data)
    assert [i.key_path for i in _find()] == [SECOND_PATH]


def test_cd_r3_acknowledged_project_layer_shadow_is_silent(project):
    _shadow_providers(project.config, drift_acknowledged=[LIST_PATH])
    assert _find(project) == []


def test_cd_r3_acknowledged_md_file_is_silent_via_agents_yaml():
    _copy_md(global_config_dir())
    _write_yaml(global_config_dir() / "agents.yaml", {"drift_acknowledged": [MD]})
    assert _find() == []


def test_cd_r3_md_acknowledgement_in_the_wrong_file_does_not_silence_it():
    _copy_md(global_config_dir())
    _write_yaml(global_config_dir() / "providers.yaml", {"drift_acknowledged": [MD]})
    kinds = sorted(i.kind for i in _find())
    assert "file_shadow" in kinds


def test_cd_r3_stale_acknowledgement_is_reported():
    _write_yaml(global_config_dir() / "providers.yaml",
                {"drift_acknowledged": ["providers.opencode.no.such.key"]})
    [item] = _find()
    assert item.kind == "stale_acknowledgement"
    assert _file_endswith(item, "providers.yaml")
    assert "providers.opencode.no.such.key" in f"{item.key_path} {item.detail}"


def test_cd_r3_acknowledgement_of_an_identical_list_is_stale():
    shipped = _dig(_shipped_yaml("providers.yaml"), LIST_KEY)
    data = _nest(LIST_KEY, copy.deepcopy(shipped))
    data["drift_acknowledged"] = [LIST_PATH]
    _write_yaml(global_config_dir() / "providers.yaml", data)
    assert [i.kind for i in _find()] == ["stale_acknowledgement"]


def test_cd_r3_stale_md_acknowledgement_is_reported():
    _write_yaml(global_config_dir() / "agents.yaml",
                {"drift_acknowledged": [MD]})  # no copy exists
    assert [i.kind for i in _find()] == ["stale_acknowledgement"]


def test_cd_r3_live_and_stale_entries_together():
    data = _nest(LIST_KEY, _differing_list())
    data["drift_acknowledged"] = [LIST_PATH, "providers.gone.list"]
    _write_yaml(global_config_dir() / "providers.yaml", data)
    assert [i.kind for i in _find()] == ["stale_acknowledgement"]


def test_cd_r3_empty_acknowledgement_list_changes_nothing():
    _shadow_providers(global_config_dir(), drift_acknowledged=[])
    assert [i.kind for i in _find()] == ["list_shadow"]


def test_cd_r3_the_acknowledgement_key_itself_is_never_drift():
    _write_yaml(global_config_dir() / "providers.yaml",
                {"drift_acknowledged": []})
    assert _find() == []


# ---- CD-R4 -----------------------------------------------------------------

def _doctor(root, capsys, monkeypatch):
    """Run `doctor` with a one-provider config so it does not shell out to the
    real agent CLIs (that takes over a minute). Drift is read from the layer
    files, not from the merged config, so the stub does not touch what is tested."""
    import multiagents.cli as cli
    config = h.make_config(providers={"p": {"bin": "no-such-cli-h11",
                                            "spawn": {"args": ["go"]}}})
    monkeypatch.setattr(cli, "load_config", lambda _paths: config)
    capsys.readouterr()
    code = cli.cmd_doctor(argparse.Namespace(path=str(root), clear=None, force=False))
    return code, capsys.readouterr().out


def _section(out):
    assert "config drift" in out, out
    return out.split("config drift", 1)[1]


def test_cd_r4_doctor_with_no_drift_prints_none(project, capsys, monkeypatch):
    _, out = _doctor(project.root, capsys, monkeypatch)
    assert "none" in _section(out).split("\n\n")[0].lower()


def test_cd_r4_doctor_lists_the_item_and_the_fix(project, capsys, monkeypatch):
    _shadow_providers(project.config)
    _, out = _doctor(project.root, capsys, monkeypatch)
    body = _section(out)
    assert LIST_PATH in body
    assert "providers.yaml" in body
    assert "drift_acknowledged" in body
    assert "remove" in body.lower()
    assert "none" not in body.split("\n\n")[0].lower()


def test_cd_r4_doctor_lists_a_file_shadow(project, capsys, monkeypatch):
    _copy_md(project.config)
    _, out = _doctor(project.root, capsys, monkeypatch)
    assert MD in _section(out)


def test_cd_r4_drift_never_changes_doctors_exit_status(project, capsys, monkeypatch):
    clean_code, _ = _doctor(project.root, capsys, monkeypatch)
    _shadow_providers(project.config)
    _copy_md(project.config)
    _write_yaml(project.config / "agents.yaml", {"drift_acknowledged": ["gone.key"]})
    drift_code, out = _doctor(project.root, capsys, monkeypatch)
    assert LIST_PATH in _section(out)
    assert drift_code == clean_code


# The MCP-server startup line (one line, naming `multiagents doctor`) is NOT
# tested: the count is logged from the lazily built runner in
# server._runner_locked, and there is no public seam to trigger and capture it
# without private module state or a live stdio session. See the result note.


# ---- CD-R5 -----------------------------------------------------------------

def test_cd_r5_layer_merging_is_unchanged_a_shadowing_list_still_replaces(project):
    from multiagents.config import load
    _shadow_providers(project.config)
    cfg = load(project)
    assert _dig(cfg.providers, ("providers", "opencode", "spawn", "args")
                if "providers" in cfg.providers else LIST_KEY[1:]) == _differing_list()


def test_cd_r5_detection_does_not_modify_any_layer_file(project):
    _shadow_providers(global_config_dir())
    _copy_md(project.config)
    def snap():
        return {str(p): p.read_bytes() for d in (global_config_dir(), project.config)
                for p in d.rglob("*") if p.is_file()}
    before = snap()
    _find(project)
    assert snap() == before
