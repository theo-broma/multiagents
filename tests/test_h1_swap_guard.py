"""The host's worktree deletion stays beneath its open directory descriptors."""

from pathlib import Path

import pytest

from multiagents.authority import HostAuthority
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths, state_root
from multiagents.tree import Tree


def test_symlink_swap_after_validation_cannot_redirect_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    authority = HostAuthority(paths, Tree(paths.tree_file, paths.events_file))
    nested = paths.worktree("ag-nested")
    nested.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("keep")

    assert authority.safe_nested_path(nested)
    nested.rmdir()
    nested.symlink_to(outside, target_is_directory=True)

    assert not authority.remove_worktree(nested)
    assert marker.read_text() == "keep"


def test_docker_extra_mount_cannot_expose_host_record(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    executor = DockerExecutor({"image": "unused", "mount_cli_from_host": False,
                               "extra_mounts": [str(state_root())]},
                              paths, {}, state_root())
    with pytest.raises(ValueError, match="host authority"):
        executor.mounts()
