"""bug-c106a9: `docker rm` / `docker down` must act on the auth sidecar too."""
import subprocess
from pathlib import Path

import pytest

from multiagents.executor import docker as docker_mod
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths


@pytest.fixture
def calls(tmp_path: Path, monkeypatch) -> list[list[str]]:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths(root)
    paths.ensure()
    log: list[list[str]] = []

    def fake_run(argv, timeout=120):
        log.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="running\n", stderr="")

    monkeypatch.setattr(docker_mod, "_run", fake_run)
    return log, DockerExecutor({"image": "img", "network": "bridge"}, paths=paths)


def test_rm_removes_auth_sidecar(calls):
    log, ex = calls
    out = ex.stop(remove=True)
    assert ["docker", "rm", "-f", ex.auth_container] in log
    assert out["acted_on"][ex.auth_container] is True


def test_down_stops_auth_sidecar(calls):
    log, ex = calls
    ex.stop(remove=False)
    assert ["docker", "stop", ex.auth_container] in log
