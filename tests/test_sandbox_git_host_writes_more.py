"""SG-R7 (the rest) — the last host writes under `.multiagents` that still
used plain paths: no link followed, no FIFO blocked on.

Contract: context/specs/sandbox-git.md.

- SG-R7 (Decisions "after implementer ag-ce8b54 on SG-R3"): files the host
  writes or reads for an agent never sit where the container can write, or
  are created with no-follow semantics, never through a pre-existing
  symlink, and read bounded and non-blocking.
- Decisions "after implementers ag-3c75c1 and ag-b65538": every file or
  directory the HOST creates or writes under `.multiagents` is written with
  the same no-follow primitives.
- "Open, after ag-5fb684": still plain paths, and in scope —
  - the driver's `launch/` files;
  - the watchdog's `{role}-status.json`;
  - `monitor-launch.log`;
  - the runner's `consult-*.lock`.

Same method as `test_sandbox_git_host_writes.py` (its helpers are reused):
plant a symlink to `tmp_path/outside`, or a FIFO, where the host is about to
write, drive the smallest public entry point that writes there, and assert
that nothing outside was created or changed and that nothing hung. Refusing
(raising, or returning an error) is an allowed answer to every attack. Each
group has a clean control showing the redirected write really happens.

Entry points:

- the driver: `driver._launch_agent(..., unattended=1)`, the body of
  `multiagents run --unattended`, with a fake provider — as
  `test_phase0_provider_compact.py` drives it. Writes `launch/` (the dir),
  `orchestrator-prompt.md`, `{role}.launched`, `{role}.session`, `{role}.pid`,
  and reads `{role}.session` (on resume) and the OTHER driver's pid file.
- the watchdog: `watchdog.write_status` / `read_status` — `{role}-status.json`
  and the `.tmp` it is renamed from.
- the monitor: `actions.launch_orchestrator` — `monitor-launch.log`. The
  interpreter it runs the CLI with is replaced by a script that prints one
  line and exits, so no orchestrator starts.
- the runner: `Runner.consult` — `consult-<agent>.lock`.

Depth: `launch/` is the only directory between `.multiagents` and these files,
and it is covered. `.multiagents` itself is a bind-mount point in the
container, which cannot be replaced from inside, so it is not attacked.

FIFO cases that pass today are marked as guards: the obvious no-follow
rewrite (`O_NOFOLLOW` without `O_NONBLOCK`) would make them hang.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402
import p0_context_harness as ch  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents import driver, watchdog  # noqa: E402
from multiagents.monitor import actions  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from test_sandbox_git_host_writes import (  # noqa: E402
    HOST_BYTES, attempt, in_thread, listing, outside, replace_with_fifo,
    replace_with_link)


# --- the driver's launch/ files ----------------------------------------------------

ROLE = "orchestrator"
LAUNCH_FILES = [f"{ROLE}-prompt.md", f"{ROLE}.launched", f"{ROLE}.session",
                f"{ROLE}.pid"]


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """A project whose orchestrator is a fake provider taking one turn;
    `launched.paths`, and `launched.run(resume=...)` to launch it."""
    root = h.make_git_repo((tmp_path / "proj").resolve())
    paths = ProjectPaths(root)
    paths.ensure()
    fake = ch.FakeProvider(paths.config, tmp_path)
    fake.control(turns=[{"exit": 0}])
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "fakeprov": {"bin": "true", "script": fake.name, "spawn": {"args": ["x"]},
                     "env": fake.env}}}))
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({"agents": {
        ROLE: {"provider": "fakeprov", "model": "m", "launch": True, "role": ROLE}}}))
    monkeypatch.setenv("FAKE_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_CTL", str(fake.ctl))
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.chdir(root)

    class Launched:
        pass

    got = Launched()
    got.paths = paths
    got.launch_dir = paths.data / "launch"
    got.fake = fake
    got.run = lambda resume=False: driver._launch_agent(
        paths, config_mod.load(paths), ROLE, resume=resume, unattended=1)
    return got


def test_control_sg_r7_the_driver_writes_its_launch_files(launched):
    assert launched.run() == 0
    assert launched.fake.calls("launch"), "the launch action never ran"
    launch = launched.launch_dir
    assert launch.is_dir() and not launch.is_symlink()
    for name in (f"{ROLE}-prompt.md", f"{ROLE}.launched", f"{ROLE}.session"):
        assert (launch / name).is_file(), sorted(os.listdir(launch))


def test_sg_r7_the_driver_creates_nothing_behind_a_symlinked_launch_dir(
        launched, tmp_path):
    target = outside(tmp_path)
    replace_with_link(launched.launch_dir, target)
    attempt(launched.run)
    assert listing(target) == [], \
        f"the driver wrote {listing(target)} in a host dir behind .multiagents/launch"


@pytest.mark.parametrize("name", LAUNCH_FILES)
def test_sg_r7_the_driver_does_not_write_through_a_symlinked_launch_file(
        launched, tmp_path, name):
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(launched.launch_dir / name, host_file)
    attempt(launched.run)
    assert host_file.exists(), f"the driver removed a host file through launch/{name}"
    assert host_file.read_bytes() == HOST_BYTES, \
        f"the driver wrote a host file through a symlinked launch/{name}"


@pytest.mark.parametrize("name", LAUNCH_FILES)
def test_sg_r7_the_driver_creates_no_host_file_through_a_dangling_launch_link(
        launched, tmp_path, name):
    target = outside(tmp_path)
    replace_with_link(launched.launch_dir / name, target / "planted")
    attempt(launched.run)
    assert listing(target) == [], \
        f"the driver created {listing(target)} through a dangling launch/{name}"


@pytest.mark.parametrize("name", LAUNCH_FILES)
def test_sg_r7_the_driver_does_not_hang_on_a_fifo_launch_file(launched, name):
    fifo = launched.launch_dir / name
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(launched.run), fifo)
    assert finished, f"the driver hung on a FIFO planted as launch/{name}"


def test_sg_r7_a_resumed_launch_does_not_hang_on_a_fifo_session_file(launched):
    """On resume the session id is READ from launch/, not written."""
    assert launched.run() == 0                  # launched once: a resume is possible
    fifo = launched.launch_dir / f"{ROLE}.session"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(lambda: launched.run(resume=True)), fifo)
    assert finished, "a resumed launch hung reading a FIFO planted as the session file"


def test_sg_r7_the_driver_does_not_hang_on_a_fifo_as_the_other_drivers_pid_file(
        launched):
    """Before launching, the driver reads the OTHER role's pid file to refuse
    running both at once. A FIFO there must not hold the launch."""
    fifo = launched.launch_dir / "initializer.pid"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(launched.run), fifo)
    assert finished, "the driver hung reading a FIFO planted as initializer.pid"


def test_sg_r7_the_driver_does_not_hang_on_a_fifo_launch_dir(launched):
    """Guard: passes today (`mkdir` does not open what is there)."""
    fifo = launched.launch_dir
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(launched.run), fifo)
    assert finished, "the driver hung on a FIFO planted as .multiagents/launch"


# --- the watchdog's {role}-status.json ----------------------------------------------

def status_paths(tmp_path) -> ProjectPaths:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    return paths


def record(role: str = ROLE) -> dict:
    return {"at": time.time(), "role": role, "pid": None, "running": False,
            "verdict": "stopped", "detail": "probe"}


def test_control_sg_r7_the_watchdog_writes_its_status_under_multiagents(tmp_path):
    paths = status_paths(tmp_path)
    watchdog.write_status(paths, record(), ROLE)
    status = paths.data / f"{ROLE}-status.json"
    assert status.is_file() and not status.is_symlink()
    assert (watchdog.read_status(paths, ROLE) or {}).get("detail") == "probe"


@pytest.mark.parametrize("role", watchdog.DRIVERS)
def test_sg_r7_the_watchdog_does_not_write_through_a_symlinked_status_temp(
        tmp_path, role):
    """`{role}-status.json` is written whole and renamed into place; what it
    is written to first sits beside it, in `.multiagents`, where an agent can
    plant a link. Whatever the temporary's name, planted here: `.tmp`."""
    paths = status_paths(tmp_path)
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(paths.data / f"{role}-status.tmp", host_file)
    attempt(lambda: watchdog.write_status(paths, record(role), role))
    assert host_file.read_bytes() == HOST_BYTES, \
        f"the watchdog wrote a host file through a symlinked {role}-status.tmp"


def test_sg_r7_the_watchdog_creates_no_host_file_through_a_dangling_status_temp(
        tmp_path):
    paths = status_paths(tmp_path)
    target = outside(tmp_path)
    replace_with_link(paths.data / f"{ROLE}-status.tmp", target / "planted")
    attempt(lambda: watchdog.write_status(paths, record(), ROLE))
    assert listing(target) == [], \
        f"the watchdog created {listing(target)} through a dangling status temp"


def test_sg_r7_the_watchdog_does_not_write_through_a_symlinked_status_file(tmp_path):
    """Guard: passes today (the temporary is renamed over the link)."""
    paths = status_paths(tmp_path)
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(paths.data / f"{ROLE}-status.json", host_file)
    attempt(lambda: watchdog.write_status(paths, record(), ROLE))
    assert host_file.read_bytes() == HOST_BYTES, \
        "the watchdog wrote a host file through a symlinked status file"


def test_sg_r7_the_watchdog_does_not_hang_on_a_fifo_status_temp(tmp_path):
    paths = status_paths(tmp_path)
    fifo = paths.data / f"{ROLE}-status.tmp"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(
        lambda: attempt(lambda: watchdog.write_status(paths, record(), ROLE)), fifo)
    assert finished, "writing the status hung on a FIFO planted as its temporary"


def test_sg_r7_the_watchdog_does_not_hang_on_a_fifo_status_file_when_writing(tmp_path):
    """Guard: passes today (the rename replaces the FIFO without opening it)."""
    paths = status_paths(tmp_path)
    fifo = paths.data / f"{ROLE}-status.json"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(
        lambda: attempt(lambda: watchdog.write_status(paths, record(), ROLE)), fifo)
    assert finished, "writing the status hung on a FIFO planted as the status file"


def test_sg_r7_reading_the_status_does_not_hang_on_a_fifo_status_file(tmp_path):
    """SG-R7: a file the host reads from where the container can write is
    read non-blocking. `multiagents status` and the monitor read this."""
    paths = status_paths(tmp_path)
    fifo = paths.data / f"{ROLE}-status.json"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(lambda: watchdog.read_status(paths, ROLE)),
                               fifo)
    assert finished, "reading the status hung on a FIFO planted as the status file"


# --- the monitor's monitor-launch.log -----------------------------------------------

LAUNCH_OUTPUT = b"launched-by-monitor\n"


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    """A project, and a Python that prints one line and exits: the monitor's
    launch runs our own CLI with it, so no orchestrator actually starts."""
    paths = status_paths(tmp_path)
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\necho launched-by-monitor\n")
    fake_python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(fake_python))
    return paths


def launch_from_monitor(paths) -> dict:
    return actions.launch_orchestrator(paths, unattended=1)


def settle(check, seconds: float = 5.0) -> bool:
    """The launched process is detached: wait for what it writes."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_control_sg_r7_the_monitor_logs_its_launch_under_multiagents(monitor):
    result = launch_from_monitor(monitor)
    assert result.get("ok"), result
    log = monitor.data / "monitor-launch.log"
    assert settle(lambda: log.is_file() and LAUNCH_OUTPUT in log.read_bytes()), \
        "the launched process's output never reached monitor-launch.log"
    assert not log.is_symlink()


def test_sg_r7_the_monitor_does_not_write_through_a_symlinked_launch_log(
        monitor, tmp_path):
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(monitor.data / "monitor-launch.log", host_file)
    attempt(lambda: launch_from_monitor(monitor))
    settle(lambda: host_file.read_bytes() != HOST_BYTES, 2.0)
    assert host_file.read_bytes() == HOST_BYTES, \
        "the launch's output was appended to a host file through monitor-launch.log"


def test_sg_r7_the_monitor_creates_no_host_file_through_a_dangling_launch_log(
        monitor, tmp_path):
    target = outside(tmp_path)
    replace_with_link(monitor.data / "monitor-launch.log", target / "planted")
    attempt(lambda: launch_from_monitor(monitor))
    settle(lambda: listing(target) != [], 2.0)
    assert listing(target) == [], \
        f"the monitor created {listing(target)} through a dangling monitor-launch.log"


def test_sg_r7_the_monitor_does_not_hang_on_a_fifo_launch_log(monitor):
    fifo = monitor.data / "monitor-launch.log"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(lambda: launch_from_monitor(monitor)),
                               fifo)
    assert finished, "the monitor's launch hung on a FIFO planted as monitor-launch.log"


# --- the runner's consult-<agent>.lock ----------------------------------------------

@pytest.fixture
def consulted(tmp_path, monkeypatch):
    from test_consult_fresh_worktree_adversary import Project
    return Project(tmp_path, monkeypatch, timeout=20)


def consult_lock(project) -> Path:
    return project.runner.paths.data / "consult-advisor.lock"


def test_control_sg_r7_a_consult_takes_its_lock_under_multiagents(consulted):
    result = consulted.consult("turn one", timeout=20)
    assert not result.get("error"), result
    lock = consult_lock(consulted)
    assert lock.exists() and not lock.is_symlink()


def test_sg_r7_a_consult_creates_no_host_file_through_a_dangling_lock_link(
        consulted, tmp_path):
    target = outside(tmp_path)
    replace_with_link(consult_lock(consulted), target / "planted")
    attempt(lambda: consulted.consult("turn one", timeout=20))
    assert listing(target) == [], \
        f"a consult created {listing(target)} through a symlinked consult lock"


def test_sg_r7_a_consult_does_not_write_through_a_symlinked_lock(consulted, tmp_path):
    """Guard: passes today (the lock is opened for append and nothing is
    written); a rewrite that truncates or writes a holder into it would not."""
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(consult_lock(consulted), host_file)
    attempt(lambda: consulted.consult("turn one", timeout=20))
    assert host_file.read_bytes() == HOST_BYTES, \
        "a consult wrote a host file through a symlinked consult lock"


def test_sg_r7_a_consult_does_not_hang_on_a_fifo_lock(consulted):
    """Guard: passes today (the lock is opened read-write, which a FIFO does
    not block); a write-only no-follow open would hang here."""
    fifo = consult_lock(consulted)
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(
        lambda: attempt(lambda: consulted.consult("turn one", timeout=20)), fifo)
    assert finished, "a consult hung on a FIFO planted as its lock"
