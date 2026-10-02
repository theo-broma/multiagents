"""CW-R2b attack #3: an agent controls records, but never the signing key.

All signatures below are produced by the real driver/server API before the
attacker saves, replaces, or replays bytes. Clock changes model the host wall
clock; monotonic time remains independent. No implementation files are edited.
"""

import asyncio
import json
import random
import sys
from types import SimpleNamespace

import pytest

from multiagents import driver, safepoint
from multiagents.paths import ProjectPaths

DRIVER, CLI, SERVER = 8100, 8101, 8102
SESSION = "attack-three"


@pytest.fixture
def handshake(tmp_path, monkeypatch):
    paths = ProjectPaths(tmp_path / "project")
    paths.data.mkdir(parents=True)
    me, wall, monotonic = [DRIVER], [1000.0], [100.0]
    starts = {pid: f"start-{pid}" for pid in (DRIVER, CLI, SERVER)}
    live = set(starts)
    monkeypatch.setattr(safepoint, "_secret", bytes(range(32)))
    monkeypatch.setattr(safepoint.os, "getpid", lambda: me[0])
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        time=lambda: wall[0], monotonic=lambda: monotonic[0]))
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: starts.get(pid, ""))
    monkeypatch.setattr(safepoint.procs, "living", lambda pid, start="":
                        pid in live and (not start or start == starts.get(pid)))
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda pid, ancestor: ancestor == CLI)
    monkeypatch.setattr(safepoint.procs, "descendants", lambda pid:
                        [SERVER] if pid == CLI and SERVER in live else [])
    monkeypatch.setattr(safepoint, "_is_root_server", lambda pid, session:
                        pid == SERVER and session == SESSION)
    h = SimpleNamespace(paths=paths, me=me, wall=wall, monotonic=monotonic,
                        live=live, starts=starts, gate=safepoint.Gate(), acked=[])
    me[0] = SERVER
    safepoint.register(paths, SESSION)
    me[0] = DRIVER
    return h


def barrier(h, session=SESSION):
    return safepoint.directory(h.paths) / f"safepoint-barrier-{safepoint._key(session)}.json"


def ack(h):
    return safepoint.directory(h.paths) / f"safepoint-server-{SERVER}.safe"


def observe(h):
    h.me[0] = SERVER
    try:
        safepoint.observe(h.paths, SESSION, h.gate, h.acked, 60)
    finally:
        h.me[0] = DRIVER


def compaction(h):
    stop = driver._AttachedCompaction.__new__(driver._AttachedCompaction)
    stop.paths, stop.cli, stop.safe_point = h.paths, CLI, 0.01
    stop.context = {"MULTIAGENTS_SESSION_ID": SESSION}
    stop._state = lambda: "announced"
    stop._cancel = lambda *a: None
    stop._blocked = lambda *a: None
    return stop


@pytest.mark.parametrize("second_at", [1000.0, 999.0], ids=["equal-timestamps", "clock-rollback"])
def test_old_signed_cancel_cannot_authorize_stop_during_transition(handshake, monkeypatch, second_at):
    """A's unseen cancellation must not reopen a server that acknowledged B.

    A clock rollback or two requests in the same wall-clock tick is enough;
    the agent only saves and restores genuine bytes, without signing anything.
    """
    h = handshake
    old_nonce = safepoint.request(h.paths, SESSION, CLI)
    assert safepoint.settle(h.paths, SESSION, old_nonce, "cancelled")
    old_cancel = barrier(h).read_bytes()
    h.wall[0] = second_at
    real_request = safepoint.request
    admitted = []

    def request_then_replay(*args):
        nonce = real_request(*args)
        current_request = barrier(h).read_bytes()
        observe(h)
        assert h.gate.closed and safepoint.acknowledged(h.paths, SERVER, nonce)
        current_ack = ack(h).read_bytes()
        barrier(h).write_bytes(old_cancel)
        observe(h)
        if not h.gate.closed:
            admitted.append(h.gate.enter("launch"))
        # Agent restores the current signed request and previously valid ack.
        barrier(h).write_bytes(current_request)
        ack(h).write_bytes(current_ack)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_then_replay)
    try:
        may_stop = compaction(h)._reach_safe_point("announced")
        assert not (may_stop and h.gate.transitions), (
            "driver authorized stopping the CLI while an admitted launch was mid-transition")
        assert h.gate.closed, "replayed cancellation of another request reopened admission"
    finally:
        for ticket in admitted:
            ticket.end()


@pytest.mark.parametrize("first_at", [1000.0, 1e12], ids=["one-second-rollback", "future-clock"])
def test_cancel_after_clock_rollback_does_not_wedge_admission(handshake, first_at):
    """The driver cancels A, retries at an earlier time, then cancels B.

    The server misses A's cancellation between polls. It must eventually
    reopen after the driver's cancellation, even if wall time stays behind.
    """
    h = handshake
    h.wall[0] = first_at
    a = safepoint.request(h.paths, SESSION, CLI)
    observe(h)
    assert h.gate.closed
    assert safepoint.settle(h.paths, SESSION, a, "cancelled")
    h.wall[0] = 999.0
    b = safepoint.request(h.paths, SESSION, CLI)
    observe(h)
    assert safepoint.settle(h.paths, SESSION, b, "cancelled")
    # Repeated polls long after the bound cannot wait for wall time to catch up.
    for elapsed in (1, 61, 100_000):
        h.monotonic[0] += elapsed
        observe(h)
    # Correcting the clock again does not excuse an indefinitely stuck gate.
    h.wall[0] = first_at + 1
    observe(h)
    assert not h.gate.closed, "cancelled compaction left admission closed with a live driver"


def test_replayed_records_from_previous_run_and_session_are_invalid(handshake, monkeypatch):
    h = handshake
    nonce = safepoint.request(h.paths, SESSION, CLI)
    request = barrier(h).read_bytes()
    observe(h)
    registration = (safepoint.directory(h.paths) / f"safepoint-server-{SERVER}.json").read_bytes()
    old_ack = ack(h).read_bytes()
    barrier(h, "other-session").write_bytes(request)
    assert safepoint.request_for(h.paths, "other-session") is None
    # Fresh per-run key rejects every old record, irrespective of replay order.
    monkeypatch.setattr(safepoint, "_secret", bytes(reversed(range(32))))
    for raw, kind in ((old_ack, "ack"), (request, "request"), (registration, "registration")):
        assert safepoint._verified(json.loads(raw), kind) is None
    assert not safepoint.acknowledged(h.paths, SERVER, nonce)


def test_old_ack_and_registration_do_not_survive_server_pid_reuse(handshake):
    h = handshake
    nonce = safepoint.request(h.paths, SESSION, CLI)
    observe(h)
    assert safepoint.acknowledged(h.paths, SERVER, nonce)
    h.starts[SERVER] = "reused-server-pid"
    assert not safepoint.acknowledged(h.paths, SERVER, nonce)
    assert safepoint.servers(h.paths, SESSION, CLI) == []
    assert safepoint.unregistered(SESSION, CLI, []) == 1


def test_generated_field_substitutions_never_authenticate(handshake):
    """Seed 0xc032b: mutate every field of each genuine record type."""
    h = handshake
    nonce = safepoint.request(h.paths, SESSION, CLI)
    request = json.loads(barrier(h).read_bytes())
    observe(h)
    records = [request, json.loads(ack(h).read_bytes()), json.loads(
        (safepoint.directory(h.paths) / f"safepoint-server-{SERVER}.json").read_bytes())]
    assert safepoint.settle(h.paths, SESSION, nonce, "commit")
    records.append(json.loads(barrier(h).read_bytes()))
    assert safepoint.settle(h.paths, SESSION, nonce, "cancelled")
    records.append(json.loads(barrier(h).read_bytes()))
    values = [None, True, False, 0, -1, 1.5, [], {}, "", "\ud800", "e\u0301", "é"]
    rng = random.Random(0xc032b)
    values += [rng.getrandbits(64) for _ in range(30)]
    for original in records:
        assert safepoint._verified(original, original["type"]) is not None
        for field in original:
            for value in values:
                if value == original[field]:
                    continue
                forged = {**original, field: value}
                assert safepoint._verified(forged, *[r["type"] for r in records]) is None, (field, value)
        for other in records:
            if original["type"] != other["type"]:
                assert safepoint._verified({**original, "type": other["type"]}, other["type"]) is None


@pytest.mark.parametrize("damage", ["delete", "truncated", "oversized", "invalid-utf8", "deep-json", "duplicate-field"])
@pytest.mark.parametrize("target", ["registration", "ack", "barrier"])
def test_damaged_records_never_authorize_driver_stop(handshake, monkeypatch, damage, target):
    h = handshake
    real_request = safepoint.request

    def request_and_damage(*args):
        nonce = real_request(*args)
        observe(h)
        path = {"registration": safepoint.directory(h.paths) / f"safepoint-server-{SERVER}.json",
                "ack": ack(h), "barrier": barrier(h)}[target]
        raw = path.read_bytes()
        if damage == "delete":
            path.unlink()
        else:
            data = {"truncated": raw[:len(raw) // 2],
                    "oversized": b" " * (safepoint.FILE_MAX_BYTES + 1),
                    "invalid-utf8": b"\xff\xfe{",
                    "deep-json": b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
                    "duplicate-field": raw[:-1] + b',"type":"cancel"}'}[damage]
            path.write_bytes(data)
        return nonce

    monkeypatch.setattr(safepoint, "request", request_and_damage)
    assert compaction(h)._reach_safe_point("announced") is False
    assert h.gate.closed, "untrusted damage reopened server admission"


@pytest.mark.parametrize("driver_reused", [False, True], ids=["driver-dies", "driver-pid-reused"])
def test_driver_death_reopens_despite_deleted_barrier(handshake, driver_reused):
    h = handshake
    safepoint.request(h.paths, SESSION, CLI)
    observe(h)
    barrier(h).unlink()
    if driver_reused:
        h.starts[DRIVER] = "different-driver"
    else:
        h.live.remove(DRIVER)
    observe(h)
    assert not h.gate.closed
    assert not ack(h).exists()


@pytest.mark.parametrize("nested", [False, True], ids=["agent", "nested-server"])
def test_key_absent_from_child_environment_cmdline_and_transcript(tmp_path, monkeypatch, nested):
    from multiagents.executor.base import build_env
    from multiagents.executor.local import LocalExecutor
    from multiagents.runner import server_env

    # Synthetic test key; no credentials or host /proc environ are read.
    key = "b7" * 32
    monkeypatch.setenv(safepoint.KEY_ENV, key)
    env = build_env(passthrough=[safepoint.KEY_ENV, f"{safepoint.KEY_ENV}={key}"],
                    blocked=[], home=None,
                    identity={safepoint.KEY_ENV: key, "MULTIAGENTS_AGENT_ID": "ag-attack3"})
    if nested:
        env = server_env({**env, safepoint.KEY_ENV: key}, "ag-attack3")
    probe = tmp_path / "probe.py"
    probe.write_text("import json, os\nfrom pathlib import Path\n"
                     "print(json.dumps(dict(os.environ)))\n"
                     "print(repr(Path('/proc/self/cmdline').read_bytes()))\n")
    run_dir = tmp_path / "run"

    async def run():
        handle = await LocalExecutor().start([sys.executable, str(probe)], tmp_path,
                                             env, run_dir=run_dir)
        try:
            return handle._proc.wait(timeout=10)
        finally:
            if handle._proc.poll() is None:
                handle._proc.kill()
                handle._proc.wait(timeout=10)

    assert asyncio.run(run()) == 0
    assert (run_dir / "exit_status").read_text().strip() == "0"
    output = (run_dir / "output.ndjson").read_text()
    assert "MULTIAGENTS_AGENT_ID" in output
    for path in run_dir.iterdir():
        if path.is_file():
            assert key.encode() not in path.read_bytes(), path.name
    assert safepoint.KEY_ENV not in output


def test_key_absent_from_real_provider_action_environment_and_argv(tmp_path, monkeypatch):
    from multiagents import scripts

    key = "d3" * 32
    # The probe dumps its own environment; keep real ambient secrets out.
    for name in list(scripts.os.environ):
        if name != "PATH":
            monkeypatch.delenv(name)
    monkeypatch.setenv(safepoint.KEY_ENV, key)
    probe = tmp_path / "action.py"
    probe.write_text(f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
                     "print(json.dumps(dict(os.environ)))\n"
                     "print(repr(Path('/proc/self/cmdline').read_bytes()))\n")
    probe.chmod(0o700)
    monkeypatch.setattr(scripts, "resolve", lambda *a, **k: probe)
    provider = SimpleNamespace(bin="sh", bin_search=[], env={safepoint.KEY_ENV: key})
    code, out, err = scripts.run_action("probe", provider, None, "check", tmp_path,
                                       extra_env={safepoint.KEY_ENV: key, "ACTION_OK": "1"})
    assert code == 0, err
    assert "ACTION_OK" in out
    assert key not in out + err
    assert safepoint.KEY_ENV not in out + err
