"""Adversary pass, slice 3 (IC-C15, IC-C16, IC-C17): the five K3 launch
boundaries and adoption after a scheduler restart, run against the REAL
`DockerBackend` over the fake `docker` CLI (`tests/support/fake_docker.py`).

The suite's own boundary tests (`test_ic_c15_adoption.py`) run them on the
reference backend, and on Docker only under SG_TEST_DOCKER=1. Here the
scheduler's isolated path drives `DockerBackend` itself (wrapped by the crash
injector `ic_s3_backends:make_docker`), so what is checked is Docker's own
record as the fake keeps it: how many times each container was started, which
containers still exist, and the bytes the host recorded.

The fake runs no workload: a started container replays the scripted stdout
(`fd.workload`), and `runs=True` keeps it running until the test finishes it.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import fake_docker as fdk  # noqa: E402
import ic_s3 as s3  # noqa: E402
import ic_support as ic  # noqa: E402
import ic_workload as wl  # noqa: E402
from ic_support import kind_of  # noqa: E402

RESTART_WAIT = 15


class DockerFakeWorld:
    """`S3Sched` (workload provider, crash injector) whose backend is the
    real DockerBackend over the fake CLI."""

    def __init__(self, tmp_path, monkeypatch):
        self.s = s3.S3Sched(tmp_path, monkeypatch)
        self.fd = fdk.FakeDocker(tmp_path).install(monkeypatch)
        monkeypatch.setenv(ic.BACKEND_ENV, "ic_s3_backends:make_docker")
        self.s._reset_server()

    def be(self):
        return s3.docker_backend(self.s.backend_root)

    def starts(self) -> dict[str, int]:
        return {cid: c["StartCount"] for cid, c in self.fd.containers().items()}

    def key_of(self, node):
        run = self.s.runs(node)[-1]
        return self.s.evidence(run["attempt_id"]).get("executor", {}).get("creation_key")

    def wait_key(self, node, timeout=ic.WAIT):
        return self.s.wait(lambda: self.s.runs(node) and self.key_of(node), timeout, "a creation key")

    def close(self):
        self.s.close()


@pytest.fixture
def dw(tmp_path, monkeypatch):
    w = DockerFakeWorld(tmp_path, monkeypatch)
    yield w
    w.close()


def crashed(w, point, stdout=b""):
    w.fd.workload(stdout=stdout)
    w.s.start()
    pid = w.s.crash_at(point)
    node = w.s.node({"lines": 1})
    w.s.wait_dead(pid, timeout=15)
    return node


# ============================================== the five launch boundaries

def test_adv_boundary_1_docker_before_creation_creates_exactly_one_container_once(dw):
    node = crashed(dw, "provision_entry", wl.lines_stdout(2))
    assert dw.fd.containers() == {}
    dw.s.start()
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    assert sorted(dw.starts().values()) == [1]


@pytest.mark.parametrize("point", ["provision_done", "run_entry"])
def test_adv_boundary_2_docker_a_created_container_is_removed_never_started(dw, point):
    node = crashed(dw, point, wl.lines_stdout(2))
    (created,) = dw.fd.containers()
    assert dw.fd.containers()[created]["State"]["Status"] == "created"
    dw.s.start()
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    # IC-C15 boundary 2: removed without keeping artifacts, never started after the fact
    assert dw.fd.container(created) is None, dw.fd.container(created)["State"]
    assert sum(dw.starts().values()) == 1


def test_adv_boundary_3_docker_started_but_not_recorded_is_adopted_and_never_started_twice(dw):
    dw.fd.workload(stdout=wl.lines_stdout(4))
    dw.s.start()
    pid = dw.s.crash_at("run_done")
    node = dw.s.node({"lines": 4})
    dw.s.wait_dead(pid, timeout=15)
    dw.s.start()
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    assert len(dw.s.runs(node)) == 1
    assert list(dw.starts().values()) == [1]
    assert dw.s.host_output(dw.s.run_ids(node)[0])[0] == wl.lines_stdout(4)


def test_adv_boundary_4_docker_exit_seen_then_crash_drains_once_and_records_the_code(dw):
    dw.fd.workload(stdout=wl.lines_stdout(3), stderr=b"E1\nE2\n", exit_code=3)
    dw.s.start()
    pid = dw.s.crash_at("exited_seen")
    node = dw.s.node({"lines": 3})
    dw.s.wait_dead(pid, timeout=15)
    dw.s.start()
    view = s3.wait_done(dw.s, node, RESTART_WAIT)
    assert view["state"] in ("done", "failed", "held"), view["state"]
    out, err = dw.s.host_output(dw.s.run_ids(node)[0])
    assert out == wl.lines_stdout(3) and err == b"E1\nE2\n"
    ev = dw.s.evidence(dw.s.runs(node)[0]["attempt_id"])["executor"]
    assert ev.get("exit_code") == 3 and ev.get("oom") is False
    assert sum(dw.starts().values()) == 1


def test_adv_boundary_5_docker_crash_at_retention_keeps_the_container_exited_and_one_run(dw):
    dw.fd.workload(stdout=wl.lines_stdout(2))
    dw.s.start()
    pid = dw.s.crash_at("remove_entry")
    node = dw.s.node({"lines": 2})
    dw.s.wait_dead(pid, timeout=15)
    dw.s.start()
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    assert len(dw.s.runs(node)) == 1
    (c,) = dw.fd.containers().values()                    # IC-C18: retained, not discarded
    assert c["State"]["Status"] == "exited" and c["StartCount"] == 1


# ============================================ adoption of a live container

def test_adv_adopt_running_docker_output_written_while_down_reaches_the_host_once(dw):
    """IC-C15 replay rule: a kill while streaming, more output (including a
    partial line) while the scheduler is down, the end while it is down."""
    dw.fd.workload(runs=True)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    (cid,) = dw.s.wait(lambda: [c for c, v in dw.fd.containers().items() if v["State"]["Running"]],
                       ic.WAIT, "a running container")
    first = wl.lines_stdout(6)
    dw.fd.append_output(cid, stdout=first[: len(first) // 2 + 7])          # ends mid-line
    run_id = dw.s.wait(lambda: dw.s.run_ids(node) and dw.s.run_ids(node)[0], ic.WAIT, "the run id")
    dw.s.wait(lambda: dw.s.host_output(run_id)[0].count(b"\n") >= 2, ic.WAIT, "two host lines")
    dw.s.kill9()
    dw.fd.append_output(cid, stdout=first[len(first) // 2 + 7:], stderr=b"late err\n")
    dw.fd.finish(cid, code=0)
    dw.s.start()
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    assert dw.s.host_output(run_id) == (first, b"late err\n")
    assert len(dw.s.runs(node)) == 1 and dw.fd.start_count(cid) == 1


def test_adv_adopt_absent_docker_container_removed_while_down_is_a_lost_launch_never_restarted(dw):
    dw.fd.workload(runs=True)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    (cid,) = dw.s.wait(lambda: [c for c, v in dw.fd.containers().items() if v["State"]["Running"]],
                       ic.WAIT, "a running container")
    dw.s.kill9()
    dw.fd.remove_behind_its_back(cid)
    dw.fd.workload(stdout=wl.lines_stdout(1))
    dw.s.start()
    view = dw.s.wait(lambda: any(c["class"] == "launch" for c in ic.causes(dw.s.get(node))) and dw.s.get(node),
                     RESTART_WAIT, "the lost launch")
    assert {r["executor"]["kind"] for r in dw.s.runs(node)} == {"isolated"}
    assert dw.s.fake.spawns() == 0                                         # nothing on the host


def test_adv_adopt_unknown_docker_daemon_down_at_restart_holds_and_never_relaunches(dw):
    dw.fd.workload(runs=True)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    (cid,) = dw.s.wait(lambda: [c for c, v in dw.fd.containers().items() if v["State"]["Running"]],
                       ic.WAIT, "a running container")
    dw.s.kill9()
    dw.fd.daemon_down()
    dw.s.start()
    time.sleep(2.5)
    view = dw.s.get(node)
    assert view["state"] not in ("done", "failed", "cancelled"), ic.text_of(view)[:400]
    assert len(dw.s.runs(node)) == 1
    dw.fd.daemon_down(False)
    dw.fd.finish(cid, code=0, stdout=wl.lines_stdout(1))
    assert s3.wait_done(dw.s, node, RESTART_WAIT)["state"] == "done"
    assert len(dw.s.runs(node)) == 1 and dw.fd.start_count(cid) == 1


# ======================================================= IC-C16 / IC-C17

def first_end(w, node):
    def ended():
        runs = w.s.runs(node)
        if not runs:
            return None
        ev = w.s.evidence(runs[0]["attempt_id"]).get("executor", {})
        return ev if ev.get("termination_confirmed") and "exit_code" in ev else None
    return w.s.wait(ended, RESTART_WAIT, "the first attempt's exit evidence")


def test_adv_cancel_racing_an_exit_releases_and_never_starts_the_container_again(dw):
    dw.fd.workload(runs=True)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    (cid,) = dw.s.wait(lambda: [c for c, v in dw.fd.containers().items() if v["State"]["Running"]],
                       ic.WAIT, "a running container")
    dw.fd.finish(cid, code=0, stdout=wl.lines_stdout(1))                   # exits as the cancel arrives
    dw.s.rpc("stop_run", {"id": node}, timeout=ic.WAIT)
    view = s3.wait_done(dw.s, node, RESTART_WAIT)
    assert view["state"] in ("done", "cancelled"), view["state"]
    assert dw.fd.start_count(cid) == 1
    assert all(not c["State"]["Running"] for c in dw.fd.containers().values())


def test_adv_oom_on_docker_is_recorded_as_oom_evidence_with_its_exit_code(dw):
    dw.fd.workload(stdout=wl.lines_stdout(1), exit_code=137, oom=True)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    ev = first_end(dw, node)
    assert ev.get("oom") is True and ev.get("exit_code") == 137
    view = dw.s.wait(lambda: ic.causes(dw.s.get(node)) and dw.s.get(node), RESTART_WAIT, "a cause")
    assert "oom" in json.dumps(ic.causes(view)).lower(), ic.causes(view)


def test_adv_a_nonzero_exit_without_oom_is_not_called_oom(dw):
    dw.fd.workload(stdout=wl.lines_stdout(1), exit_code=137, oom=False)
    dw.s.start()
    node = dw.s.node({"lines": 1})
    ev = first_end(dw, node)
    assert ev.get("oom") is False and ev.get("exit_code") == 137
    view = dw.s.wait(lambda: ic.causes(dw.s.get(node)) and dw.s.get(node), RESTART_WAIT, "a cause")
    assert '"oom"' not in json.dumps(ic.causes(view)).lower() or all(
        c.get("oom") in (None, False) for c in ic.causes(view)), ic.causes(view)


def test_adv_steer_racing_an_exit_starts_one_new_container_and_never_restarts_the_old(tmp_path, monkeypatch):
    """IC-C16 / D-S3.2: steer is stop, then a new attempt. The container exits
    on its own just as the steer arrives."""
    s = s3.FakeDockerSched(tmp_path, monkeypatch)
    try:
        s.fd.workload(runs=True, stdout=s3.events_stdout())
        s.start()
        node = s.create()["id"]
        (old,) = s.wait(lambda: [c for c, v in s.fd.containers().items() if v["State"]["Running"]],
                        ic.WAIT, "a running container")
        run = s.wait(lambda: s.runs(node) and s.runs(node)[0], ic.WAIT, "the run")
        s.fd.workload(stdout=s3.events_stdout())
        s.fd.finish(old, code=0)
        reply = s.rpc("steer_run", {"run_id": run["run_id"], "message": "again"})
        view = s3.wait_done(s, node, RESTART_WAIT)
        assert view["state"] in ("done", "held"), view["state"]
        assert s.fd.start_count(old) == 1
        assert all(c["StartCount"] <= 1 for c in s.fd.containers().values())
        assert all(not c["State"]["Running"] for c in s.fd.containers().values())
        if reply.get("ok") and len(s.runs(node)) > 1:
            assert {r["executor"]["kind"] for r in s.runs(node)} == {"isolated"}
    finally:
        s.close()
