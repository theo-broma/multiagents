"""HF2.1–HF2.3 / F30: contradictions are refused across the full mount list."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h
from st_docker import DockerWorld


def assert_refused(ex, path, readonly_entry):
    problems = ex.configuration_problems()
    matching = [message for message in problems if str(path) in message]
    assert matching, f"no collision diagnostic for {path}: {problems}"
    message = " ".join(matching).lower().replace("_", "-")
    assert re.search(r"read[- ]?only", message)
    labels = readonly_entry.split("|")
    if "extra" in labels:
        labels.extend(["user", "configured"])
    assert any(label in message for label in labels), (
        f"diagnostic must identify read-only entry: {message}")
    with pytest.raises(ValueError, match=re.escape(str(path))):
        ex.mounts()


@pytest.mark.parametrize("kind", ["worktrees", "homes", "data", "git-objects"])
def test_hf2_1_extra_read_only_refuses_a_writable_builtin(tmp_path, kind):
    world_path = tmp_path / "project"
    world_path.mkdir()
    ex = h.make_docker_executor(world_path, mount_cli_from_host=False)
    path = {"worktrees": ex.paths.worktrees, "homes": ex.paths.homes,
            "data": ex.paths.data, "git-objects": world_path / ".git" / "objects"}[kind]
    path.mkdir(parents=True, exist_ok=True)
    ex.config["extra_mounts"] = [{"path": str(path), "read_only": True}]
    assert_refused(ex, path, "extra")


@pytest.mark.parametrize("first_read_only", [False, True])
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "existing"])
def test_hf2_1_duplicate_contradictions_are_refused_in_either_order(
        tmp_path, first_read_only, exists):
    path = tmp_path / "duplicate"
    if exists:
        path.mkdir()
    ex = h.make_docker_executor(tmp_path, extra_mounts=[
        {"path": str(path), "read_only": first_read_only},
        {"path": str(path), "read_only": not first_read_only},
    ])
    assert_refused(ex, path, "extra")


def final_mount_case(tmp_path, kind, *, exists=True):
    """Declare provider inputs through the same executor builder as C1."""
    provider = SimpleNamespace(name="hf2", enabled=True, available=lambda: None,
                               home_links=[], container_private_home=[], transcript={},
                               adapter="")
    readonly = False
    if kind == "provider-home":
        path = Path.home() / ".hf2-linked"
        provider.home_links = [".hf2-linked"]
    elif kind == "private-state":
        path = Path.home() / ".hf2-private"
        provider.container_private_home = [".hf2-private"]
    elif kind == "transcript":
        path = Path.home() / ".hf2-sessions"
        provider.transcript = {"dir": str(path / "{slug}"), "glob": "*.jsonl"}
    elif kind == "adapter":
        path = tmp_path / "providers" / "hf2-adapter.sh"
        path.parent.mkdir()
        path.write_text("#!/bin/sh\n")
        provider.adapter = "hf2-adapter.sh"
        readonly = True
    else:
        path = tmp_path / "hf2-cli"
        if exists:
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        provider.available = lambda: str(path)
        readonly = True
    if exists and kind in {"provider-home", "private-state", "transcript"}:
        path.mkdir()
    ex = h.make_docker_executor(tmp_path, providers={"hf2": provider})
    return ex, path, readonly


@pytest.mark.parametrize("kind,exists", [
    (kind, exists) for kind in ("cli", "provider-home", "private-state", "transcript", "adapter")
    for exists in (False, True) if kind != "adapter" or exists
])
def test_hf2_1a_collisions_include_final_provider_mounts(tmp_path, kind, exists):
    ex, path, readonly = final_mount_case(tmp_path, kind, exists=exists)
    ex.config["extra_mounts"] = [{"path": str(path), "read_only": not readonly}]
    assert_refused(ex, path, "extra" if not readonly else "hf2|cli|binary|adapter")


def test_hf2_1a_comparison_expands_tilde(tmp_path):
    ex, path, _ = final_mount_case(tmp_path, "provider-home")
    ex.config["extra_mounts"] = [{"path": "~/.hf2-linked", "read_only": True}]
    assert_refused(ex, path, "extra")


def test_hf2_1a_parent_child_overlap_is_valid(tmp_path):
    ex, path, _ = final_mount_case(tmp_path, "provider-home")
    child = path / "readonly-child"
    child.mkdir()
    ex.config["extra_mounts"] = [{"path": str(child), "read_only": True}]
    assert ex.configuration_problems() == []
    assert (path, False) in ex.mounts()
    assert (child, True) in ex.mounts()


def test_hf2_1a_comparison_is_lexical_not_resolved(tmp_path):
    target = tmp_path / "directory"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    ex = h.make_docker_executor(tmp_path, extra_mounts=[
        {"path": str(target), "read_only": True}, str(alias)])
    assert ex.configuration_problems() == []
    assert (target, True) in ex.mounts()
    assert (alias, False) in ex.mounts()


def test_hf2_1a_configuration_check_is_pure_and_does_not_recurse(tmp_path, monkeypatch):
    world = DockerWorld(tmp_path, monkeypatch)
    ex = world.executor()
    ex.providers = {"hf2": SimpleNamespace(
        name="hf2", available=lambda: None, home_links=[],
        container_private_home=[".hf2-private"],
        transcript={"dir": "~/.hf2-sessions/{slug}"})}
    path = Path.home() / ".hf2-private"
    ex.config["extra_mounts"] = [{"path": str(path), "read_only": True}]

    def forbidden_mounts():
        pytest.fail("configuration_problems must not recurse into mounts")

    monkeypatch.setattr(ex, "mounts", forbidden_mounts)
    roots = [world.base, Path.home(), h.ProjectPaths(world.root).worktrees.parent.parent]
    before = {root: sorted(str(p) for p in root.rglob("*")) for root in roots}
    problems = ex.configuration_problems()
    assert world.calls() == [], "configuration check must not invoke Docker"
    assert {root: sorted(str(p) for p in root.rglob("*")) for root in roots} == before
    assert any(str(path) in message for message in problems)


@pytest.mark.parametrize("readonly", [False, True])
def test_hf2_2_identical_extra_entries_are_deduplicated(tmp_path, readonly):
    path = tmp_path / "duplicate"
    path.mkdir()
    entry = {"path": str(path), "read_only": readonly}
    ex = h.make_docker_executor(tmp_path, extra_mounts=[entry.copy(), entry.copy()])
    assert ex.configuration_problems() == []
    assert [(p, ro) for p, ro in ex.mounts() if p == path] == [(path, readonly)]


@pytest.mark.parametrize("kind", ["cli", "provider-home", "private-state", "transcript", "adapter"])
def test_hf2_2_agreeing_extra_and_final_mount_are_deduplicated(tmp_path, kind):
    ex, path, readonly = final_mount_case(tmp_path, kind)
    ex.config["extra_mounts"] = [{"path": str(path), "read_only": readonly}]
    assert ex.configuration_problems() == []
    assert [(p, ro) for p, ro in ex.mounts() if p == path] == [(path, readonly)]


def test_hf2_3_builtin_collisions_without_extra_mounts_remain_valid(tmp_path):
    # config_dir deliberately equals the root, just as in the C1 builder.
    ex = h.make_docker_executor(tmp_path, mount_cli_from_host=False)
    assert ex.configuration_problems() == []
    assert [(p, ro) for p, ro in ex.mounts() if p == tmp_path] == [(tmp_path, True)]
