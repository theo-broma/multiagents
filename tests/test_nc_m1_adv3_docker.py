"""Adversary round 3 (M1): NC-R3 docker mount refusals that survived mutation."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import nc, off  # noqa: E402,F401
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir, state_root  # noqa: E402


def _executor(sched, extra_mounts=None):
    from multiagents.executor.docker import DockerExecutor
    from multiagents.providers import load_providers
    docker = {"extra_mounts": extra_mounts} if extra_mounts is not None else {}
    sched.p.project["executor"] = {"kind": "docker", "docker": docker}
    sched.p.write()
    paths = ProjectPaths(sched.root)
    cfg = config_mod.load(paths)
    return DockerExecutor(cfg.project["executor"]["docker"], paths,
                          load_providers(cfg.providers), global_config_dir())


def test_adv3_gate_off_an_existing_scheduler_dir_is_still_never_mounted(off):
    off.state_dir.mkdir(parents=True)
    executor = _executor(off, [{"path": str(off.state_dir)}])
    with pytest.raises(ValueError):
        executor.mounts()


def test_adv3_a_writable_extra_mount_of_the_transport_dir_by_another_name_is_refused(nc, tmp_path):
    nc.rpc_dir.mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "rpc-alias"
    alias.symlink_to(nc.rpc_dir)
    executor = _executor(nc, [{"path": str(alias), "read_only": False}])
    with pytest.raises(ValueError):
        executor.mounts()


def test_adv3_a_mount_of_every_projects_transport_dirs_is_refused(nc):
    parent = state_root() / "scheduler-rpc"
    parent.mkdir(parents=True, exist_ok=True)
    executor = _executor(nc, [{"path": str(parent), "read_only": True}])
    with pytest.raises(ValueError):
        executor.mounts()


def test_adv3_a_permissive_transport_dir_is_made_private_before_mounting(nc):
    nc.rpc_dir.mkdir(parents=True)
    nc.rpc_dir.chmod(0o755)
    _executor(nc).mounts()
    assert stat.S_IMODE(nc.rpc_dir.stat().st_mode) == 0o700
