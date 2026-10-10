"""Claude effort reaches spawned agents and driver-launched sessions."""

import json

import pytest

from multiagents import config as config_mod, driver
from multiagents.config import AgentSpec
from multiagents.paths import ProjectPaths
from multiagents.providers import load_providers
from test_phase0_provider_compact import SID, Scratch, _launch_env, _spawn_argv


def _providers(tmp_path):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    raw = config_mod.load(paths).providers
    raw["claude-b"] = {"extends": "claude"}
    return load_providers(raw)


@pytest.mark.parametrize("name", ["claude", "claude-b"])
def test_spawn_passes_effort(tmp_path, name):
    argv = _spawn_argv(_providers(tmp_path)[name], effort="medium")
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv.count("--effort") == 1


@pytest.mark.parametrize("name", ["claude", "claude-b"])
@pytest.mark.parametrize("options", [{}, {"effort": None}, {"effort": ""}])
def test_spawn_without_effort_has_no_flag(tmp_path, name, options):
    argv = _spawn_argv(_providers(tmp_path)[name], **options)
    assert "--effort" not in argv


@pytest.mark.parametrize("role", ["orchestrator", "initializer"])
@pytest.mark.parametrize("effort", [None, "", "medium"])
def test_driver_launch_context_only_carries_set_effort(tmp_path, role, effort):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    spec = AgentSpec(role, "claude", "opus", effort=effort)
    context = driver._launch_context(paths, config_mod.load(paths), spec)
    if effort:
        assert context["MULTIAGENTS_EFFORT"] == effort
    else:
        assert "MULTIAGENTS_EFFORT" not in context


def test_driver_launch_action_receives_effort(tmp_path, monkeypatch):
    env = _launch_env(tmp_path, monkeypatch, {"effort": "medium"})
    assert env["MULTIAGENTS_EFFORT"] == "medium"


@pytest.mark.parametrize("effort", [None, ""])
def test_driver_launch_action_without_effort_omits_it(tmp_path, monkeypatch, effort):
    env = _launch_env(tmp_path, monkeypatch, {"effort": effort})
    assert "MULTIAGENTS_EFFORT" not in env


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("unattended", ["0", "1"])
@pytest.mark.parametrize("effort", [None, "", "medium"])
def test_launch_passes_effort_on_first_launch_and_resume(
        tmp_path, resume, unattended, effort):
    scratch = Scratch(tmp_path)
    if resume:
        scratch.session()
    env = {"MULTIAGENTS_RESUME": "1" if resume else "0",
           "MULTIAGENTS_UNATTENDED": unattended}
    if effort is not None:
        env["MULTIAGENTS_EFFORT"] = effort
    got = scratch.run("claude", "launch-command", **env)
    assert got.returncode == 0, got.stderr
    argv = json.loads(got.stdout)
    flag = "--resume" if resume else "--session-id"
    assert argv[argv.index(flag) + 1] == SID
    if effort:
        assert argv[argv.index("--effort") + 1] == effort
        assert argv.count("--effort") == 1
    else:
        assert "--effort" not in argv
