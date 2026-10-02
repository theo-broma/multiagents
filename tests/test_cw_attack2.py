"""Second CW attack: bind-mount aliases must not expose the host handshake.

These use the actual DockerExecutor mount inventory and actual handshake I/O;
no Docker daemon, live provider, or signal is involved.
"""

import json
import os
from types import SimpleNamespace

import pytest

from multiagents import safepoint
from multiagents.config import AgentSpec, Config
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths


def config(extra_mounts=()):
    return Config(
        project={"executor": {"kind": "docker", "docker": {
            "mount_cli_from_host": False, "extra_mounts": list(extra_mounts)}}},
        providers={}, models={}, instruction_dirs=[],
        agents={"worker": AgentSpec("worker", "p", "m")},
    )


def forge_ack_from_visible_files(paths, nonce, pid):
    """What a writer who can see the handshake directory but lacks the key
    can do: copy every readable field (the registration's MAC included) into
    an acknowledgement, with and without a record type."""
    record = safepoint._read(paths, f"safepoint-server-{pid}.json")
    for forged in ({**record, "nonce": nonce},
                   {**record, "type": "ack", "nonce": nonce},
                   {k: v for k, v in record.items() if k != "mac"} | {"nonce": nonce}):
        safepoint._write(paths, f"safepoint-server-{pid}.safe", forged)
        assert not safepoint.acknowledged(paths, pid, nonce)


@pytest.mark.parametrize("ancestor", [False, True], ids=["handshake", "safepoints-parent"])
def test_r2b_forged_ack_through_a_bind_mount_alias_is_rejected(tmp_path, monkeypatch, ancestor):
    """CW-R2b: exposure is no longer detected; a writer reaching the handshake
    through a bind-mount alias is harmless because it cannot sign."""
    paths = ProjectPaths(tmp_path / "project")
    paths.root.mkdir()
    state = tmp_path / "host-state"
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    where = safepoint.directory(paths)
    where.mkdir(parents=True)
    alias = paths.root / "mounted-alias"
    alias.symlink_to(where.parent if ancestor else where, target_is_directory=True)
    cfg = config([str(alias)])
    mounts = DockerExecutor(cfg.project["executor"]["docker"], paths, {}, state).mounts()
    assert any(source == alias for source, _ in mounts)
    session = "cw-attack2-session"
    safepoint.register(paths, session)
    nonce = safepoint.request(paths, session, os.getpid())
    visible = alias / paths.slug if ancestor else alias
    record = json.loads((visible / f"safepoint-server-{os.getpid()}.json").read_text())
    gate = safepoint.Gate()
    with gate.enter("launch before pid is recorded"):
        # Written through the alias, exactly as the mount would allow.
        (visible / f"safepoint-server-{os.getpid()}.safe").write_text(
            json.dumps({**record, "nonce": nonce}))
        assert gate.transitions == 1
        assert not safepoint.acknowledged(paths, os.getpid(), nonce)
    forge_ack_from_visible_files(paths, nonce, os.getpid())


@pytest.mark.parametrize("component", ["state-root", "safepoints", "project-key"])
def test_symlink_in_handshake_path_into_project_is_refused(tmp_path, monkeypatch, component):
    paths = ProjectPaths(tmp_path / "project")
    paths.root.mkdir()
    state = tmp_path / "host-state"
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    writable = paths.root / "agent-writable"
    writable.mkdir()
    where = safepoint.directory(paths)
    if component == "state-root":
        state.symlink_to(writable, target_is_directory=True)
    elif component == "safepoints":
        state.mkdir()
        (state / "safepoints").symlink_to(writable, target_is_directory=True)
    else:
        where.parent.mkdir(parents=True)
        where.symlink_to(writable, target_is_directory=True)
    session = "cw-attack2-session"
    if component == "state-root":
        # The state root itself is not exposure-checked any more (CW-R2b):
        # whatever lands in the writable project is unforgeable and any
        # record an agent plants there is rejected.
        safepoint.register(paths, session)
        nonce = safepoint.request(paths, session, os.getpid())
        forge_ack_from_visible_files(paths, nonce, os.getpid())
        return
    try:
        safepoint.request(paths, session, 4200)
    except OSError:
        return  # Refusing to publish through a symlink satisfies the boundary.
    pytest.fail("the handshake request was written through a symlinked path component")


@pytest.mark.parametrize("state", ["drain", "commit"])
@pytest.mark.parametrize("reused", [False, True])
def test_dead_or_reused_driver_does_not_leave_admission_closed(tmp_path, monkeypatch, state, reused):
    """Crash recovery, including a pid reused by a different process."""
    paths = ProjectPaths(tmp_path / "project")
    clock = [100.0]
    driver, cli, server = 5400, 5401, 5402
    starts = {driver: "driver-start", cli: "cli-start", server: "server-start"}
    live = set(starts)
    monkeypatch.setattr(safepoint, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0]))
    monkeypatch.setattr(safepoint.procs, "start_time", lambda pid: starts.get(pid, ""))
    monkeypatch.setattr(safepoint.procs, "living", lambda pid, start="":
                        pid in live and (not start or start == starts[pid]))
    monkeypatch.setattr(safepoint.procs, "descends_from", lambda pid, ancestor: ancestor == cli)
    monkeypatch.setattr(safepoint.os, "getpid", lambda: driver)
    nonce = safepoint.request(paths, "crash-session", cli)
    monkeypatch.setattr(safepoint.os, "getpid", lambda: server)
    safepoint.register(paths, "crash-session")
    gate, acked = safepoint.Gate(), []
    safepoint.observe(paths, "crash-session", gate, acked, 10)
    assert gate.closed
    if state == "commit":
        assert safepoint.settle(paths, "crash-session", nonce, state)
        safepoint.observe(paths, "crash-session", gate, acked, 10)
        assert gate.committed
    if reused:
        starts[driver] = "unrelated-process-start"
    else:
        live.remove(driver)
    clock[0] += 10 + safepoint.COMMITTED_GRACE_SECONDS + 1
    safepoint.observe(paths, "crash-session", gate, acked, 10)
    assert not gate.closed
    assert not safepoint.acknowledged(paths, server, nonce)


@pytest.mark.parametrize("field,bad", [
    ("nonce", "wrong"), ("session", "other-session"),
    ("pid", 999999), ("start", "reused-pid-start"),
])
def test_ack_binding_rejects_each_independently_wrong_field(tmp_path, field, bad):
    paths = ProjectPaths(tmp_path / "project")
    session = "binding-session"
    safepoint.register(paths, session)
    nonce = safepoint.request(paths, session, os.getpid())
    record = safepoint._read(paths, f"safepoint-server-{os.getpid()}.json")
    safepoint._write(paths, f"safepoint-server-{os.getpid()}.safe",
                     {**record, "nonce": nonce, field: bad})
    assert not safepoint.acknowledged(paths, os.getpid(), nonce)


def test_unreadable_unrelated_proc_entry_blocks_even_registered_servers(monkeypatch):
    """Characterize hidepid=1: global uncertainty suppresses CW forever.

    This intentionally records the current fail-closed contract, rather than
    asserting that an unknown process should be ignored.
    """
    from multiagents import procs
    monkeypatch.setattr(procs.os, "listdir", lambda path: ["6100", "6101", "9900"])

    def stat(pid):
        if pid == 9900:
            raise PermissionError("hidepid: other user's process")
        return ["S", "1" if pid == 6100 else "6100"]

    monkeypatch.setattr(procs, "_stat", stat)
    assert procs.descendants(6100) is None
    assert safepoint.unregistered("session", 6100, [{"pid": 6101}]) is None


def test_agent_environment_does_not_inherit_host_safepoint_dir(tmp_path, monkeypatch):
    from multiagents.executor.base import build_env
    monkeypatch.setenv(safepoint.ENV, str(tmp_path / "host-only-handshake"))
    env = build_env(passthrough=[], blocked=[], home=tmp_path / "agent-home",
                    identity={"MULTIAGENTS_AGENT_ID": "ag-worker"})
    assert safepoint.ENV not in env


@pytest.mark.parametrize("mount", ["safepoints", "state-root", "home"])
def test_r2b_forged_records_under_a_direct_ancestor_mount_are_rejected(tmp_path, monkeypatch, mount):
    """A container mounting an ancestor of the handshake sees and can write
    it; with no key it can neither acknowledge nor open admission."""
    paths = ProjectPaths(tmp_path / "project")
    paths.root.mkdir()
    home = tmp_path / "host-home"
    state = home / ".multiagents"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    source = {"safepoints": state / "safepoints", "state-root": state, "home": home}[mount]
    session = "ancestor-session"
    safepoint.register(paths, session)
    assert source in safepoint.directory(paths).parents
    nonce = safepoint.request(paths, session, os.getpid())
    forge_ack_from_visible_files(paths, nonce, os.getpid())
    # An unsigned "cancel" or fresh "request" planted in the barrier is not
    # an authenticated request for this process.
    name = f"safepoint-barrier-{safepoint._key(session)}.json"
    barrier = safepoint._read(paths, name)
    safepoint._write(paths, name, {k: v for k, v in barrier.items() if k != "mac"}
                     | {"type": "cancel", "state": "cancelled"})
    assert safepoint.request_for(paths, session) is None


@pytest.mark.parametrize("reused", [False, True])
def test_server_killed_after_ack_does_not_leave_a_valid_ack(tmp_path, monkeypatch, reused):
    paths = ProjectPaths(tmp_path / "project")
    pid = 7201
    current = ["original-start"]
    live = [True]
    monkeypatch.setattr(safepoint.os, "getpid", lambda: pid)
    monkeypatch.setattr(safepoint.procs, "start_time", lambda value: current[0])
    monkeypatch.setattr(safepoint.procs, "living", lambda value, start="":
                        live[0] and (not start or start == current[0]))
    safepoint.register(paths, "server-crash")
    nonce = safepoint.request(paths, "server-crash", 7200)
    assert safepoint.acknowledge(paths, nonce, "server-crash")
    assert safepoint.acknowledged(paths, pid, nonce)
    if reused:
        current[0] = "replacement-start"
        safepoint.register(paths, "server-crash")
    else:
        live[0] = False
    assert not safepoint.acknowledged(paths, pid, nonce)
