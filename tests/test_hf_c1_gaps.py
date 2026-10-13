"""HF2.1/HF2.1a (F30) and HF3.1/HF3.4 (F62): adversarial coverage gaps.

Only public executor results and the fake Docker daemon's effects are asserted.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from multiagents.executor import docker

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h
from st_docker import DockerWorld


@pytest.fixture
def world(tmp_path, monkeypatch):
    return DockerWorld(tmp_path, monkeypatch)


def install_path(tmp_path, monkeypatch, kind, *, exists=True):
    """Declare an installation outside the project, never the real package."""
    path = tmp_path / "installation" / ("multiagents" if kind == "package" else "python")
    if exists:
        path.parent.mkdir()
        if kind == "package":
            path.mkdir()
        else:
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
    monkeypatch.setattr(docker, "server_install_paths", lambda: [path])
    return path


@pytest.mark.parametrize("kind", ["package", "interpreter"])
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "existing"])
def test_hf2_1a_writable_extra_equal_to_server_install_path_is_refused(
        world, monkeypatch, kind, exists):
    path = install_path(world.base, monkeypatch, kind, exists=exists)
    ex = world.executor(extra_mounts=[{"path": str(path), "read_only": False}])

    problems = ex.configuration_problems()
    matching = [problem for problem in problems if str(path) in problem]
    assert matching, f"no server-install collision diagnostic for {path}: {problems}"
    assert re.search(r"read[-_ ]?only", " ".join(matching), re.I)
    with pytest.raises(ValueError, match=re.escape(str(path))):
        ex.mounts()
    assert world.calls() == [], "mount admission must not invoke Docker"
    assert path.exists() is exists, "mount admission must not create an installation"


@pytest.mark.parametrize("kind", ["package", "interpreter"])
def test_hf2_1a_read_only_extra_equal_to_server_install_path_is_valid(
        world, monkeypatch, kind):
    path = install_path(world.base, monkeypatch, kind)
    ex = world.executor(extra_mounts=[{"path": str(path), "read_only": True}])

    assert ex.configuration_problems() == []
    matching = [mount for mount in ex.mounts() if mount[0] == path]
    assert matching == [(path, True)]
    assert world.calls() == []


def test_hf2_1a_writable_extra_parent_of_server_install_is_not_a_collision(
        world, monkeypatch):
    path = install_path(world.base, monkeypatch, "package")
    parent = path.parent
    ex = world.executor(extra_mounts=[{"path": str(parent), "read_only": False}])

    assert ex.configuration_problems() == []
    assert (parent, False) in ex.mounts()
    assert world.calls() == []


@pytest.mark.parametrize("read_only_index", [0, 1], ids=["ro-first", "ro-second"])
def test_hf2_1_duplicate_diagnostic_identifies_the_correct_read_only_entry(
        tmp_path, read_only_index):
    path = tmp_path / "contradictory-mount"
    entries = [{"path": str(path), "read_only": index == read_only_index}
               for index in range(2)]
    ex = h.make_docker_executor(tmp_path, mount_cli_from_host=False, extra_mounts=entries)

    matching = [message for message in ex.configuration_problems() if str(path) in message]
    assert matching, f"no collision diagnostic for {path}"
    for message in matching:
        assert re.search(r"read[-_ ]?only", message, re.I), message
        # Either name the indexed extra entry or quote its read_only=True spelling.
        indexed_entry = re.search(
            rf"extra[_-]mounts\s*\[\s*{read_only_index}\s*\]"
            rf"|\b(?:entry|index)\s*[#:=]?\s*{read_only_index}\b", message, re.I)
        spelled_entry = re.search(
            r"['\"]?read[_-]only['\"]?\s*[:=]\s*true\b", message, re.I)
        assert indexed_entry or spelled_entry, (
            f"read-only entry is extra_mounts[{read_only_index}] = "
            f"{entries[read_only_index]!r}, but diagnostic says: {message}")
    with pytest.raises(ValueError, match=re.escape(str(path))):
        ex.mounts()


def existing_proxy_without_bridge(world, *, stopped=False):
    ex = world.auth_executor()
    assert ex.ensure_auth_proxy().get("ok") is True
    state = world.read()
    box = state["containers"][ex.auth_container]
    del box["NetworkSettings"]["Networks"]["bridge"]
    if stopped:
        box["State"]["Status"] = "exited"
    world.write(state)
    world.clear_calls()
    return ex


def assert_existing_proxy_bridge_result(world, ex, reason):
    if reason is not None:
        world.fail(["network", "connect", "bridge"], reason=reason, target=ex.auth_container)
    outcome = ex.ensure_auth_proxy()
    assert outcome.get("ok") is (reason is None), outcome
    box = world.box(ex.auth_container)
    assert box["State"]["Status"] == "running"
    assert ("bridge" in box["NetworkSettings"]["Networks"]) is (reason is None)
    assert ["network", "connect", "bridge", ex.auth_container] in world.calls()
    assert not any(call[:1] == ["rm"] and ex.auth_container in call for call in world.calls())
    if reason is not None:
        assert outcome["error"] == reason
        world.recover()
        assert ex.ensure_auth_proxy().get("ok") is True
        assert "bridge" in world.box(ex.auth_container)["NetworkSettings"]["Networks"]


@pytest.mark.parametrize("reason", [None, "stopped-connect-denied", "endpoint already exists"],
                         ids=["connect-succeeds", "connect-fails", "misleading-error"])
def test_hf3_1_hf3_4_started_existing_proxy_checks_bridge(world, reason):
    ex = existing_proxy_without_bridge(world, stopped=True)
    assert_existing_proxy_bridge_result(world, ex, reason)


@pytest.mark.parametrize("reason", [None, "unread-mounts-connect-denied", "endpoint already exists"],
                         ids=["connect-succeeds", "connect-fails", "misleading-error"])
def test_hf3_1_hf3_4_running_proxy_with_unreadable_mounts_checks_bridge(
        world, monkeypatch, reason):
    ex = existing_proxy_without_bridge(world)
    original_run = docker._run

    def partial_inspection(argv, *args, **kwargs):
        # Fail Docker observations containing mounts, while leaving status and
        # network observations available. This is the existing ST1/D11 seam.
        if argv[:2] == ["docker", "inspect"] and argv[-1] == ex.auth_container:
            if "{{json .}}" in argv or any(".Mounts" in arg for arg in argv):
                return subprocess.CompletedProcess(argv, 1, "", "mount observation unavailable")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(docker, "_run", partial_inspection)
    assert_existing_proxy_bridge_result(world, ex, reason)
