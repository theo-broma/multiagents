"""ST2-R1: a replacement cannot inherit a receipt without readable identity."""

import subprocess

from multiagents.executor import docker
from st_docker import DockerWorld, admits


def test_st2_r1_replaced_proxy_without_readable_identity_never_inherits_digest(
        tmp_path, monkeypatch):
    world = DockerWorld(tmp_path, monkeypatch)
    ex = world.executor(network="allowlist", egress_allowlist=["example.com"])
    assert ex.ensure_proxy().get("ok")
    # The named proxy was independently replaced with an older filter.
    # Its name and networks match, but its container identity does not.
    state = world.read()
    box = state["containers"][ex.proxy_container]
    box["Id"] = "independent-replacement"
    box["State"]["StartedAt"] = "2026-10-12T12:34:56Z"
    box["loaded_filter"] = r"(^|\.)retired\.example\.net$" + "\n"
    world.write(state)
    world.clear_calls()
    real_run = docker._run

    def partial_inspection(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"] and argv[-1] == ex.proxy_container:
            fmt = argv[3]
            if fmt in ("{{json .}}", "{{.Id}}", "{{.State.StartedAt}}"):
                return subprocess.CompletedProcess(argv, 1, "", "identity observation unavailable")
            if "range .Mounts" in fmt:
                mounts = world.box(ex.proxy_container)["Mounts"]
                text = "\n".join(m["Source"] + ">" + m["Destination"] for m in mounts)
                return subprocess.CompletedProcess(argv, 0, text, "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(docker, "_run", partial_inspection)
    outcome = ex.ensure_proxy()
    # Either refusal or reconciliation is safe; accepting the stale filter is not.
    if outcome.get("ok"):
        applied = world.box(ex.proxy_container)["loaded_filter"]
        assert not admits(applied, "retired.example.net"), (outcome, applied, world.calls())
        assert admits(applied, "example.com"), (outcome, applied)
    else:
        assert "error" in outcome
