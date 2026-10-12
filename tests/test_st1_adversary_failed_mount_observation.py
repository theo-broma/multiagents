"""ST1-R2/R3 / D11: unknown mounts allow running policy-no boxes, not starts."""

import subprocess

import pytest

from multiagents.executor import docker
from st_docker import DockerWorld


@pytest.mark.parametrize("status, requirement", [
    ("running", "ST1-R2"),
    ("exited", "ST1-R3"),
])
@pytest.mark.parametrize("policy", ["unless-stopped", "no"])
def test_st1_r2_r3_failed_mount_observation_follows_d11(
        tmp_path, monkeypatch, status, requirement, policy):
    world = DockerWorld(tmp_path, monkeypatch)
    ex = world.executor()
    assert not ex.protect_project()
    missing = world.base / "vanished-bind-source"
    mounts = [{"Type": "bind", "Source": str(p), "Destination": str(p),
               "RW": not ro} for p, ro in ex.mounts()]
    mounts.append({"Type": "bind", "Source": str(missing),
                   "Destination": "/historical-bind", "RW": True})
    world.put(ex.container, status=status, policy=policy, mounts=mounts)
    real_run = docker._run

    def partial_inspection(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"] and argv[-1] == ex.container:
            fmt = argv[3]
            if fmt == "{{json .}}" or ("range .Mounts" in fmt and ".Source" in fmt):
                return subprocess.CompletedProcess(argv, 1, "", "mount observation unavailable")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(docker, "_run", partial_inspection)
    outcome = ex.ensure_running()
    if status == "running":
        assert outcome.get("ok"), (requirement, outcome, world.calls())
    else:
        assert not outcome.get("ok"), (requirement, outcome, world.calls())
        assert "cannot observe mounts" in outcome["error"]
        assert "mount observation unavailable" in outcome["error"]
        assert ex.container in outcome["error"]
        assert "recreate" in outcome["error"]
    update = ["update", "--restart", "no", ex.container]
    if policy == "unless-stopped":
        assert update in world.calls()
    assert not [a for a in world.calls()
                if a[0] in ("start", "run", "rm", "stop") and ex.container in a]
    assert not missing.exists()
