"""PN-R1: planning can overlap execution, except during bootstrap."""

import os
from types import SimpleNamespace

import pytest

from multiagents import driver
from multiagents.config import AgentSpec
from multiagents.paths import ProjectPaths


class ReachedProviderSetup(Exception):
    """Stop before launching a real provider, after the clash checks."""


@pytest.fixture
def launch(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    (paths.root / "BRIEF.md").write_text("# Existing brief\n")
    monkeypatch.setattr(driver, "_launched_spec",
                        lambda *args: AgentSpec("driver", "fake", "model"))

    def providers(*args):
        raise ReachedProviderSetup

    monkeypatch.setattr(driver, "load_providers", providers)
    config = SimpleNamespace(team="implement", providers={})

    def start(role, running, force=False):
        driver._write_pid(paths, running, os.getpid())
        return driver._launch_agent(paths, config, role, resume=True, force=force)

    return paths, start


@pytest.mark.parametrize("role,running", [
    ("initializer", "orchestrator"),
    ("orchestrator", "initializer"),
    ("initializer", "orchestrator-turn"),
    ("orchestrator", "initializer-turn"),
])
def test_different_roles_can_overlap(launch, capsys, role, running):
    _, start = launch
    with pytest.raises(ReachedProviderSetup):
        start(role, running)
    note = capsys.readouterr().err
    assert len(note.splitlines()) == 1
    assert f"an {running} is running (pid {os.getpid()})" in note
    assert "context/plans/" in note
    assert "context/specs/" in note


@pytest.mark.parametrize("role,running", [
    ("orchestrator", "orchestrator"),
    ("initializer", "initializer"),
    ("orchestrator", "orchestrator-turn"),
    ("orchestrator-turn", "orchestrator"),
    ("initializer", "initializer-turn"),
    ("initializer-turn", "initializer"),
])
def test_same_role_is_refused(launch, capsys, role, running):
    _, start = launch
    assert start(role, running) == 2
    message = capsys.readouterr().err
    assert f"{running} is already running here (pid {os.getpid()})" in message
    assert "same role" in message


@pytest.mark.parametrize("role,running,bootstrap", [
    ("orchestrator", "orchestrator", False),
    ("initializer", "initializer", False),
    ("initializer", "orchestrator", True),
    ("orchestrator", "orchestrator-turn", False),
])
def test_force_overrides_refusal(launch, capsys, role, running, bootstrap):
    paths, start = launch
    if bootstrap:
        (paths.root / "BRIEF.md").unlink()
    with pytest.raises(ReachedProviderSetup):
        start(role, running, force=True)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("role,running", [
    ("initializer", "orchestrator"),
    ("initializer", "orchestrator-turn"),
    ("initializer-turn", "orchestrator"),
])
def test_bootstrap_initializer_is_refused_while_orchestrator_runs(launch, capsys, role, running):
    paths, start = launch
    (paths.root / "BRIEF.md").unlink()
    assert start(role, running) == 2
    message = capsys.readouterr().err
    assert f"{running} is already running" in message
    assert "create BRIEF.md during bootstrap" in message
    assert "--force" in message
