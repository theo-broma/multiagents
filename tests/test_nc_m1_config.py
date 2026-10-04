"""NC-R1, NC-R2, NC-R3 (milestone M1): the gate, the configuration keys, and
where the scheduler's state may and may not be visible.

Black box: the shipped defaults file, the merged configuration, `doctor`'s
output, the docker executor's mount list and the files the scheduler leaves on
disk. Nothing of `multiagents.scheduler` is imported except the NC-R71 seams.

Assumptions (also listed in the run report):
  * `scheduler start` with the gate off must leave no socket and no state, and
    its exit code is not asserted.
  * The docker executor reads the gate from the project's merged config, so it
    is built here from `config.load(paths)` exactly as `cli._docker_executor`
    does.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from nc_harness import Sched, files_under, live, nc, off  # noqa: E402,F401

from multiagents import cli, config as config_mod, manifest, server  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir, shipped_defaults_dir, state_root  # noqa: E402

DEFAULTS = {
    "enabled": False,
    "timezone": "Europe/Paris",
    "starvation_after_seconds": 7200,
    "window_tolerance_seconds": 60,
    "admission_timeout_seconds": 10,
    "tick_seconds": 5,
}


# ------------------------------------------------------------------ layering

def layered(tmp_path, monkeypatch, project_yaml=None, global_yaml=None):
    root = h.make_git_repo(tmp_path / "proj")
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    gdir = tmp_path / "gconf"
    gdir.mkdir(exist_ok=True)
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    if project_yaml is not None:
        (paths.config / "project.yaml").write_text(project_yaml)
    if global_yaml is not None:
        (gdir / "project.yaml").write_text(global_yaml)
    return paths, gdir


def test_nc_r2_shipped_defaults_document_every_key_with_its_default():
    shipped = yaml.safe_load((shipped_defaults_dir() / "project.yaml").read_text())
    assert shipped.get("scheduler") == DEFAULTS


def test_nc_r2_with_nothing_configured_the_defaults_apply(tmp_path, monkeypatch):
    paths, _ = layered(tmp_path, monkeypatch)
    assert config_mod.load(paths, seed=False).project.get("scheduler") == DEFAULTS


def test_nc_r2_global_overrides_shipped(tmp_path, monkeypatch):
    paths, _ = layered(tmp_path, monkeypatch,
                       global_yaml="scheduler:\n  timezone: America/New_York\n  tick_seconds: 2\n")
    got = config_mod.load(paths, seed=False).project["scheduler"]
    assert got == {**DEFAULTS, "timezone": "America/New_York", "tick_seconds": 2}


def test_nc_r2_project_overrides_global_and_the_keys_layer_independently(tmp_path, monkeypatch):
    paths, _ = layered(
        tmp_path, monkeypatch,
        global_yaml=("scheduler:\n  timezone: America/New_York\n"
                     "  starvation_after_seconds: 100\n  window_tolerance_seconds: 5\n"),
        project_yaml="scheduler:\n  timezone: Asia/Tokyo\n  window_tolerance_seconds: 9\n")
    got = config_mod.load(paths, seed=False).project["scheduler"]
    assert got["timezone"] == "Asia/Tokyo"                 # project beats global
    assert got["window_tolerance_seconds"] == 9
    assert got["starvation_after_seconds"] == 100          # only global sets it
    assert got["tick_seconds"] == 5                        # only shipped sets it
    assert got["enabled"] is False


def test_nc_r2_the_gate_follows_the_same_precedence_global_on_project_off(tmp_path, monkeypatch):
    paths, _ = layered(tmp_path, monkeypatch,
                       global_yaml="scheduler:\n  enabled: true\n",
                       project_yaml="scheduler:\n  enabled: false\n")
    assert config_mod.load(paths, seed=False).project["scheduler"]["enabled"] is False
    paths2, _ = layered(tmp_path / "second", monkeypatch,
                        global_yaml="scheduler:\n  enabled: true\n")
    assert config_mod.load(paths2, seed=False).project["scheduler"]["enabled"] is True


# -------------------------------------------------------------------- doctor

@pytest.fixture
def doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *a: {})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *a: {})
    monkeypatch.setattr(cli, "read_all", lambda *a: {})
    monkeypatch.setattr(cli, "_report_agents", lambda *a: 0)
    monkeypatch.setattr(cli, "find_shadowing", lambda *a: [])
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *a: 0)
    counter = {"n": 0}

    def run(project_yaml=None, global_yaml=None):
        counter["n"] += 1
        paths, _ = layered(tmp_path / f"doc{counter['n']}", monkeypatch,
                           project_yaml=project_yaml, global_yaml=global_yaml)
        capsys.readouterr()
        code = cli.cmd_doctor(argparse.Namespace(path=str(paths.root), clear=None, force=False))
        out = capsys.readouterr().out
        match = re.search(r"^(\d+) problem\(s\)$", out, re.M)
        return code, int(match.group(1)) if match else 0, out

    return run


INVALID = [
    ("timezone", "Mars/Base"),
    ("timezone", "''"),
    ("timezone", "5"),
    ("starvation_after_seconds", "0"),
    ("starvation_after_seconds", "-5"),
    ("starvation_after_seconds", "soon"),
    ("window_tolerance_seconds", "0"),
    ("window_tolerance_seconds", "-1"),
    ("admission_timeout_seconds", "0"),
    ("admission_timeout_seconds", "-10"),
    ("tick_seconds", "0"),
    ("tick_seconds", "-2"),
    ("tick_seconds", "often"),
]


@pytest.mark.parametrize("key,value", INVALID)
def test_nc_r2_doctor_reports_an_invalid_value_as_one_problem_with_file_and_line(
        doctor, key, value):
    _, base, _ = doctor("scheduler:\n  enabled: false\n")
    text = f"scheduler:\n  enabled: false\n  {key}: {value}\n"
    code, problems, out = doctor(text)
    assert problems == base + 1, out
    assert code == 1
    assert re.search(r"project\.yaml:3\b", out), out       # the line of the offending key
    assert key in out


def test_nc_r2_an_invalid_value_is_a_problem_with_the_gate_on_too(doctor):
    _, base, _ = doctor("scheduler:\n  enabled: true\n")
    _, problems, out = doctor("scheduler:\n  enabled: true\n  timezone: Mars/Base\n")
    assert problems == base + 1, out
    assert "Mars/Base" in out


def test_nc_r2_an_invalid_value_in_the_global_layer_names_the_global_file(doctor):
    _, base, _ = doctor()
    _, problems, out = doctor(global_yaml="scheduler:\n  timezone: Mars/Base\n")
    assert problems == base + 1, out
    assert re.search(r"gconf/project\.yaml:2\b", out), out


@pytest.mark.parametrize("key,value", [
    ("timezone", "UTC"), ("timezone", "America/New_York"), ("timezone", "Asia/Kolkata"),
    ("starvation_after_seconds", "1"), ("window_tolerance_seconds", "1"),
    ("admission_timeout_seconds", "1"), ("tick_seconds", "1"),
])
def test_nc_r2_doctor_accepts_the_smallest_valid_values(doctor, key, value):
    _, base, _ = doctor("scheduler:\n  enabled: false\n")
    _, problems, out = doctor(f"scheduler:\n  enabled: false\n  {key}: {value}\n")
    assert problems == base, out


# ---------------------------------------------------------------------- gate

def test_nc_r1_gate_off_start_leaves_no_socket_no_state_and_no_process(off):
    out = off.cli("scheduler", "start", timeout=30)
    # the command exists (a missing subcommand would also "leave nothing")
    assert "invalid choice" not in out.stderr and "usage:" not in out.stderr, out.stderr
    assert not off.sock.exists()
    assert not off.state_dir.exists() or not list(off.state_dir.iterdir())
    assert not (state_root() / "scheduler-rpc").exists() or not any(
        (state_root() / "scheduler-rpc").rglob("*.sock"))


# ------------------------------------------------------------ NC-R3: host state

def _modes(root: Path):
    return [(p, stat.S_IMODE(p.stat().st_mode)) for p in files_under(root)]


def test_nc_r3_the_scheduler_dir_is_0700_and_everything_in_it_0600(live):
    assert stat.S_IMODE(live.state_dir.stat().st_mode) == 0o700
    seen = _modes(live.state_dir)
    assert len(seen) > 1, "the scheduler keeps its state under the scheduler dir"
    for path, mode in seen:
        if path.is_dir():
            assert mode == 0o700, (path, oct(mode))
        else:
            assert mode == 0o600, (path, oct(mode))


def test_nc_r3_files_stay_private_after_writes(live):
    live.create()
    live.create(kind="simple", task="second")
    for path, mode in _modes(live.state_dir):
        assert mode & 0o077 == 0, (path, oct(mode))


def test_nc_r3_the_transport_dir_holds_only_the_socket_and_is_private(live):
    assert stat.S_IMODE(live.rpc_dir.stat().st_mode) == 0o700
    assert live.rpc_dir.stat().st_uid == os.getuid()
    assert sorted(p.name for p in live.rpc_dir.iterdir()) == ["rpc.sock"]
    assert stat.S_IMODE(live.sock.stat().st_mode) == 0o600
    assert stat.S_ISSOCK(live.sock.stat().st_mode)


def test_nc_r3_the_state_dir_is_not_under_the_transport_dir_nor_the_reverse(live):
    state, rpc = live.state_dir.resolve(), live.rpc_dir.resolve()
    assert state != rpc and state not in rpc.parents and rpc not in state.parents


def test_nc_r3_no_socket_is_left_in_the_scheduler_dir(live):
    assert not [p for p in files_under(live.state_dir) if p.is_socket()]


# --------------------------------------------------------- NC-R3: docker mounts

def _executor(sched: Sched, extra_mounts=None):
    from multiagents.executor.docker import DockerExecutor
    from multiagents.providers import load_providers

    docker = {"extra_mounts": extra_mounts} if extra_mounts is not None else {}
    sched.p.project["executor"] = {"kind": "docker", "docker": docker}
    sched.p.write()
    paths = ProjectPaths(sched.root)
    cfg = config_mod.load(paths)
    return DockerExecutor(cfg.project["executor"]["docker"], paths,
                          load_providers(cfg.providers), global_config_dir())


def _sources(executor) -> list[Path]:
    return [Path(src).resolve() for src, _ in executor.mounts()]


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def test_nc_r3_gate_on_the_container_gets_the_transport_dir_at_the_same_path(nc):
    sources = _sources(_executor(nc))
    assert nc.rpc_dir.resolve() in sources


def test_nc_r3_gate_on_nothing_under_the_scheduler_dir_is_mounted(nc):
    state = nc.state_dir.resolve()
    for source in _sources(_executor(nc)):
        assert not _within(source, state), source
        assert not _within(state, source), f"{source} would expose the scheduler dir"


def test_nc_r3_gate_on_only_the_transport_dir_not_its_parent(nc):
    parent = (state_root() / "scheduler-rpc").resolve()
    sources = _sources(_executor(nc))
    assert parent not in sources
    assert [s for s in sources if _within(s, parent)] == [nc.rpc_dir.resolve()]


def test_nc_r1_gate_off_the_container_gets_no_scheduler_mount_at_all(off):
    sources = _sources(_executor(off))
    for source in sources:
        assert not _within(source, (state_root() / "scheduler-rpc").resolve())
        assert not _within(source, (state_root() / "scheduler").resolve())


@pytest.mark.parametrize("where", ["dir", "parent", "inside"])
def test_nc_r3_a_configured_mount_that_exposes_the_scheduler_dir_is_refused(nc, where):
    target = {"dir": nc.state_dir,
              "parent": state_root() / "scheduler",
              "inside": nc.state_dir / "sub"}[where]
    target.mkdir(parents=True, exist_ok=True)
    executor = _executor(nc, [{"path": str(target)}])
    with pytest.raises(ValueError):
        executor.mounts()


def test_nc_r3_a_configured_mount_of_the_whole_state_root_is_refused(nc):
    state_root().mkdir(parents=True, exist_ok=True)
    executor = _executor(nc, [{"path": str(state_root())}])
    with pytest.raises(ValueError):
        executor.mounts()


def test_nc_r3_an_unrelated_configured_mount_is_still_allowed(nc, tmp_path):
    extra = tmp_path / "toolchain"
    extra.mkdir()
    assert extra.resolve() in _sources(_executor(nc, [{"path": str(extra)}]))


def test_nc_r9_the_root_capability_appears_in_no_container_argument_or_mount(live):
    token = live.root_token()
    executor = _executor(live)
    assert token not in " ".join(executor.run_args())
    for source, _ in executor.mounts():
        src = Path(source)
        # a mount that holds the token's file would hold the token
        assert not _within(live.state_dir.resolve(), src.resolve())
        assert not _within(src.resolve(), live.state_dir.resolve())
