"""HF3.1–HF3.4 / F62: auth bridge admission, observation and cleanup."""

import subprocess
import sys
from pathlib import Path

import pytest

from multiagents.executor import docker

sys.path.insert(0, str(Path(__file__).parent / "support"))
from st_docker import DockerWorld


@pytest.fixture
def world(tmp_path, monkeypatch):
    return DockerWorld(tmp_path, monkeypatch)


@pytest.mark.parametrize("reason", [
    "", "bridge attachment refused", "endpoint already exists", "x" * 401,
], ids=["empty-stderr", "ordinary", "misleading-already-exists", "truncated"])
def test_hf3_1_failed_new_attachment_reports_error_and_removes_container(world, reason):
    ex = world.auth_executor()
    world.fail(["network", "connect", "bridge"], reason=reason, target=ex.auth_container)
    outcome = ex.ensure_auth_proxy()
    assert outcome.get("ok") is False, outcome
    assert outcome["error"] == reason[:400]
    assert ["rm", "-f", ex.auth_container] in world.calls()
    assert ex.auth_container not in world.read()["containers"]
    world.recover()
    assert ex.ensure_auth_proxy().get("ok") is True
    assert "bridge" in world.box(ex.auth_container)["NetworkSettings"]["Networks"]


@pytest.mark.parametrize("existing", [False, True], ids=["created", "running"])
@pytest.mark.parametrize("reason", ["endpoint already exists", "permission denied"])
def test_hf3_2_failed_connect_with_actual_bridge_membership_is_success(
        world, monkeypatch, existing, reason):
    ex = world.auth_executor()
    if existing:
        assert ex.ensure_auth_proxy().get("ok") is True
        detach_bridge(world, ex)
    world.clear_calls()
    original_run = docker._run

    def connected_but_failed(argv, *args, **kwargs):
        result = original_run(argv, *args, **kwargs)
        if argv[:4] == ["docker", "network", "connect", "bridge"]:
            assert result.returncode == 0, result.stderr
            # The daemon applied the mutation, but its reply reports failure.
            return subprocess.CompletedProcess(argv, 1, "", reason)
        return result

    monkeypatch.setattr(docker, "_run", connected_but_failed)
    outcome = ex.ensure_auth_proxy()
    assert outcome.get("ok") is True, outcome
    assert "bridge" in world.box(ex.auth_container)["NetworkSettings"]["Networks"]
    assert not any(a[:1] == ["rm"] and ex.auth_container in a for a in world.calls())


def test_hf3_3_failed_cleanup_reports_connect_and_removal_errors(world):
    ex = world.auth_executor()
    state = world.read()
    state["failures"] = [
        {"prefix": ["network", "connect", "bridge"], "target": ex.auth_container,
         "reason": "bridge-connect-denied"},
        {"prefix": ["rm", "-f"], "target": ex.auth_container,
         "reason": "container-removal-denied"},
    ]
    world.write(state)
    outcome = ex.ensure_auth_proxy()
    assert outcome.get("ok") is False, outcome
    assert "bridge-connect-denied" in outcome["error"]
    assert "container-removal-denied" in outcome["error"]
    assert ["rm", "-f", ex.auth_container] in world.calls()
    assert ex.auth_container in world.read()["containers"]


def detach_bridge(world, ex):
    state = world.read()
    del state["containers"][ex.auth_container]["NetworkSettings"]["Networks"]["bridge"]
    world.write(state)


@pytest.mark.parametrize("fail", [False, True], ids=["succeeds", "fails"])
def test_hf3_4_existing_running_proxy_without_bridge_is_connected_and_preserved(world, fail):
    ex = world.auth_executor()
    assert ex.ensure_auth_proxy().get("ok") is True
    detach_bridge(world, ex)
    world.clear_calls()
    if fail:
        world.fail(["network", "connect", "bridge"], reason="running-connect-denied",
                   target=ex.auth_container)
    outcome = ex.ensure_auth_proxy()
    assert outcome.get("ok") is (not fail), outcome
    if fail:
        assert outcome["error"] == "running-connect-denied"
    else:
        assert "bridge" in world.box(ex.auth_container)["NetworkSettings"]["Networks"]
    assert ["network", "connect", "bridge", ex.auth_container] in world.calls()
    assert not any(a[:1] == ["rm"] and ex.auth_container in a for a in world.calls())
    assert world.box(ex.auth_container)["State"]["Status"] == "running"
    if fail:
        world.recover()
        assert ex.ensure_auth_proxy().get("ok") is True
        assert "bridge" in world.box(ex.auth_container)["NetworkSettings"]["Networks"]


def test_hf3_4_existing_running_proxy_already_on_bridge_is_reusable(world):
    ex = world.auth_executor()
    assert ex.ensure_auth_proxy().get("ok") is True
    world.clear_calls()
    assert ex.ensure_auth_proxy().get("ok") is True
    assert world.box(ex.auth_container)["State"]["Status"] == "running"
    assert not any(a[:1] == ["rm"] and ex.auth_container in a for a in world.calls())
