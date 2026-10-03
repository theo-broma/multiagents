"""LV-R1/R2: pid reuse and actual reaping beyond the C15 contract examples."""
import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import procs
from multiagents.runner import Runner
from test_c15_liveness_steer_hold import (  # noqa: F401
    AID, box, container_world, h, record_in_container, refused_for_liveness, w,
)


@pytest.mark.parametrize("wrapper_reused,agent_reused,expected", [
    (False, False, True), (True, False, True), (True, True, True),
])
def test_forged_start_files_cannot_make_live_processes_dead(box, wrapper_reused, agent_reused, expected):
    live = box.live_session_leader()
    box.record(live.pid, live.pid)
    start = int(procs.start_time(live.pid))
    for name, reused in (("wrapper.pid", wrapper_reused), ("container.pid", agent_reused)):
        (box.run_dir / (name + ".start")).write_text(f"{live.pid} {start + int(reused)}\n")
    assert box.executor.wrapper_verdict(AID) is expected


def test_forged_wrapper_start_without_an_agent_record_cannot_authorize_death(box):
    live = box.live_session_leader()
    box.record(live.pid)
    start = int(procs.start_time(live.pid)) + 1
    (box.run_dir / "wrapper.pid.start").write_text(f"{live.pid} {start}\n")
    assert box.executor.wrapper_verdict(AID) is True


def test_malformed_start_files_do_not_override_kernel_liveness(box):
    live = box.live_session_leader()
    box.record(live.pid, live.pid)
    for name in ("wrapper.pid.start", "container.pid.start"):
        (box.run_dir / name).write_text("incomplete\n")
    assert box.executor.wrapper_verdict(AID) is True


def test_recorded_dead_agent_does_not_hide_a_descendant_in_another_process_group(box):
    records = box.tmp / "descendant.pids"
    child_source = (
        "import os, subprocess, sys; "
        "p = subprocess.Popen(['sleep', '600'], preexec_fn=os.setpgrp); "
        "open(sys.argv[1], 'w').write('%d %d' % (os.getpid(), p.pid))"
    )
    leader_source = (
        "import os, subprocess, sys; "
        "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "preexec_fn=os.setpgrp)"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_source, child_source,
                               str(records)], start_new_session=True)
    leader.wait(timeout=10)
    agent, descendant = map(int, records.read_text().split())
    try:
        assert os.getsid(descendant) == leader.pid
        assert os.getpgid(descendant) not in (leader.pid, agent)
        box.record(leader.pid, agent)
        assert box.executor.wrapper_verdict(AID) is True
        assert box.executor.kill_detached(AID, grace=0)
        assert box.executor.wrapper_verdict(AID) is True
    finally:
        os.kill(descendant, signal.SIGKILL)


def test_probe_bookkeeping_failure_is_unknown(box, monkeypatch):
    live = box.live_session_leader()
    box.record(live.pid, live.pid)
    token = uuid.uuid4().hex
    occupied = Path(f"/tmp/multiagents-alive-{token}.pid")
    occupied.mkdir()
    monkeypatch.setattr("multiagents.executor.docker.uuid.uuid4",
                        lambda: SimpleNamespace(hex=token))
    try:
        assert box.executor.wrapper_alive(AID) is None
        assert box.executor.wrapper_verdict(AID) is None
    finally:
        occupied.rmdir()


def test_timed_out_probe_descendants_are_reaped(box):
    box.record(999999999, 999999998)
    log = box.hanging_probe()
    assert box.executor.wrapper_verdict(AID) is None
    pids = [int(token) for token in log.read_text().split()]
    assert len(pids) >= 3
    assert not [pid for pid in pids if Path(f"/proc/{pid}").exists()]


@pytest.mark.parametrize("answer", [False, None])
def test_stop_settles_recovered_cleanup_without_a_blocked_reason(w, monkeypatch, answer):
    g, fake = container_world(w, monkeypatch)

    async def refused():
        aid = await h.running_with_session(w, g)
        record_in_container(w, aid)
        assert refused_for_liveness(await w.server.steer_agent(aid, "steerhold: one"))
        return aid
    aid = asyncio.run(refused())
    record = w.node(aid).cleanup_hold
    assert not record["steer_cleanup"].get("blocked_reason")
    for owner in (record, record["steer_cleanup"]):
        owner["owner_pid"] = 999999999
    w.runner._release(aid)
    w.runner._holds.pop(aid)
    w.tree().update(aid, cleanup_hold=record)
    recovered = Runner(w.runner.paths, w.runner.config)
    monkeypatch.setattr(recovered, "_docker_for", lambda container: fake)
    fake.answer = answer
    stopped = asyncio.run(recovered.stop(aid))
    assert stopped["status"] == "cancelled"
    assert stopped["predecessor_death_confirmed"] is (answer is False)
    fake.answer = False
    steered = asyncio.run(recovered.steer(aid, "steerhold: retry"))
    assert steered["steered"] is True, steered
    assert g.spawns() == 2
