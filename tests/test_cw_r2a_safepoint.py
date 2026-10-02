"""CW-R2a (implementer's own checks): the handshake is host-only and fails
closed. Complements tests/test_cw_attack.py, whose unwritable-request case
places its blocker in the old `launch/` location."""

from types import SimpleNamespace

import pytest

from multiagents import driver, safepoint
from multiagents.paths import ProjectPaths, state_root
from multiagents.tree import Tree

SESSION = "cw-r2a-session"
CLI = 4300
SERVER = 4301


@pytest.fixture
def stop(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    paths.data.mkdir(parents=True)
    monkeypatch.setattr(safepoint.procs, "alive", lambda pid, start="": pid in {CLI, SERVER})
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda pid, ancestor: ancestor == CLI)
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: [])
    compaction = driver._AttachedCompaction.__new__(driver._AttachedCompaction)
    compaction.paths = paths
    compaction.cli = CLI
    compaction.context = {"MULTIAGENTS_SESSION_ID": SESSION}
    compaction.safe_point = 5
    compaction._state = lambda: "announced"
    compaction.cancelled = []
    compaction._cancel = lambda *args: compaction.cancelled.append(args)
    compaction._blocked = lambda *args: None
    return compaction


def register(paths, pid=SERVER):
    safepoint._write(paths, f"safepoint-server-{pid}.json", safepoint._sign(
        {"type": "registration", "pid": pid, "start": f"start-{pid}", "session": SESSION}))


def ack(paths, nonce, pid=SERVER, start=None):
    """An acknowledgement as the server signs it (CW-R2b)."""
    safepoint._write(paths, f"safepoint-server-{pid}.safe", safepoint._sign(
        {"type": "ack", "pid": pid, "start": start or f"start-{pid}", "nonce": nonce,
         "session": SESSION}))


def test_the_handshake_lives_under_the_state_root_not_the_project(stop):
    where = safepoint.directory(stop.paths)
    assert state_root() in where.parents
    assert stop.paths.data not in where.parents


def test_an_unwritable_request_cancels_the_stop(stop):
    register(stop.paths)
    name = f"safepoint-barrier-{safepoint._key(SESSION)}.json"
    (safepoint.directory(stop.paths) / name).mkdir()
    assert not stop._reach_safe_point("announced")
    assert stop.cancelled == [("safe_point_error",)]


def test_no_server_and_no_proc_is_not_a_yes(stop, monkeypatch):
    stop.safe_point = 0.3
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: None)
    assert not stop._reach_safe_point("announced")


def test_no_server_registered_and_proc_shows_none_proceeds(stop):
    assert stop._reach_safe_point("announced")


def test_an_unregistered_server_seen_in_proc_blocks_the_stop(stop, monkeypatch):
    stop.safe_point = 0.3
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: [SERVER] if pid == CLI else [])
    monkeypatch.setattr(safepoint, "_is_root_server", lambda pid, session: pid == SERVER)
    assert not stop._reach_safe_point("announced")
    assert stop.cancelled == [("safe_point_timeout",)]


def test_a_bound_ack_from_every_registered_server_proceeds(stop, monkeypatch):
    register(stop.paths)
    request = safepoint.request

    def request_then_ack(*args):
        nonce = request(*args)
        ack(stop.paths, nonce)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_ack)
    assert stop._reach_safe_point("announced")
    record = safepoint._read(stop.paths, f"safepoint-barrier-{safepoint._key(SESSION)}.json")
    assert record["state"] == "commit"


def test_an_ack_with_the_wrong_start_time_does_not_count(stop):
    register(stop.paths)
    nonce = safepoint.request(stop.paths, SESSION, CLI)
    ack(stop.paths, nonce, start="start-recycled")
    assert not safepoint.acknowledged(stop.paths, SERVER, nonce)


@pytest.mark.parametrize("driver_alive", [True, False])
def test_a_committed_latch_expires_only_once_its_driver_is_gone(stop, monkeypatch,
                                                                driver_alive):
    DRIVER = 4399
    clock = [100.0]
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 100.0))
    live = {CLI, SERVER, DRIVER}
    monkeypatch.setattr(safepoint.procs, "alive", lambda pid, start="": pid in live)
    register(stop.paths)
    monkeypatch.setattr(safepoint.os, "getpid", lambda: DRIVER)
    nonce = safepoint.request(stop.paths, SESSION, CLI)
    monkeypatch.setattr(safepoint.os, "getpid", lambda: SERVER)
    gate, acked = safepoint.Gate(), []
    safepoint.observe(stop.paths, SESSION, gate, acked, 10)
    assert gate.closed and safepoint.acknowledged(stop.paths, SERVER, nonce)
    assert safepoint.settle(stop.paths, SESSION, nonce, "commit")
    safepoint.observe(stop.paths, SESSION, gate, acked, 10)
    assert gate.committed
    if not driver_alive:
        live.discard(DRIVER)
    clock[0] += 10 + safepoint.COMMITTED_GRACE_SECONDS + 1
    safepoint.observe(stop.paths, SESSION, gate, acked, 10)
    if driver_alive:
        assert gate.closed, "expired while the driver could still stop the CLI"
    else:
        assert not gate.closed
        assert not safepoint.acknowledged(stop.paths, SERVER, nonce)


def test_an_unreadable_descendant_makes_the_listing_unknown(monkeypatch):
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: [7] if pid == CLI else [])
    monkeypatch.setattr(safepoint, "_is_root_server", lambda pid, session: None)
    assert safepoint.unregistered(SESSION, CLI, []) is None


def test_a_server_starting_under_a_request_starts_closed(stop, monkeypatch):
    monkeypatch.setattr(safepoint.os, "getpid", lambda: SERVER)
    safepoint.request(stop.paths, SESSION, CLI)
    gate = safepoint.Gate()
    safepoint.observe(stop.paths, SESSION, gate, [], 60)
    with pytest.raises(RuntimeError, match="stopping"):
        gate.enter("a launch")


def test_unknown_listing_blocks_even_with_every_registered_server_acked(stop, monkeypatch):
    """Review r2 #2."""
    stop.safe_point = 0.3
    register(stop.paths)
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: None)
    request = safepoint.request

    def request_then_ack(*args):
        nonce = request(*args)
        ack(stop.paths, nonce)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_ack)
    assert not stop._reach_safe_point("announced")
    assert stop.cancelled == [("safe_point_timeout",)]


def test_an_unreadable_parent_makes_descendants_unknown(monkeypatch):
    """Review r2 #3: not an empty answer."""
    import os
    from multiagents import procs
    real = procs._stat

    def unreadable(pid):
        if pid == os.getpid():
            raise PermissionError("no")
        return real(pid)

    monkeypatch.setattr(procs, "_stat", unreadable)
    assert procs.descendants(1) is None
    assert procs.descends_from(os.getpid(), 1) is None


def test_an_unreadable_start_time_does_not_validate_an_ack(stop, monkeypatch):
    """Review r2 #4: pid reuse cannot be ruled out without it."""
    register(stop.paths)
    nonce = safepoint.request(stop.paths, SESSION, CLI)
    ack(stop.paths, nonce)
    assert safepoint.acknowledged(stop.paths, SERVER, nonce)
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: "")
    assert not safepoint.acknowledged(stop.paths, SERVER, nonce)


@pytest.mark.parametrize("before,after,code,delivered", [
    ((True, (1, 10)), (True, (1, 10)), 20, False),   # launch script failed first
    ((True, (1, 10)), (True, (1, 10)), 0, False),    # exit 0, nothing recorded
    ((True, (0, 0)), (True, (1, 40)), 1, True),      # the CLI wrote the turn
    ((True, (1, 10)), (True, (1, 90)), None, True),
    ((True, None), (True, (1, 90)), 0, False),       # unknown before
    ((False, None), (False, None), 0, True),         # no transcript: clean exit
    ((False, None), (False, None), 20, False),
])
def test_the_return_message_is_dropped_only_once_delivery_is_established(
        before, after, code, delivered):
    """Review r2 #5."""
    assert driver._delivered(before, after, code) is delivered


def test_a_zombie_is_not_alive():
    """Review r2 #6: exited, not yet reaped."""
    import subprocess
    import time
    from pathlib import Path
    from multiagents import procs
    child = subprocess.Popen(["true"])
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            state = Path(f"/proc/{child.pid}/stat").read_text().rpartition(")")[2].split()[0]
            if state == "Z":
                break
            time.sleep(0.02)
        assert state == "Z"
        assert procs.alive(child.pid, procs.start_time(child.pid))
        assert not procs.living(child.pid, procs.start_time(child.pid))
        assert not safepoint._alive({"pid": child.pid, "start": procs.start_time(child.pid)})
    finally:
        child.wait()


def test_the_unattended_message_is_rendered_at_delivery(tmp_path):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    from multiagents.tree import Node
    tree.add(Node(id="ag-run001", agent="coder", provider="p", model="m", parent=None,
                  depth=1, status="running", task="t", session=SESSION))
    from multiagents import compact_return
    snap = compact_return.snapshot(tree, SESSION, 9000)
    driver._keep_return_message(paths, "orchestrator", SESSION, snapshot=snap,
                                outcome={"code": 0, "detail": "9000 -> 900", "unsupported": 64})
    tree.set_status("ag-run001", "done")          # after the compaction
    note = driver._peek_return_message(paths, "orchestrator", SESSION, tree)
    assert "finished during compaction" in note
    assert driver._peek_return_message(paths, "orchestrator", SESSION, tree), "peek keeps it"
    driver._drop_return_message(paths, "orchestrator")
    assert driver._peek_return_message(paths, "orchestrator", SESSION, tree) == ""


def test_a_turn_whose_launch_failed_before_the_cli_keeps_the_message(tmp_path, monkeypatch):
    """Review r2 #5, end to end: the turn after the compaction exits 20 without
    the CLI writing anything (a launch script that failed first); the message
    is not lost, and the next turn carries it."""
    import re
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_phase0_unattended_compact as uc
    from multiagents.tree import Node

    lp = uc.Loop(tmp_path, monkeypatch)
    lp.reading(uc.OVER)
    lp.tree.add(Node(id="ag-run001", agent="coder", provider="fakeprov", model="m",
                     parent=None, depth=1, status="running", task="t", session=uc.SID))
    lp.run([uc.productive(), {"exit": 20}, uc.productive(1_000)], max_turns=3)
    nudges = [e["env"].get("MULTIAGENTS_NUDGE", "") for e in lp.fake.calls("launch")]
    marker = re.compile(r"compacted by the driver", re.IGNORECASE)
    assert len(nudges) == 3
    assert marker.search(nudges[1]) and marker.search(nudges[2]), (
        "the message was dropped by a turn that never reached the CLI")


def test_delivery_is_read_from_the_host_transcript_in_a_docker_project(tmp_path, monkeypatch):
    """Review r3 (CW-R4): the orchestrator's CLI runs on the host and writes the
    host transcript even when the project's agents run in docker with a
    container profile that keeps theirs elsewhere. Delivery is read from the
    host one: two clean turns deliver the message once, then it is gone."""
    import re
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_phase0_unattended_compact as uc
    from multiagents.tree import Node

    private = tmp_path / "container-profile"
    agents_executor = SimpleNamespace(
        kind="docker", container="multiagents-test",
        private_state=lambda name="": {Path.home() / ".claude": private},
        vault_state=lambda name="": {}, auth_proxy_enabled=lambda: False,
        container_home=lambda: Path.home(),
        # Every transcript an agent writes lands in the private profile.
        host_path=lambda path: private / path.name)
    lp = uc.Loop(tmp_path, monkeypatch)
    lp.reading(uc.OVER)
    lp.tree.add(Node(id="ag-run001", agent="coder", provider="fakeprov", model="m",
                     parent=None, depth=1, status="running", task="t", session=uc.SID))
    lp.fake.control(transcript=str(lp.transcript), events=str(lp.paths.events_file),
                    turns=[uc.productive(), uc.productive(1_000), uc.productive(1_000)],
                    compact={"exit": 0, "stdout": "9000 -> 900 tokens\n"})
    driver._supervise(lp.paths, lp.config, "orchestrator", lp.spec, lp.provider,
                      agents_executor, dict(lp.context), 3)
    nudges = [e["env"].get("MULTIAGENTS_NUDGE", "") for e in lp.fake.calls("launch")]
    marker = re.compile(r"compacted by the driver", re.IGNORECASE)
    assert len(nudges) == 3
    assert not marker.search(nudges[0])
    assert marker.search(nudges[1]), "the compaction happened and was not reported"
    assert not marker.search(nudges[2]), "delivered twice: delivery read the wrong transcript"
    assert driver._peek_return_message(lp.paths, "orchestrator", uc.SID) == ""
