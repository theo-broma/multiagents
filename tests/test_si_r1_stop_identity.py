"""SI-R1: stopping requires the record, kernel and socket to agree.

Record field assumptions: project/nonce extend the existing pid/pid_start.
Refusal wording is deliberately unconstrained, but must explain the refusal.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.world import World
from multiagents import procs, scheduler

BOUND = 3


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.write_config()
    try:
        yield w
    finally:
        w.close()


def record(world):
    return world.paths.scheduler / "process.json"


def refuse(result):
    # No spelling or prose has been specified for the new refusal message.
    def has_message(value):
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(has_message(item) for item in value.values())
        if isinstance(value, list):
            return any(has_message(item) for item in value)
        return False

    assert has_message(result), f"refusal needs a message: {result}"


def test_si_r1_live_unrelated_pid_is_not_stop_authority(world):
    with subprocess.Popen(["sleep", "60"]) as decoy:
        try:
            record(world).parent.mkdir(parents=True, exist_ok=True)
            record(world).write_text(json.dumps({
                "project": str(world.root.resolve()), "nonce": "stale-instance",
                "pid": decoy.pid, "pid_start": procs.start_time(decoy.pid)}))
            result = scheduler.stop(world.root)
            assert procs.living(decoy.pid), "stop signalled an unrelated process"
            refuse(result)
        finally:
            if decoy.poll() is None:
                decoy.kill()
            decoy.wait(timeout=BOUND)


@pytest.mark.parametrize("mismatch", ["pid_start", "project", "nonce"])
def test_si_r1_identity_mismatch_is_refused_and_scheduler_survives(world, mismatch):
    pid = world.start_scheduler()
    info = json.loads(record(world).read_text())
    info.update(project=str(world.root.resolve()), nonce=info.get("nonce", "instance"))
    info[mismatch] = {"pid_start": str(int(info["pid_start"]) + 1),
                      "project": str(world.tmp / "other-project"),
                      "nonce": "another-schedulers-instance"}[mismatch]
    record(world).write_text(json.dumps(info))
    result = scheduler.stop(world.root)
    assert procs.living(pid), f"stop signalled a scheduler despite mismatched {mismatch}"
    assert world.sock_answers(), "the mismatched scheduler must remain available"
    refuse(result)


def test_si_r1_process_record_identifies_the_resolved_project_and_instance(world):
    alias = world.tmp / "project-alias"
    alias.symlink_to(world.root, target_is_directory=True)
    world.root = alias
    pid = world.start_scheduler()
    info = json.loads(record(world).read_text())
    assert info.get("project") == str(world.root.resolve()), info
    assert isinstance(info.get("nonce"), str) and info["nonce"].strip(), info
    assert info.get("pid") == pid
    assert str(info.get("pid_start")) == procs.start_time(pid)
    first_nonce = info["nonce"]
    assert scheduler.stop(world.root).get("stopped") is True
    world.start_scheduler()
    assert json.loads(record(world).read_text())["nonce"] != first_nonce


@pytest.mark.parametrize("surface", ["api", "cli"])
def test_si_r1_normal_stop_still_works(world, surface):
    pid = world.start_scheduler()
    if surface == "api":
        result = scheduler.stop(world.root)
    else:
        response = world.cli("scheduler", "stop")
        assert response.returncode == 0, response.stderr
        result = json.loads(response.stdout)
    assert result.get("stopped") is True, result
    assert not procs.living(pid)
    assert not world.sock_answers()


@pytest.mark.parametrize("missing", ["project", "nonce", "both"])
def test_si_r1_old_format_record_is_refused_fail_closed(world, missing):
    """A record without project or nonce (older scheduler, test fake) is refused."""
    pid = world.start_scheduler()
    info = json.loads(record(world).read_text())
    for key in ("project", "nonce") if missing == "both" else (missing,):
        info.pop(key, None)
    record(world).write_text(json.dumps(info))
    result = scheduler.stop(world.root)
    assert procs.living(pid), f"stop signalled a scheduler with a record lacking {missing}"
    assert world.sock_answers(), "the scheduler must remain available"
    refuse(result)
