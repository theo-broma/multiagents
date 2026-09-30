"""H7 follow-up coverage for the PS-R1a amendments of 2026-09-30.

From `context/specs/h7-provider-startup.md`, "Amendments of 2026-09-30, after
the adversary (ag-bd9c41)" — the three tightenings the adversary listed as
untested:

- a directory, including an executable one, is not a binary;
- a relative explicit `bin` is refused at config load, never a ValueError
  raised later from `resolve_bin`;
- the docker host-mount derivation resolves with the provider's merged env
  PATH, the same env a native launch uses, not the server's PATH.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
from multiagents.executor.docker import DockerExecutor  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir  # noqa: E402
from multiagents.providers import Provider, load_providers  # noqa: E402


def _exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _provider(name: str, **fields) -> Provider:
    return Provider.from_dict(name, {"bin": name, "spawn": {"args": ["go"]}, **fields})


# A directory is not a binary, however permissive its mode. A PATH entry (or
# an explicit `bin`) that names one is skipped and the search carries on.

def test_executable_directory_on_path_is_skipped_and_search_carries_on(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    directory = first / "testcli"
    directory.mkdir(parents=True)
    directory.chmod(directory.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    binary = _exe(second / "testcli")
    found = _provider("p", bin="testcli").resolve_bin(env={"PATH": f"{first}:{second}"})
    assert found.path == binary.resolve(), (
        f"an executable directory named like the binary must be skipped, "
        f"not run; searched={found.searched}")
    assert found.via == "PATH"


def test_executable_directory_on_path_alone_is_not_found(tmp_path):
    first = tmp_path / "first"
    directory = first / "testcli"
    directory.mkdir(parents=True)
    directory.chmod(directory.stat().st_mode | stat.S_IXUSR)
    found = _provider("p", bin="testcli").resolve_bin(env={"PATH": str(first)})
    assert found.path is None and found.launcher is None


def test_explicit_bin_pointing_at_a_directory_fails_without_falling_through(tmp_path):
    binary = _exe(tmp_path / "elsewhere" / "testcli")
    directory = tmp_path / "dirbin" / "testcli"
    directory.mkdir(parents=True)
    directory.chmod(directory.stat().st_mode | stat.S_IXUSR)
    p = _provider("p", bin=str(directory), bin_search=[str(binary.parent)])
    found = p.resolve_bin(env={"PATH": str(binary.parent)})
    assert found.path is None
    # PS-R1: an explicit `bin` never falls through to PATH or bin_search.
    assert found.searched == [str(directory)], found.searched


# A relative explicit `bin` is refused where the config is loaded, like a
# relative `bin_search` entry — not by a ValueError from resolve_bin later,
# mid-operation.

@pytest.mark.parametrize("bin_value", ["./relative/testcli", "relative/testcli",
                                       "foo/../testcli"])
def test_relative_explicit_bin_is_refused_at_config_load(bin_value):
    with pytest.raises(ValueError, match="bin"):
        load_providers({"p": {"bin": bin_value, "spawn": {"args": ["go"]}}})


def test_relative_explicit_bin_never_raises_from_resolve_bin():
    # A Provider built around the load check (the dataclass directly, as
    # tests and harnesses build it) resolves to nothing instead of raising.
    p = Provider(name="p", bin="./relative/testcli", spawn={}, stream={})
    found = p.resolve_bin(env={"PATH": "/usr/bin:/bin"})
    assert found.path is None and found.launcher is None


# The docker host-mount derivation resolves against the provider's merged env
# — the ambient PATH with the instance's own `env:` applied, exactly what a
# native launch of that provider resolves against — not the server's PATH.

def test_docker_mount_derivation_uses_the_providers_merged_env_path(tmp_path, monkeypatch):
    releases = tmp_path / "opt" / "acme2" / "releases"
    binary = _exe(releases / "acme2")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")       # the server cannot see it
    p = Provider.from_dict("acme2", {
        "bin": "acme2", "spawn": {"args": ["go"]}, "bin_versions_depth": 1,
        "env": {"PATH": str(releases)}})
    ex = DockerExecutor({"image": "img", "network": "bridge"},
                        ProjectPaths(tmp_path), {"acme2": p}, global_config_dir())
    ex.inside = lambda: False
    mounts = dict(ex.mounts())
    assert mounts.get(releases) is True, (
        f"the versions root must be derived through the provider's own env "
        f"PATH; mounts={mounts}")
    assert binary.parent == releases
