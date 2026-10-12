"""ST1/ST2: field-only Docker observations retain lifecycle compatibility."""

import hashlib
import json
import subprocess

import pytest

from multiagents.executor import docker


def result(argv, stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


@pytest.mark.parametrize("error", ["format unsupported", "No such object",
                                  OSError("probe unavailable"),
                                  subprocess.TimeoutExpired(["docker", "inspect"], 120)])
def test_st1_failed_whole_inspect_still_probes_fields(monkeypatch, error):
    """ST1-R2 / D11: field fallback does not forgive a failed policy update."""
    ex = docker.DockerExecutor({"network": "none"})
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "update"]:
            return result(argv, stderr="policy update denied", code=1)
        if argv[3] == "{{json .}}":
            if isinstance(error, Exception):
                raise error
            return result(argv, stderr=error, code=1)
        if argv[3] == "{{.State.Status}}":
            return result(argv, "running")
        return result(argv, code=1)

    monkeypatch.setattr(docker, "_run", run)
    monkeypatch.setattr(docker, "docker_available", lambda: True)
    monkeypatch.setattr(ex, "inside", lambda: False)
    box = ex._inspect_container(ex.container)
    assert box["State"]["Status"] == "running"
    outcome = ex.ensure_running()
    assert not outcome.get("ok"), outcome
    assert "policy update denied" in outcome["error"]
    assert ["docker", "update", "--restart", "no", ex.container] in calls
    assert not any(argv[:2] in (["docker", "start"], ["docker", "run"],
                               ["docker", "rm"], ["docker", "stop"]) for argv in calls)


def test_st1_absence_is_confirmed_by_field_status(monkeypatch):
    ex = docker.DockerExecutor({"network": "none"})
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return result(argv, stderr="No such container", code=1)

    monkeypatch.setattr(docker, "_run", run)
    assert ex._inspect_container(ex.container) is None
    assert calls[-1][3] == "{{.State.Status}}"


def test_st1_unobserved_mounts_refuse_exited_container_without_claiming_mount_drift(monkeypatch):
    """ST1-R3 / D11: an exited container needs observable sources to start."""
    ex = docker.DockerExecutor({"network": "none"})
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            if argv[3] == "{{.State.Status}}":
                return result(argv, "exited")
            if "range .Mounts" in argv[3]:
                return result(argv, stderr="mount observation unavailable", code=1)
        return result(argv)

    monkeypatch.setattr(docker, "_run", run)
    monkeypatch.setattr(docker, "docker_available", lambda: True)
    monkeypatch.setattr(ex, "inside", lambda: False)
    monkeypatch.setattr(ex, "protect_project", lambda: "")
    outcome = ex.ensure_running()
    assert not outcome.get("ok"), outcome
    assert "cannot observe mounts" in outcome["error"]
    assert "mount observation unavailable" in outcome["error"]
    assert ex.container in outcome["error"]
    assert "recreate" in outcome["error"]
    assert ["docker", "start", ex.container] not in calls
    assert not any(argv[:2] == ["docker", "run"] for argv in calls)


@pytest.mark.parametrize("read_networks", [False, True])
def test_st2_field_proxy_reconciles_identity_and_preserves_known_networks(
        tmp_path, monkeypatch, read_networks):
    monkeypatch.setattr(docker, "state_root", lambda: tmp_path / "state")
    ex = docker.DockerExecutor({"network": "allowlist", "egress_allowlist": ["example.com"]},
                               config_dir=tmp_path)
    monkeypatch.setattr(ex, "image_exists", lambda name: True)
    calls = []
    box = {"id": "original", "mounts": [], "networks": {ex.network, "bridge", "extra"}}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            fmt = argv[3]
            if fmt == "{{json .}}":
                return result(argv, stderr="unsupported format", code=1)
            values = {"{{.State.Status}}": "running", "{{.Id}}": box["id"],
                      "{{.State.StartedAt}}": "same-start-time",
                      "{{.HostConfig.RestartPolicy.Name}}": "no"}
            if "range .Mounts" in fmt:
                return result(argv, "\n".join(box["mounts"]))
            if fmt == "{{json .NetworkSettings.Networks}}" and read_networks:
                return result(argv, json.dumps(dict.fromkeys(box["networks"], {})))
            return result(argv, values[fmt]) if fmt in values else result(argv, code=1)
        if argv[:2] == ["docker", "run"]:
            box["id"] = "replacement"
            box["networks"] = {argv[argv.index("--network") + 1]}
            box["mounts"] = [argv[i + 1].replace(":", ">", 1).rsplit(":", 1)[0]
                             for i, value in enumerate(argv[:-1]) if value == "-v"]
        if argv[:3] == ["docker", "network", "connect"]:
            box["networks"].add(argv[3])
        return result(argv)

    monkeypatch.setattr(docker, "_run", run)
    first = ex.ensure_proxy()
    assert first.get("ok") and first.get("created"), first
    if read_networks:
        assert "extra" in box["networks"]
    count = len(calls)
    second = ex.ensure_proxy()
    assert second.get("ok") and second.get("existed"), second
    assert not any(argv[:2] == ["docker", "rm"] for argv in calls[count:])
    box["id"] = "externally-replaced"
    third = ex.ensure_proxy()
    assert third.get("ok") and third.get("created"), third


@pytest.mark.parametrize("missing", ["Id", "NetworkSettings"])
def test_st2_incomplete_whole_proxy_inspect_never_records_applied_filter(
        tmp_path, monkeypatch, missing):
    monkeypatch.setattr(docker, "state_root", lambda: tmp_path / "state")
    ex = docker.DockerExecutor({"network": "allowlist", "egress_allowlist": ["example.com"]},
                               config_dir=tmp_path)
    monkeypatch.setattr(ex, "image_exists", lambda name: True)
    box = {"Id": "proxy", "State": {"Status": "running"}, "Mounts": [],
           "NetworkSettings": {"Networks": {ex.network: {}, "bridge": {}}}}
    del box[missing]

    def run(argv, **kwargs):
        return result(argv, json.dumps(box) if argv[:2] == ["docker", "inspect"] else "")

    monkeypatch.setattr(docker, "_run", run)
    config_dir = ex.write_proxy_config(tmp_path / "proxy" / ex.slug)
    receipt_path = tmp_path / "state" / "container-state" / ex.slug / "egress-proxy" / "applied.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps({"digest": hashlib.sha256((config_dir / "filter").read_bytes()).hexdigest(),
                                        "container_id": None}))
    outcome = ex.ensure_proxy()
    assert not outcome.get("ok"), outcome
    assert "incomplete" in outcome["error"]
    receipts = list((tmp_path / "state").rglob("applied.json"))
    assert len(receipts) == 1
    assert "digest" not in json.loads(receipts[0].read_text())
