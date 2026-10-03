"""SF-R3: operator release stops a blocked predecessor before lifting its hold."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

import test_sf_review_r3 as h
import test_sf_review_r5 as previous
import test_sf_review_r6 as r6
from multiagents import procs
from multiagents.executor.base import running
from multiagents.runner import Runner

w = h.w


@pytest.fixture
def predecessor():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             start_new_session=True)
    try:
        yield child, procs.start_time(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize("kill_fails,identity_source", [
    (False, "captured"), (True, "captured"), (False, "hold"),
])
def test_docker_unknown_probe_attempts_both_identities_and_records_uncertainty(
        w, monkeypatch, predecessor, kill_fails, identity_source):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    child, start = predecessor
    record = r6.unknown_hold(w, aid, pid=child.pid, start=start, kind="docker")
    identity = {"kind": "docker", "container": "predecessor-container"}
    record["executor"] = record["steer_cleanup"]["predecessor"]["executor"] = identity
    if identity_source == "hold":
        record["steer_cleanup"]["predecessor"]["executor"] = {"kind": "unknown"}
    w.tree().update(aid, cleanup_hold=record)
    run_dir = w.runner.paths.run_dir(aid)
    (run_dir / "wrapper.pid").write_text("111\n")
    (run_dir / "container.pid").write_text("222\n")
    recovered = Runner(w.runner.paths, w.runner.config)
    monkeypatch.setattr(recovered, "_docker_for", lambda container: None)
    recovered._settle_holds()
    assert recovered._holds[aid].steer["blocked_reason"]
    calls = []

    class Docker:
        kind = "docker"

        def inside(self):
            return False

        def kill_detached(self, node_id):
            assert node_id == aid
            assert aid in recovered._locks
            assert not recovered._holds[aid].steer.get("operator_release")
            assert (run_dir / "wrapper.pid").read_text().strip() == "111"
            assert (run_dir / "container.pid").read_text().strip() == "222"
            calls.append(node_id)
            if kill_fails:
                raise OSError("daemon unavailable")
            return True

        def wrapper_verdict(self, node_id):
            return None

        def wrapper_alive(self, node_id):
            # A transport exit code must not confirm container death.
            return False

    def docker_for(container):
        assert container == "predecessor-container"
        return Docker()
    monkeypatch.setattr(recovered, "_docker_for", docker_for)
    real_detached = recovered.stop_detached
    detached = []

    def stop_detached(node):
        detached.append(node.id)
        assert node.pid == child.pid and node.pid_start == start
        assert node.exec_identity == identity
        return real_detached(node)
    monkeypatch.setattr(recovered, "stop_detached", stop_detached)
    real_release = recovered._release
    state = {"storage_failed": False}

    def release(node_id):
        assert calls == [aid]
        assert not running(child.pid, start), "host client still alive at release"
        saved = recovered.tree.get(aid).cleanup_hold
        assert saved["predecessor_death_confirmed"] is False
        assert saved["steer_cleanup"]["predecessor_death_confirmed"] is False
        if not state["storage_failed"]:
            state["storage_failed"] = True
            raise OSError("lock release failed once")
        return real_release(node_id)
    monkeypatch.setattr(recovered, "_release", release)
    result = asyncio.run(recovered.stop(aid))
    assert detached == calls == [aid]
    assert result["released_steer_hold"] is True
    assert result["predecessor_death_confirmed"] is False
    assert "unconfirmed" in result["warning"]
    recovered._settle_holds()
    assert not recovered._held(aid)
    assert aid not in recovered._locks
    assert recovered.provider_slots()["acme"]["in_use"] == 0
    events = w.p.events_of("steer_hold_released")
    assert len(events) == 1
    assert events[0]["predecessor_death_confirmed"] is False


@pytest.mark.parametrize("source", ["captured", "node", "hold"])
def test_known_predecessor_is_killed_and_confirmed_before_operator_release(
        w, monkeypatch, predecessor, source):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    child, start = predecessor
    record = r6.unknown_hold(w, aid, pid=child.pid, start=start, kind="local")
    if source != "captured":
        saved = record["steer_cleanup"]["predecessor"]
        saved["pid"], saved["pid_start"] = None, ""
        if source == "hold":
            record.update(pid=child.pid, pid_start=start)
            saved["executor"] = {"kind": "unknown"}
        else:
            w.tree().update(aid, pid=child.pid, pid_start=start,
                            exec_identity={"kind": "local"})
    record["steer_cleanup"]["blocked_reason"] = "previous identity recovery failed"
    w.tree().update(aid, cleanup_hold=record)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    real_detached = recovered.stop_detached
    stopped = []

    def stop_detached(node):
        assert aid in recovered._locks
        assert procs.alive(child.pid, start)
        stopped.append(node.id)
        result = real_detached(node)
        child.wait(timeout=10)
        return result
    monkeypatch.setattr(recovered, "stop_detached", stop_detached)
    real_release = recovered._release

    def release(node_id):
        assert stopped == [aid], "hold released without attempting predecessor termination"
        assert not procs.alive(child.pid, start)
        saved = recovered.tree.get(aid).cleanup_hold
        assert saved["predecessor_death_confirmed"] is True
        return real_release(node_id)
    monkeypatch.setattr(recovered, "_release", release)
    result = asyncio.run(recovered.stop(aid))
    assert stopped == [aid]
    assert result["predecessor_death_confirmed"] is True
    assert "warning" not in result
    assert not recovered._held(aid)
    assert aid not in recovered._locks
    assert recovered.provider_slots()["acme"]["in_use"] == 0
    events = w.p.events_of("steer_hold_released")
    assert len(events) == 1
    assert events[0]["predecessor_death_confirmed"] is True
