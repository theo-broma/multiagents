"""CW-R2b (implementer's checks): the safe-point handshake is authenticated.

Forged MACs, deletion, replay after cancellation, tampering, an omitted
participant, and the key reaching no environment but the orchestrator CLI's.
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import driver, safepoint, scripts
from multiagents.config import AgentSpec, Config
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner, server_env

SESSION = "cw-r2b-session"
DRIVER, CLI, SERVER, OTHER = 4500, 4501, 4502, 4503
KEY = safepoint.KEY_ENV


@pytest.fixture
def hs(tmp_path, monkeypatch):
    """A handshake between a fake driver, CLI and server(s), real file I/O."""
    paths = ProjectPaths(tmp_path / "project")
    paths.data.mkdir(parents=True)
    live = {DRIVER, CLI, SERVER, OTHER}
    me = [DRIVER]
    monkeypatch.setattr(safepoint.os, "getpid", lambda: me[0])
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(safepoint.procs, "living", lambda pid, start="": pid in live
                        and (not start or start == f"start-{pid}"))
    monkeypatch.setattr(safepoint.procs, "alive", lambda pid, start="": pid in live)
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda pid, a: a == CLI)
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid: [])

    def act_as(pid):
        me[0] = pid
    compaction = driver._AttachedCompaction.__new__(driver._AttachedCompaction)
    compaction.paths, compaction.cli = paths, CLI
    compaction.context = {"MULTIAGENTS_SESSION_ID": SESSION}
    compaction.safe_point = 0.3
    compaction._state = lambda: "announced"
    compaction._cancel = lambda *a: None
    compaction._blocked = lambda *a: None
    return SimpleNamespace(paths=paths, act_as=act_as, live=live, stop=compaction)


def barrier_name():
    return f"safepoint-barrier-{safepoint._key(SESSION)}.json"


def server_answers(hs, gate, acked, pid=SERVER):
    hs.act_as(pid)
    safepoint.observe(hs.paths, SESSION, gate, acked, 60)
    hs.act_as(DRIVER)


# ------------------------------------------------------------ forged MACs --

def test_a_forged_ack_does_not_count(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    nonce = safepoint.request(hs.paths, SESSION, CLI)
    forged = {"type": "ack", "pid": SERVER, "start": f"start-{SERVER}", "nonce": nonce,
              "session": SESSION, "mac": "0" * 64}
    safepoint._write(hs.paths, f"safepoint-server-{SERVER}.safe", forged)
    assert not safepoint.acknowledged(hs.paths, SERVER, nonce)
    unsigned = {k: v for k, v in forged.items() if k != "mac"}
    safepoint._write(hs.paths, f"safepoint-server-{SERVER}.safe", unsigned)
    assert not safepoint.acknowledged(hs.paths, SERVER, nonce)


def test_a_record_signed_with_another_key_does_not_count(hs, monkeypatch):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    nonce = safepoint.request(hs.paths, SESSION, CLI)
    real = safepoint._secret
    monkeypatch.setattr(safepoint, "_secret", os.urandom(32))
    hs.act_as(SERVER)
    safepoint.acknowledge(hs.paths, nonce, SESSION)        # an agent with its own key
    hs.act_as(DRIVER)
    monkeypatch.setattr(safepoint, "_secret", real)
    assert not safepoint.acknowledged(hs.paths, SERVER, nonce)


def test_a_forged_request_does_not_close_admission(hs):
    record = {"type": "request", "session": SESSION, "nonce": "f" * 32, "state": "drain",
              "at": 1e12, "requester": {"pid": DRIVER, "start": f"start-{DRIVER}"},
              "cli": {"pid": CLI, "start": f"start-{CLI}"}, "mac": "1" * 64}
    safepoint._write(hs.paths, barrier_name(), record)
    gate = safepoint.Gate()
    server_answers(hs, gate, [])
    assert not gate.closed


def test_a_forged_registration_is_not_listed(hs):
    safepoint._write(hs.paths, f"safepoint-server-{SERVER}.json",
                     {"type": "registration", "pid": SERVER, "start": f"start-{SERVER}",
                      "session": SESSION, "mac": "2" * 64})
    assert safepoint.servers(hs.paths, SESSION, CLI) == []


# ---------------------------------------------------------------- deletion --

def test_deleting_the_request_never_reopens_an_acknowledged_server(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed
    (safepoint.directory(hs.paths) / barrier_name()).unlink()
    server_answers(hs, gate, acked)
    assert gate.closed, "a deleted request reopened admission"
    (safepoint.directory(hs.paths) / barrier_name()).write_text("{not json")
    server_answers(hs, gate, acked)
    assert gate.closed, "a malformed request reopened admission"


def test_deleting_a_registration_does_not_make_the_server_absent(hs, monkeypatch):
    """The server is still there in /proc: the stop is blocked, not taken."""
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    (safepoint.directory(hs.paths) / f"safepoint-server-{SERVER}.json").unlink()
    monkeypatch.setattr(safepoint.procs, "descendants",
                        lambda pid: [SERVER] if pid == CLI else [])
    monkeypatch.setattr(safepoint, "_is_root_server", lambda pid, session: pid == SERVER)
    assert not hs.stop._reach_safe_point("announced")


# ----------------------------------------------------------------- replay --

def test_an_old_ack_replayed_after_cancellation_does_not_authorise(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    first = safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    old_ack = (safepoint.directory(hs.paths) / f"safepoint-server-{SERVER}.safe").read_bytes()
    assert safepoint.settle(hs.paths, SESSION, first, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed
    second = safepoint.request(hs.paths, SESSION, CLI)
    (safepoint.directory(hs.paths) / f"safepoint-server-{SERVER}.safe").write_bytes(old_ack)
    assert not safepoint.acknowledged(hs.paths, SERVER, second)


def test_an_old_request_replayed_after_cancellation_does_not_close(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    nonce = safepoint.request(hs.paths, SESSION, CLI)
    old = (safepoint.directory(hs.paths) / barrier_name()).read_bytes()
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    safepoint.settle(hs.paths, SESSION, nonce, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed
    (safepoint.directory(hs.paths) / barrier_name()).write_bytes(old)
    server_answers(hs, gate, acked)
    assert not gate.closed, "a cancelled request, replayed, closed admission again"


def test_a_stale_signed_request_does_not_close_a_fresh_server(hs, monkeypatch):
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    later = safepoint.time.time() + 3600
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        time=lambda: later, monotonic=lambda: 10_000.0))
    gate = safepoint.Gate()
    server_answers(hs, gate, [])
    assert not gate.closed


# --------------------------------------------------------------- tampering --

@pytest.mark.parametrize("field,value", [
    ("type", "cancel"), ("state", "cancelled"), ("nonce", "a" * 32),
    ("session", "other"), ("cli", {"pid": OTHER, "start": f"start-{OTHER}"})])
def test_a_tampered_barrier_is_ignored(hs, field, value):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed
    path = safepoint.directory(hs.paths) / barrier_name()
    record = json.loads(path.read_text())
    path.write_text(json.dumps({**record, field: value}))
    server_answers(hs, gate, acked)
    assert gate.closed, f"a barrier with {field} tampered reopened admission"


def test_a_tampered_ack_does_not_count(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    old = safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    safepoint.settle(hs.paths, SESSION, old, "cancelled")
    server_answers(hs, gate, acked)
    hs.act_as(SERVER)
    safepoint.acknowledge(hs.paths, old, SESSION)
    hs.act_as(DRIVER)
    new = safepoint.request(hs.paths, SESSION, CLI)
    path = safepoint.directory(hs.paths) / f"safepoint-server-{SERVER}.safe"
    path.write_text(json.dumps({**json.loads(path.read_text()), "nonce": new}))
    assert not safepoint.acknowledged(hs.paths, SERVER, new)


# ------------------------------------------------------- omitted participant --

def test_a_registered_server_that_did_not_ack_blocks_the_stop(hs, monkeypatch):
    for pid in (SERVER, OTHER):
        hs.act_as(pid)
        safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    request = safepoint.request

    def request_and_first_answers(*args):
        nonce = request(*args)
        hs.act_as(SERVER)
        safepoint.acknowledge(hs.paths, nonce, SESSION)
        hs.act_as(DRIVER)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_first_answers)
    assert not hs.stop._reach_safe_point("announced")


def test_every_registered_server_acked_proceeds(hs, monkeypatch):
    for pid in (SERVER, OTHER):
        hs.act_as(pid)
        safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    request = safepoint.request

    def request_and_all_answer(*args):
        nonce = request(*args)
        for pid in (SERVER, OTHER):
            hs.act_as(pid)
            safepoint.acknowledge(hs.paths, nonce, SESSION)
        hs.act_as(DRIVER)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_all_answer)
    assert hs.stop._reach_safe_point("announced")


def test_cancellation_reopens_and_driver_death_reopens(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed
    hs.live.discard(DRIVER)
    server_answers(hs, gate, acked)
    assert not gate.closed


# ------------------------------------------------------------ key leakage --

@pytest.fixture
def key_everywhere(monkeypatch):
    monkeypatch.setenv(KEY, "ab" * 32)
    return "ab" * 32


def test_the_server_takes_the_key_out_of_its_environment(key_everywhere, monkeypatch):
    monkeypatch.setattr(safepoint, "_secret", None)
    safepoint.adopt_key()
    assert KEY not in os.environ
    assert safepoint._secret == bytes.fromhex(key_everywhere)


def test_no_key_in_agent_environments(key_everywhere):
    from multiagents.executor.base import build_env
    env = build_env(passthrough=[KEY, f"{KEY}=literal"], blocked=[], home=None,
                    identity={KEY: "identity", "MULTIAGENTS_AGENT_ID": "ag-x"})
    assert KEY not in env


def test_no_key_in_a_nested_server_environment(key_everywhere):
    assert KEY not in server_env({KEY: key_everywhere, "PATH": "/bin"}, "ag-x")


def test_no_key_in_provider_action_environments(key_everywhere):
    provider = SimpleNamespace(bin="sh", bin_search=[], env={KEY: "overlay"})
    env = scripts.build_env("p", provider, None, {KEY: "extra"})
    assert KEY not in env


def test_no_key_in_a_launched_agent_with_passthrough_and_overlay(tmp_path, key_everywhere,
                                                               monkeypatch):
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    git = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, env=git)
    config = Config(project={"security": {"env_passthrough": [KEY]}},
                    providers={"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 0"]},
                                     "env": {KEY: "overlay"}}},
                    agents={"worker": AgentSpec("worker", "p", "m")},
                    models={}, instruction_dirs=[])
    runner = Runner(paths, config)
    seen = {}

    async def capture(self, argv, cwd, env, **kwargs):
        seen.update(env)
        raise RuntimeError("captured")

    monkeypatch.setattr(type(runner.executor()), "start", capture)
    asyncio.run(runner.start("worker", "go"))          # reported failed, not raised
    assert seen, "the launch never reached the executor"
    assert KEY not in seen


def test_no_key_in_the_docker_env_file(tmp_path, key_everywhere, monkeypatch):
    from multiagents.executor.docker import DockerExecutor
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    executor = DockerExecutor({}, paths, {}, tmp_path / "state")
    monkeypatch.setattr(executor, "inside", lambda: False)
    monkeypatch.setattr(executor, "ensure_running", lambda: {"ok": True})
    monkeypatch.setattr(executor, "_start_wrapped", lambda *a, **k: "started")
    result = asyncio.run(executor.start(["true"], tmp_path, {
        KEY: key_everywhere, "MULTIAGENTS_AGENT_ID": "ag-env"}, run_dir=tmp_path / "run"))
    assert result == "started"
    written = executor.env_file("ag-env").read_text()
    assert "MULTIAGENTS_AGENT_ID=ag-env" in written
    assert KEY not in written


def test_the_driver_puts_the_key_only_into_its_cli(tmp_path, monkeypatch):
    """_run_supervised: the CLI's environment carries this run's key and the
    handshake directory, and nothing in the driver's own environment does."""
    seen = {}

    def attached(argv, env, stalled=None):
        seen.update(env)
        return 0

    monkeypatch.setattr(driver, "_run_attached", attached)
    monkeypatch.setattr(driver, "_start_supervisor", lambda *a, **k: None)
    from multiagents import watchdog
    monkeypatch.setattr(watchdog, "write_status", lambda *a, **k: None)
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    config = Config(project={}, providers={}, agents={}, models={}, instruction_dirs=[])
    driver._run_supervised(paths, config, "orchestrator", AgentSpec("o", "p", "m"),
                           SimpleNamespace(name="p"), object(), {}, ["cli"],
                           {KEY: "stale-from-outside"})
    assert len(bytes.fromhex(seen[KEY])) == 32
    assert seen[KEY] != "stale-from-outside"
    assert KEY not in os.environ


# ------------------------------------------------- review r7 (ag-cb7944) --

def _bytes(hs, name):
    return (safepoint.directory(hs.paths) / name).read_bytes()


def _put(hs, name, raw):
    (safepoint.directory(hs.paths) / name).write_bytes(raw)


def test_r7_1_a_superseded_cancellation_replayed_does_not_reopen(hs):
    """A acked; the driver cancels A (the server misses it) and asks B; the
    server moves to B. Replaying A's request and cancellation reopens nothing,
    so restoring B's request and ack cannot let a stop through with a launch
    admitted in between."""
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    a = safepoint.request(hs.paths, SESSION, CLI)
    a_request = _bytes(hs, barrier_name())
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed and acked == [a]
    assert safepoint.settle(hs.paths, SESSION, a, "cancelled")
    a_cancel = _bytes(hs, barrier_name())
    b = safepoint.request(hs.paths, SESSION, CLI)       # before the server looked
    b_request = _bytes(hs, barrier_name())
    server_answers(hs, gate, acked)
    assert gate.closed and acked == [b]
    b_ack = _bytes(hs, f"safepoint-server-{SERVER}.safe")
    for replay in (a_cancel, a_request, a_cancel):
        _put(hs, barrier_name(), replay)
        server_answers(hs, gate, acked)
        assert gate.closed, "a superseded request's record reopened admission"
        with pytest.raises(RuntimeError, match="stopping"):
            gate.enter("a launch slipped in")
    _put(hs, barrier_name(), b_request)
    _put(hs, f"safepoint-server-{SERVER}.safe", b_ack)
    server_answers(hs, gate, acked)
    assert gate.closed and gate.transitions == 0


def test_r7_1_only_the_current_requests_cancellation_reopens(hs):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    a = safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    safepoint.settle(hs.paths, SESSION, a, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed
    b = safepoint.request(hs.paths, SESSION, CLI)
    server_answers(hs, gate, acked)
    assert gate.closed and acked == [b]
    safepoint.settle(hs.paths, SESSION, b, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed


def test_r7_1_an_older_request_never_seen_is_superseded(hs, monkeypatch):
    hs.act_as(DRIVER)
    clock = [1000.0]
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: 10_000.0))
    safepoint.request(hs.paths, SESSION, CLI)
    older = _bytes(hs, barrier_name())                  # never shown to the server
    clock[0] += 1
    b = safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    safepoint.settle(hs.paths, SESSION, b, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed
    _put(hs, barrier_name(), older)
    server_answers(hs, gate, acked)
    assert not gate.closed, "an older request replayed closed admission"


@pytest.mark.parametrize("mac", ["é" * 64, "é", "", "G" * 64, "0" * 63, 7, None, ["x"]])
def test_r7_2_a_malformed_mac_is_invalid_never_an_exception(hs, mac):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    nonce = safepoint.request(hs.paths, SESSION, CLI)
    record = json.loads(_bytes(hs, barrier_name()))
    _put(hs, barrier_name(), json.dumps({**record, "mac": mac}).encode())
    assert safepoint._verified({**record, "mac": mac}, "request") is None
    assert safepoint.settle(hs.paths, SESSION, nonce, "commit") is False
    ack = {"type": "ack", "pid": SERVER, "start": f"start-{SERVER}", "nonce": nonce,
           "session": SESSION, "mac": mac}
    _put(hs, f"safepoint-server-{SERVER}.safe", json.dumps(ack).encode())
    assert safepoint.acknowledged(hs.paths, SERVER, nonce) is False


def test_r7_2_a_malformed_ack_cancels_rather_than_stops(hs, monkeypatch):
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    request = safepoint.request

    def request_then_garbage(*args):
        nonce = request(*args)
        _put(hs, f"safepoint-server-{SERVER}.safe",
             json.dumps({"type": "ack", "pid": SERVER, "mac": "é" * 64}).encode())
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_garbage)
    assert hs.stop._reach_safe_point("announced") is False


@pytest.mark.parametrize("field,value", [
    ("cli", ["not", "a", "dict"]), ("cli", {"pid": "4501"}), ("cli", {"pid": True}),
    ("requester", "driver"), ("requester", {"pid": 1.5}), ("nonce", 12),
    ("seq", "soon"), ("seq", 1.5), ("seq", True), ("seq", 0), ("monotonic", "soon"),
    ("monotonic", float("nan")), ("session", None)])
def test_r7_2_odd_fields_in_a_signed_record_never_raise(hs, field, value):
    """Even a correctly signed record with odd field types is handled."""
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    record = json.loads(_bytes(hs, barrier_name()))
    record = safepoint._sign({k: v for k, v in record.items() if k != "mac"} | {field: value})
    _put(hs, barrier_name(), json.dumps(record).encode())
    gate = safepoint.Gate()
    server_answers(hs, gate, [])
    assert not gate.closed


def test_r7_2_a_huge_or_undecodable_file_is_no_record(hs):
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    _put(hs, barrier_name(), b"{" + b" " * (safepoint.FILE_MAX_BYTES + 10) + b"}")
    assert safepoint._read(hs.paths, barrier_name()) is None
    _put(hs, barrier_name(), b"\xff\xfe\x00garbage")
    assert safepoint._read(hs.paths, barrier_name()) is None
    _put(hs, barrier_name(), b"[" * 100_000)
    assert safepoint._read(hs.paths, barrier_name()) is None


def test_r7_2_a_bad_key_in_the_environment_is_a_key_nobody_has(monkeypatch):
    monkeypatch.setattr(safepoint, "_secret", None)
    monkeypatch.setenv(KEY, "zz-not-hex")
    safepoint.adopt_key()
    assert KEY not in os.environ and len(safepoint._secret) == 32


def test_r7_3_no_key_from_a_providers_mcp_env(tmp_path, key_everywhere, monkeypatch):
    """The server overlay (`mcp.env`) is the last one: the key is stripped after it."""
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    git = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, env=git)
    config = Config(project={},
                    providers={"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 0"]}}},
                    agents={"worker": AgentSpec("worker", "p", "m", can_spawn=True)},
                    models={}, instruction_dirs=[])
    runner = Runner(paths, config)
    monkeypatch.setattr(runner, "_hand_server",
                        lambda *a, **k: ([], {KEY: "from-mcp-env", "MCP_OK": "1"}))
    seen = {}

    async def capture(self, argv, cwd, env, **kwargs):
        seen.update(env)
        raise RuntimeError("captured")

    monkeypatch.setattr(type(runner.executor()), "start", capture)
    asyncio.run(runner.start("worker", "go"))
    assert seen.get("MCP_OK") == "1", "the mcp overlay was not applied"
    assert KEY not in seen


def test_r7_4_no_key_in_the_running_container_probe_env_file(tmp_path, key_everywhere,
                                                           monkeypatch):
    from multiagents.executor import docker as docker_mod
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    executor = docker_mod.DockerExecutor({}, paths, {}, tmp_path / "state")
    written = {}

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="running\n", stderr="")

    class Child:
        returncode, pid = 0, 0

        def __init__(self, command, **kwargs):
            path = command[command.index("--env-file") + 1]
            written["env"] = Path(path).read_text()

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(docker_mod.subprocess, "run", run)
    monkeypatch.setattr(docker_mod.subprocess, "Popen", Child)
    executor.exec_in_running(["true"], 5, env={KEY: key_everywhere, "PROBE": "1"})
    assert "PROBE=1" in written["env"]
    assert KEY not in written["env"]


# ------------------------------------------------- review r8 (ag-6b214c) --

@pytest.fixture
def wall(monkeypatch):
    """The wall clock, under the test's control; the monotonic one is real."""
    import time as real_time
    clock = [1000.0]
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=real_time.monotonic))
    return clock


def test_r8_a_wall_clock_rollback_cannot_reopen_with_a_replayed_cancellation(hs, wall):
    """A made and cancelled at t=1000; the clock goes back; B made at t=999 and
    acknowledged by a server that never saw A. A's genuine cancellation,
    replayed, must not reopen — and so B's request and ack, restored after a
    launch would have slipped in, cannot let the stop through."""
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    a = safepoint.request(hs.paths, SESSION, CLI)
    assert safepoint.settle(hs.paths, SESSION, a, "cancelled")
    a_cancel = _bytes(hs, barrier_name())
    wall[0] = 999.0                                      # the clock moved back
    b = safepoint.request(hs.paths, SESSION, CLI)
    b_request = _bytes(hs, barrier_name())
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed and acked == [b]
    b_ack = _bytes(hs, f"safepoint-server-{SERVER}.safe")
    _put(hs, barrier_name(), a_cancel)
    server_answers(hs, gate, acked)
    assert gate.closed, "an older cancellation reopened admission after a clock rollback"
    with pytest.raises(RuntimeError, match="stopping"):
        gate.enter("a launch")
    _put(hs, barrier_name(), b_request)
    _put(hs, f"safepoint-server-{SERVER}.safe", b_ack)
    assert gate.transitions == 0


def test_r8_a_wall_clock_rollback_does_not_wedge_admission(hs, wall):
    """A acknowledged at t=1000, cancelled unseen; the clock goes back and B
    is made at t=999. The server must move to B — and B's cancellation must
    then reopen it, not leave it closed on A for the driver's lifetime."""
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    a = safepoint.request(hs.paths, SESSION, CLI)
    gate, acked = safepoint.Gate(), []
    server_answers(hs, gate, acked)
    assert gate.closed and acked == [a]
    safepoint.settle(hs.paths, SESSION, a, "cancelled")
    wall[0] = 999.0
    b = safepoint.request(hs.paths, SESSION, CLI)
    server_answers(hs, gate, acked)
    assert acked == [b], "the newer request was taken for an older one"
    safepoint.settle(hs.paths, SESSION, b, "cancelled")
    server_answers(hs, gate, acked)
    assert not gate.closed, "admission wedged closed after a clock rollback"


def test_r8_a_sequence_from_another_key_does_not_verify(hs, monkeypatch):
    hs.act_as(DRIVER)
    safepoint.request(hs.paths, SESSION, CLI)
    record = json.loads(_bytes(hs, barrier_name()))
    assert record["seq"] >= 1
    monkeypatch.setattr(safepoint, "_secret", os.urandom(32))
    assert safepoint._verified(record, "request") is None


def test_r8_start_inside_the_container_strips_a_directly_supplied_key(tmp_path, monkeypatch):
    from multiagents.executor import docker as docker_mod
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    executor = docker_mod.DockerExecutor({}, paths, {}, tmp_path / "state")
    seen = {}

    async def local_start(self, argv, cwd, env, **kwargs):
        seen.update(env)
        return "started"

    monkeypatch.setattr(docker_mod.LocalExecutor, "start", local_start)
    asyncio.run(executor._start_inside(["true"], tmp_path, {KEY: "ab" * 32, "X": "1"},
                                       run_dir=tmp_path / "run"))
    assert seen.get("X") == "1" and KEY not in seen


# ------------------------------------------------- review r9 (ag-c30bf5) --

LATE = 4504


class Server:
    """An in-process root server: its gate, its acks, and what it admitted."""

    def __init__(self, hs, pid):
        self.hs, self.pid = hs, pid
        self.gate, self.acked, self.admitted = safepoint.Gate(), [], []

    def start(self):
        """`server._serve`'s order: await a grant, register, look."""
        self.hs.act_as(self.pid)
        safepoint.await_grant(self.gate)
        safepoint.register(self.hs.paths, SESSION)
        safepoint.observe(self.hs.paths, SESSION, self.gate, self.acked, 60)
        self.hs.act_as(DRIVER)
        return self

    def poll(self):
        self.hs.act_as(self.pid)
        safepoint.observe(self.hs.paths, SESSION, self.gate, self.acked, 60)
        self.hs.act_as(DRIVER)

    def try_launch(self):
        """A launch admitted and left in flight, if the gate lets it in."""
        try:
            self.admitted.append(self.gate.enter("a launch"))
        except RuntimeError:
            pass


@pytest.fixture
def r9(hs, monkeypatch):
    hs.live.add(LATE)
    safepoint._key_bytes()              # adopted first, as `server._serve` does
    monkeypatch.setattr(safepoint, "_driver", {"pid": DRIVER, "start": f"start-{DRIVER}"})
    granter = safepoint.Granter(hs.paths, SESSION, CLI)       # ticked by hand
    hs.stop.granter = granter
    first = Server(hs, SERVER).start()
    granter.tick()
    first.poll()
    assert not first.gate.closed, "the first server was not granted"
    hs.granter, hs.first = granter, first
    return hs


def test_r9_the_exact_repro_a_server_started_mid_handshake_is_not_missed(r9, monkeypatch):
    """S acks B; after the driver's checks an agent deletes the barrier, a new
    root server T starts and tries a launch, the agent restores B, and the
    driver goes to commit."""
    hs = r9
    request = safepoint.request

    def request_and_s_acks(*args):
        nonce = request(*args)
        hs.first.poll()
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_s_acks)
    late = Server(hs, LATE)
    attacked = []

    def state_with_attack():
        if not attacked:
            attacked.append(True)
            path = safepoint.directory(hs.paths) / barrier_name()
            saved = path.read_bytes()
            path.unlink()
            late.start()
            late.try_launch()
            hs.granter.tick()                       # held: grants nobody
            path.write_bytes(saved)
            late.poll()
        return "announced"

    hs.stop._state = state_with_attack
    stopped = hs.stop._reach_safe_point("announced")
    assert attacked
    assert late.admitted == [], "a server started mid-handshake admitted a launch"
    assert not (stopped and late.gate.transitions), "committed with a launch in flight"


@pytest.mark.parametrize("step", ["before-request-granted", "before-request-ungranted",
                                  "after-request", "after-first-ack", "before-commit",
                                  "after-commit"])
def test_r9_a_server_starting_at_any_step_never_leaves_a_launch_under_a_stop(
        r9, monkeypatch, step):
    hs = r9
    late = Server(hs, LATE)

    def arrive():
        late.start()
        late.try_launch()
        late.poll()

    if step.startswith("before-request"):
        late.start()
        if step == "before-request-granted":
            hs.granter.tick()
            late.poll()
        late.try_launch()
    request, settle = safepoint.request, safepoint.settle

    def hooked_request(*args):
        nonce = request(*args)
        if step == "after-request":
            arrive()
        hs.first.poll()
        if step == "after-first-ack":
            arrive()
        return nonce

    def hooked_settle(paths, session, nonce, state):
        done = settle(paths, session, nonce, state)
        if step == "after-commit" and state == "commit":
            arrive()
        return done

    def state():
        if step == "before-commit" and not late.admitted and late.gate.driver is None:
            arrive()
        late.poll()
        return "announced"

    monkeypatch.setattr(safepoint, "request", hooked_request)
    monkeypatch.setattr(safepoint, "settle", hooked_settle)
    hs.stop._state = state
    stopped = hs.stop._reach_safe_point("announced")
    if stopped:
        assert late.gate.transitions == 0 and hs.first.gate.transitions == 0, (
            f"{step}: the stop went ahead with a launch in flight")
    if step in ("after-request", "after-first-ack", "before-commit", "after-commit"):
        assert late.admitted == [], f"{step}: an ungranted server admitted a launch"


def test_r9_a_granted_server_opens_and_an_ungranted_one_waits(r9):
    hs = r9
    late = Server(hs, LATE).start()
    late.try_launch()
    assert late.admitted == []
    hs.granter.tick()
    late.poll()
    late.try_launch()
    assert len(late.admitted) == 1


def test_r9_a_grant_for_another_server_or_a_forged_one_does_not_open(r9):
    hs = r9
    late = Server(hs, LATE).start()
    grant = safepoint._sign({"type": "grant", "pid": OTHER, "start": f"start-{OTHER}",
                             "session": SESSION})
    _put(hs, f"safepoint-server-{LATE}.grant", json.dumps(grant).encode())
    late.poll()
    assert late.gate.ungranted
    forged = {"type": "grant", "pid": LATE, "start": f"start-{LATE}", "session": SESSION,
              "mac": "0" * 64}
    _put(hs, f"safepoint-server-{LATE}.grant", json.dumps(forged).encode())
    late.poll()
    assert late.gate.ungranted


def test_r9_the_driver_dying_opens_an_ungranted_server(r9):
    hs = r9
    late = Server(hs, LATE).start()
    hs.live.discard(DRIVER)
    late.poll()
    late.try_launch()
    assert len(late.admitted) == 1


def test_r9_no_grant_is_issued_while_a_request_is_out(r9, monkeypatch):
    hs = r9
    request = safepoint.request
    seen = {}

    def request_then_late_server(*args):
        nonce = request(*args)
        Server(hs, LATE).start()
        hs.granter.tick()
        seen["granted"] = (safepoint.directory(hs.paths)
                           / f"safepoint-server-{LATE}.grant").exists()
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_late_server)
    hs.stop._reach_safe_point("announced")
    assert seen["granted"] is False


def test_r9_the_driver_identity_never_leaves_the_cli(key_everywhere, monkeypatch):
    monkeypatch.setenv(safepoint.DRIVER_ENV, "1:2")
    env = scripts.build_env("p", SimpleNamespace(bin="sh", bin_search=[], env={}), None, {})
    assert safepoint.DRIVER_ENV not in env
    assert safepoint.DRIVER_ENV not in server_env({safepoint.DRIVER_ENV: "1:2"}, "ag-x")


# ------------------------------------------------ review r10 (ag-c7285d) --

@pytest.mark.parametrize("during", ["deadline", "message", "cli-exit"])
def test_r10_what_happens_during_the_final_listing_still_cancels(hs, monkeypatch, during):
    """Everything is acknowledged; while the last listing before the commit
    runs, the bound passes, a message is sent, or the CLI ends. No commit."""
    import time as real_time
    hs.act_as(SERVER)
    safepoint.register(hs.paths, SESSION)
    hs.act_as(DRIVER)
    request = safepoint.request

    def request_and_ack(*args):
        nonce = request(*args)
        hs.act_as(SERVER)
        safepoint.acknowledge(hs.paths, nonce, SESSION)
        hs.act_as(DRIVER)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_ack)
    real = hs.stop._participants
    calls = []

    def listing(session, nonce):
        calls.append(1)
        result = real(session, nonce)
        if len(calls) == 2:                          # the one before the commit
            if during == "deadline":
                real_time.sleep(hs.stop.safe_point + 0.05)
            elif during == "message":
                hs.stop._state = lambda: "a message was sent"
            else:
                hs.live.discard(CLI)
        return result

    hs.stop._participants = listing
    assert hs.stop._reach_safe_point("announced") is False
    assert len(calls) == 2, "the final listing did not run"
    record = json.loads(_bytes(hs, barrier_name()))
    assert record["state"] != "commit", f"committed although {during} happened"


@pytest.mark.parametrize("damage", ["deleted", "corrupted", "other-server"])
def test_r10_a_grant_lost_before_it_was_read_is_issued_again(r9, damage):
    hs = r9
    late = Server(hs, LATE).start()
    hs.granter.tick()
    path = safepoint.directory(hs.paths) / f"safepoint-server-{LATE}.grant"
    assert path.exists()
    if damage == "deleted":
        path.unlink()
    elif damage == "corrupted":
        path.write_text('{"type": "grant", "mac": "é"}')
    else:
        path.write_text(json.dumps(safepoint._sign(
            {"type": "grant", "pid": OTHER, "start": f"start-{OTHER}", "session": SESSION})))
    late.poll()
    assert late.gate.ungranted
    hs.granter.tick()
    late.poll()
    assert not late.gate.ungranted, f"a {damage} grant was never issued again"
    late.try_launch()
    assert len(late.admitted) == 1


def test_r10_no_grant_is_reissued_while_a_request_is_out(r9):
    hs = r9
    late = Server(hs, LATE).start()
    hs.granter.hold()
    hs.granter.tick()
    assert not (safepoint.directory(hs.paths) / f"safepoint-server-{LATE}.grant").exists()
    hs.granter.release()
    hs.granter.tick()
    late.poll()
    assert not late.gate.ungranted
