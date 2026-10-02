"""CW robustness regressions: writable handshake, stop races, durable returns.

No real provider or signal is used. Process identity is a deterministic fake
host /proc; handshake reads and writes are the production filesystem code.
"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from multiagents import compact_return, driver, safepoint
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Tree


SESSION = "cw-attack-session"
CLI = 4200
SERVER = 4201


def register_as(paths, pid, monkeypatch):
    """A root server's registration, made through the public (signing) path."""
    with monkeypatch.context() as patch:
        patch.setattr(safepoint.os, "getpid", lambda: pid)
        safepoint.register(paths, SESSION)


@pytest.fixture
def stop(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    paths.data.mkdir(parents=True)
    monkeypatch.setattr(safepoint.procs, "alive", lambda pid, start="": pid in {CLI, SERVER, SERVER + 1})
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda pid, ancestor: ancestor == CLI)
    register_as(paths, SERVER, monkeypatch)
    compaction = driver._AttachedCompaction.__new__(driver._AttachedCompaction)
    compaction.paths = paths
    compaction.cli = CLI
    compaction.context = {"MULTIAGENTS_SESSION_ID": SESSION}
    compaction.safe_point = 0.001
    compaction._state = lambda: "announced-transcript"
    compaction._cancel = lambda *args: None
    compaction._blocked = lambda *args: None
    return compaction


def barrier(paths):
    return safepoint._read(paths, f"safepoint-barrier-{safepoint._key(SESSION)}.json")


def test_agent_written_ack_cannot_authorize_stopping_an_inflight_launch(stop, monkeypatch):
    """The agent needs only files already exposed under launch/, not /proc."""
    gate = safepoint.Gate()
    launch = gate.enter("launch held between claim and pid record")
    request = safepoint.request

    def request_then_agent_writes_ack(*args):
        nonce = request(*args)
        # These values are all readable in launch/. Deliberately omit start:
        # the driver accepts even less than the honest server writes.
        registration = safepoint._read(stop.paths, f"safepoint-server-{SERVER}.json")
        seen = barrier(stop.paths)
        safepoint._write(stop.paths, f"safepoint-server-{SERVER}.safe",
                         {"pid": registration["pid"], "nonce": seen["nonce"]})
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_agent_writes_ack)
    try:
        may_stop = stop._reach_safe_point("announced-transcript")
        assert not may_stop, "agent-written ack authorized CLI stop while launch is in flight"
    finally:
        launch.end()


def test_unwritable_handshake_does_not_authorize_cli_stop(stop):
    # An agent can leave a directory at the barrier's filename. The actual
    # request write then fails while the live server registration remains.
    name = f"safepoint-barrier-{safepoint._key(SESSION)}.json"
    (safepoint.directory(stop.paths) / name).mkdir(parents=True)
    assert not stop._reach_safe_point("announced-transcript"), "failed request was treated as a safe point"


def test_new_input_wins_even_when_all_servers_ack(stop, monkeypatch):
    request = safepoint.request

    def request_then_ack_and_user_action(*args):
        nonce = request(*args)
        safepoint._write(stop.paths, f"safepoint-server-{SERVER}.safe",
                         {"pid": SERVER, "start": f"start-{SERVER}", "nonce": nonce})
        stop._state = lambda: "new-user-message"
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_ack_and_user_action)
    assert not stop._reach_safe_point("announced-transcript"), "committed stop despite a new user message"
    assert barrier(stop.paths)["state"] != "commit"


def test_second_root_server_registered_before_commit_is_not_missed(stop, monkeypatch):
    """A second server comes up while the driver checks the first one's ack."""
    second_gate = safepoint.Gate()
    request = safepoint.request
    acknowledged = safepoint.acknowledged

    def request_and_first_ack(*args):
        nonce = request(*args)
        safepoint._write(stop.paths, f"safepoint-server-{SERVER}.safe",
                         {"pid": SERVER, "nonce": nonce})
        return nonce

    def first_ack_then_second_server(paths, pid, nonce):
        ready = acknowledged(paths, pid, nonce)
        if pid == SERVER:
            safepoint._write(paths, f"safepoint-server-{SERVER + 1}.json",
                             {"pid": SERVER + 1, "start": f"start-{SERVER + 1}", "session": SESSION})
        return ready

    monkeypatch.setattr(safepoint, "request", request_and_first_ack)
    monkeypatch.setattr(safepoint, "acknowledged", first_ack_then_second_server)
    with second_gate.enter("second server's first launch"):
        may_stop = stop._reach_safe_point("announced-transcript")
        assert not may_stop, "committed with a newly registered root server still admitting launches"
        assert not second_gate.closed


def test_exit_before_next_unattended_cli_starts_preserves_return_message(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    paths.data.mkdir(parents=True)
    note = "Driver compacted this session; collect_agent ag-unseen before deciding."
    driver._keep_return_message(paths, "orchestrator", SESSION, note)
    monkeypatch.setattr(driver, "_orchestrator_hold", lambda *args: None)
    monkeypatch.setattr(driver.scripts, "exec_action", lambda *args, **kwargs: (["fake-cli"], {}))

    def exit_before_launch(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(driver.subprocess, "Popen", exit_before_launch)
    code = driver._supervise(paths, SimpleNamespace(), "orchestrator",
                             SimpleNamespace(provider="fake"), None, None,
                             {"MULTIAGENTS_SESSION_ID": SESSION, "MULTIAGENTS_RESUME": "1"}, 1)
    assert code == 0
    assert driver._take_return_message(paths, "orchestrator", SESSION) == note, \
        "the driver deleted the checklist before any CLI received it"


def test_cancelled_ack_reopens_admission_and_stale_ack_does_not_match(stop, monkeypatch):
    monkeypatch.setattr(safepoint.os, "getpid", lambda: SERVER)
    nonce = safepoint.request(stop.paths, SESSION, CLI)
    # Here the fake requester must also be a live host process.
    gate, acked = safepoint.Gate(), []
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert gate.closed and safepoint.acknowledged(stop.paths, SERVER, nonce)
    safepoint.settle(stop.paths, SESSION, nonce, "cancelled")
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert not gate.closed
    with gate.enter("new launch"):
        assert gate.transitions == 1
    new_nonce = safepoint.request(stop.paths, SESSION, CLI)
    assert not safepoint.acknowledged(stop.paths, SERVER, new_nonce)


def test_crashed_requester_does_not_close_admission(stop, monkeypatch):
    monkeypatch.setattr(safepoint.os, "getpid", lambda: SERVER)
    safepoint.request(stop.paths, SESSION, CLI)
    monkeypatch.setattr(safepoint.procs, "alive", lambda pid, start="": pid == CLI)
    gate = safepoint.Gate()
    safepoint.observe(stop.paths, SESSION, gate, [], 60)
    assert not gate.closed


def test_ack_waits_for_all_admitted_transitions_and_the_quiet_period(stop, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 100.0))
    monkeypatch.setattr(safepoint.os, "getpid", lambda: SERVER)
    nonce = safepoint.request(stop.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    first, second = gate.enter("launch"), gate.enter("steer")
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert not gate.closed
    first.end()
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert not gate.closed
    second.end()
    clock[0] += 0.299
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert not gate.closed and not safepoint.acknowledged(stop.paths, SERVER, nonce)
    clock[0] += 0.002
    safepoint.observe(stop.paths, SESSION, gate, acked, 60)
    assert gate.closed and safepoint.acknowledged(stop.paths, SERVER, nonce)


def test_unknown_proc_ancestry_and_two_registered_servers_require_both_acks(stop, monkeypatch):
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda *args: None)
    register_as(stop.paths, SERVER + 1, monkeypatch)
    request = safepoint.request

    def request_and_ack_only_first(*args):
        nonce = request(*args)
        safepoint._write(stop.paths, f"safepoint-server-{SERVER}.safe",
                         {"pid": SERVER, "nonce": nonce})
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_ack_only_first)
    assert not stop._reach_safe_point("announced-transcript")
    assert barrier(stop.paths)["state"] == "cancelled"


def test_retry_waiting_at_gate_is_cancelled_without_admitting_a_launch():
    async def scenario():
        gate = safepoint.Gate()
        gate.close("committed-stop", committed=True)
        admitted = []

        async def retry():
            with await gate.enter_when_open():
                admitted.append(True)

        task = asyncio.create_task(retry())
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not admitted and gate.transitions == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["launch", "steer", "consult", "answer_question"])
def test_every_public_launch_transition_is_refused_after_ack(operation):
    run = Runner.__new__(Runner)
    run.gate.close("acked-stop", committed=False)

    async def invoke():
        if operation == "launch":
            return await run.start("worker", "task")
        if operation == "steer":
            return await run.steer("ag-worker", "task")
        if operation == "consult":
            return await run._consult_turn("advisor", None, "task", None)
        return await run.answer_question("q-worker", "answer")

    with pytest.raises(RuntimeError, match="stopping"):
        asyncio.run(invoke())
    assert run.gate.transitions == 0


def test_generated_large_ids_keep_message_cap_and_checklist(tmp_path):
    rng = random.Random(647544)
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    for count in (0, 1, 2, 200):
        nodes = {"ag-" + "x" * rng.randrange(1, 10000) + str(i):
                 {"parent": None, "status": "running", "agent": "coder", "session": SESSION}
                 for i in range(count)}
        fake = SimpleNamespace(read=lambda: {"nodes": nodes}, events_path=tree.events_path,
                               unseen=lambda session: [SimpleNamespace(id=i) for i in nodes],
                               open_questions=lambda: [{"id": i} for i in nodes],
                               open_tickets=lambda: [])
        snap = compact_return.snapshot(fake, SESSION, 9000)
        note = compact_return.message(fake, SESSION, snap,
                                      {"code": 0, "detail": "9000 -> 900", "unsupported": 64})
        assert len(note) <= 4000
        assert compact_return.CHECKLIST in note
