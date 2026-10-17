"""SI-R2: observations of foreign processes never grant teardown authority."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from nc_fixture.world import World
from nc_harness import Sched
from multiagents import procs
from multiagents.scheduler import owner

BOUND = 3


def test_si_r2_worlds_sharing_state_root_and_slug_do_not_stop_each_other(tmp_path, monkeypatch):
    a = World(tmp_path / "a", monkeypatch)
    b = World(tmp_path / "b", monkeypatch)
    # A's socket resolves to B. Project ownership remains distinct, even
    # when an erroneous slug aliases the same state/socket location.
    a.paths.slug = b.paths.slug
    monkeypatch.setattr(a, "root_token", b.root_token)
    try:
        pid = b.start_scheduler()
        assert a.sock == b.sock
        assert a.status()["pid"] == pid
        a.close()
        assert procs.living(pid), "closing A killed B's scheduler"
        assert b.sock_answers()
        a.close()  # repeated cleanup has the same ownership boundary
        assert b.sock_answers()
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("harness", [World, Sched])
@pytest.mark.parametrize("marker", ["other-owner", "other-project"])
def test_si_r2_foreign_marked_decoy_survives_teardown(tmp_path, monkeypatch, harness, marker):
    fixture = harness(tmp_path, monkeypatch)
    with subprocess.Popen(["sleep", "60"], env=owner.unmarked_env()) as foreign_owner:
        project = fixture.root if marker == "other-owner" else tmp_path / "elsewhere"
        owner_pid = foreign_owner.pid if marker == "other-owner" else os.getpid()
        env = dict(owner.unmarked_env(), **owner.mark_env(
            owner_pid, procs.start_time(owner_pid), project))
        with subprocess.Popen(["sleep", "60"], env=env) as decoy:
            try:
                if harness is World:
                    # A provider PID file is just as untrusted as RPC status.
                    monkeypatch.setattr(fixture.fx, "pids", lambda: [decoy.pid])
                    monkeypatch.setattr(fixture, "status", lambda: {"pid": decoy.pid})
                else:
                    monkeypatch.setattr(fixture, "rpc", lambda *args, **kwargs: {
                        "ok": True, "result": {"pid": decoy.pid}})
                    assert fixture.pid() == decoy.pid
                fixture.close()
                assert procs.living(decoy.pid), "teardown killed a foreign marked process"
                fixture.close()
                assert procs.living(decoy.pid)
            finally:
                fixture.close()
                if decoy.poll() is None:
                    decoy.kill()
                decoy.wait(timeout=BOUND)
                foreign_owner.kill()
                foreign_owner.wait(timeout=BOUND)


@pytest.mark.parametrize("harness", [World, Sched])
def test_si_r2_cli_started_scheduler_is_owned_and_reaped(tmp_path, monkeypatch, harness):
    fixture = harness(tmp_path, monkeypatch)
    try:
        if harness is World:
            fixture.write_config()
        response = fixture.cli("scheduler", "start")
        assert response.returncode == 0, (response.stdout, response.stderr)
        pid = fixture.status()["pid"] if harness is World else fixture.pid()
        fixture.close()
        assert not procs.living(pid), "CLI-launched scheduler escaped owned teardown"
    finally:
        fixture.close()
