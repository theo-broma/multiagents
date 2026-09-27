"""SG-R7 — files the host writes or reads for an agent never sit where the
container can write; and a `docker exec` timeout leaves nothing running.

Contract: context/specs/sandbox-git.md, Decisions "after implementer ag-ce8b54
on SG-R3":

- SG-R7: `DockerExecutor.start()` writes the agent's env file, and
  `ContainerGit` reads it back. Such a file is either created with no-follow
  semantics and read bounded and non-blocking, or moved to a host-only
  location that is not mounted writable. Verified by: an agent that replaces
  its env file path, or the directory holding it, with a symlink to a host
  file cannot make the host write that file, and a FIFO in its place does
  not hang `start()`.
- A `docker exec` timeout kills the process inside the container too; it
  must not leave git running.

Where the agent attacks: ``<project>/.multiagents/env/<agent id>.env`` and
``<project>/.multiagents/env`` — the container-writable location the spec
names as today's. An implementation that moves the file to a host-only place
makes these attacks inert, which is one of the two ways the contract allows,
so every test here passes for it too. Nothing asserts where the file is, nor
whether `start()` refuses or goes ahead: only that the host file is
untouched, that nothing hangs, and — when `start()` does go ahead — that the
agent still gets its environment.

`start()` is driven as `test_phase0_versioned_mount.py`'s `_issued_command`
does: the container is taken as up, and the process creation is captured
rather than run. `ContainerGit` talks to a fake `docker` binary on PATH.

The timeout test's fake `docker exec` behaves as the real one does in the way
that matters: the command it runs is NOT its own child in any sense the host
can kill — it is started in its own session and survives the client being
killed. Any further `docker exec <container> ...` (a kill, say) runs on this
machine, whose pid namespace the fake "container" shares.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import multiagents.executor.docker as docker_mod
from multiagents.executor.docker import ContainerGit, DockerExecutor
from multiagents.paths import ProjectPaths

AGENT = "ag-e7f11e"
HOST_BYTES = b"HOST_SECRET=do-not-touch\n"
JOIN = 10.0


@pytest.fixture(autouse=True)
def _outside(monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)


def executor(tmp_path: Path) -> DockerExecutor:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    return DockerExecutor({"image": "img", "network": "bridge"}, paths=paths)


def attacked_file(ex: DockerExecutor) -> Path:
    """Today's env file: under `.multiagents`, which the container writes."""
    return ex.paths.data / "env" / f"{AGENT}.env"


def host_file(tmp_path: Path, name: str = "host-secret") -> Path:
    """A host file outside the project, which the agent wants written."""
    target = tmp_path / "outside" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(HOST_BYTES)
    return target


def link_file(ex: DockerExecutor, target: Path) -> None:
    path = attacked_file(ex)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.symlink_to(target)


def link_dir(ex: DockerExecutor, target_dir: Path) -> None:
    env_dir = attacked_file(ex).parent
    if env_dir.is_symlink() or env_dir.is_file():
        env_dir.unlink()
    elif env_dir.is_dir():
        for child in env_dir.iterdir():
            child.unlink()
        env_dir.rmdir()
    target_dir.mkdir(parents=True, exist_ok=True)
    env_dir.symlink_to(target_dir, target_is_directory=True)


def in_thread(fn):
    """Run `fn` in a daemon thread; `(finished, result, exception)`."""
    box: dict = {}

    def body():
        try:
            box["result"] = fn()
        except BaseException as exc:          # noqa: BLE001 — reported below
            box["error"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    thread.join(JOIN)
    return (not thread.is_alive()), box.get("result"), box.get("error")


# --- start(): what it writes ---------------------------------------------------

def start(ex: DockerExecutor, monkeypatch, env: dict[str, str]) -> dict:
    """`start()` for AGENT with the container taken as up. Returns what the
    `docker exec` it issued was handed: its argv and, if it named an
    `--env-file`, that file's text as it stood at that moment."""
    seen: dict = {}

    async def fake_exec(*command, **kw):
        argv = [str(c) for c in command]
        seen["command"] = argv
        if "--env-file" in argv:
            path = Path(argv[argv.index("--env-file") + 1])
            seen["env_file"] = path
            seen["env_text"] = path.read_text() if path.is_file() else None
        return SimpleNamespace(pid=4242, stdout=None, stderr=None, returncode=None)

    monkeypatch.setattr(docker_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container,
                                      "existed": True})
    asyncio.run(ex.start(["true"], ex.paths.root,
                         {"MULTIAGENTS_AGENT_ID": AGENT, **env}))
    return seen


def assert_agent_got_its_environment(seen: dict, host_target: Path) -> None:
    """When `start()` went ahead, the agent's environment still reached
    `docker exec`, and not through the host file."""
    command = seen.get("command")
    assert command, "start() returned without issuing docker exec"
    if "env_file" in seen:
        assert seen["env_file"].resolve() != host_target.resolve(), \
            "docker exec was handed the host file as its env file"
        assert seen["env_text"] is not None, "the env file handed over is not a file"
        assert "AGENT_MARKER=from-start" in seen["env_text"].splitlines()
    else:
        assert "AGENT_MARKER=from-start" in command


def test_control_sg_r7_start_writes_the_agent_env_where_the_container_writes(
        tmp_path, monkeypatch):
    """The attack surface is live today: `start()` writes under
    `.multiagents/env`. (Passes today; if the file moves to a host-only
    place this control may go red, and the SG-R7 tests below become inert,
    which the contract allows.)"""
    ex = executor(tmp_path)
    seen = start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    assert attacked_file(ex).is_file()
    assert_agent_got_its_environment(seen, tmp_path / "nowhere")


def test_sg_r7_start_does_not_write_through_a_symlinked_env_file(tmp_path, monkeypatch):
    ex = executor(tmp_path)
    target = host_file(tmp_path)
    link_file(ex, target)
    try:
        seen = start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    except Exception:                          # refusing is allowed
        seen = None
    assert target.read_bytes() == HOST_BYTES, \
        "start() wrote the agent's env into a host file through a symlink"
    if seen is not None:
        assert_agent_got_its_environment(seen, target)


def test_sg_r7_start_does_not_chmod_a_host_file_through_a_symlink(tmp_path, monkeypatch):
    """`start()` also chmods the env file: that must not reach the host
    file either."""
    ex = executor(tmp_path)
    target = host_file(tmp_path)
    target.chmod(0o644)
    link_file(ex, target)
    try:
        start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    except Exception:
        pass
    assert target.stat().st_mode & 0o777 == 0o644, \
        "start() changed a host file's mode through a symlink"


def test_sg_r7_start_does_not_write_through_a_symlinked_env_dir(tmp_path, monkeypatch):
    """The directory holding the env file is a symlink to a host directory
    that already has a file of that name."""
    ex = executor(tmp_path)
    outside = tmp_path / "outside-dir"
    link_dir(ex, outside)
    target = outside / f"{AGENT}.env"
    target.write_bytes(HOST_BYTES)
    try:
        seen = start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    except Exception:
        seen = None
    assert target.read_bytes() == HOST_BYTES, \
        "start() wrote into a host directory through a symlinked env dir"
    if seen is not None:
        assert_agent_got_its_environment(seen, target)


def test_sg_r7_start_creates_nothing_in_a_host_dir_behind_a_symlinked_env_dir(
        tmp_path, monkeypatch):
    ex = executor(tmp_path)
    outside = tmp_path / "outside-dir"
    link_dir(ex, outside)
    try:
        start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    except Exception:
        pass
    assert list(outside.iterdir()) == [], \
        f"start() created {sorted(p.name for p in outside.iterdir())} in a host dir"


def test_sg_r7_start_does_not_hang_on_a_fifo_env_file(tmp_path, monkeypatch):
    ex = executor(tmp_path)
    path = attacked_file(ex)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    os.mkfifo(path)
    finished, _, _ = in_thread(
        lambda: start(ex, monkeypatch, {"AGENT_MARKER": "from-start"}))
    if not finished:
        # Unblock the stuck writer so the thread does not outlive the test.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
    assert finished, "start() hung on a FIFO planted as the agent's env file"


def test_sg_r7_start_does_not_hang_on_a_fifo_behind_a_symlinked_env_dir(
        tmp_path, monkeypatch):
    ex = executor(tmp_path)
    outside = tmp_path / "outside-dir"
    link_dir(ex, outside)
    fifo = outside / f"{AGENT}.env"
    os.mkfifo(fifo)
    finished, _, _ = in_thread(
        lambda: start(ex, monkeypatch, {"AGENT_MARKER": "from-start"}))
    if not finished:
        try:
            os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    assert finished, "start() hung on a FIFO behind a symlinked env dir"


# --- ContainerGit: what it reads -----------------------------------------------

def test_control_sg_r7_container_git_reads_back_what_start_wrote(tmp_path, monkeypatch):
    """Not vacuous: the environment `start()` handed the agent is the one
    `ContainerGit` runs git with. (Passes today.)"""
    ex = executor(tmp_path)
    start(ex, monkeypatch, {"AGENT_MARKER": "from-start"})
    assert ContainerGit(ex, AGENT).environ().get("AGENT_MARKER") == "from-start"


def test_sg_r7_container_git_does_not_read_a_host_file_through_a_symlink(tmp_path):
    ex = executor(tmp_path)
    link_file(ex, host_file(tmp_path))
    finished, env, error = in_thread(lambda: ContainerGit(ex, AGENT).environ())
    assert finished, "reading the env file hung"
    assert error is None, error
    assert "HOST_SECRET" not in env, \
        "ContainerGit read a host file through a symlinked env file"


def test_sg_r7_container_git_does_not_read_through_a_symlinked_env_dir(tmp_path):
    ex = executor(tmp_path)
    outside = tmp_path / "outside-dir"
    link_dir(ex, outside)
    (outside / f"{AGENT}.env").write_bytes(HOST_BYTES)
    finished, env, error = in_thread(lambda: ContainerGit(ex, AGENT).environ())
    assert finished, "reading the env file hung"
    assert error is None, error
    assert "HOST_SECRET" not in env, \
        "ContainerGit read a host directory's file through a symlinked env dir"


def test_sg_r7_container_git_does_not_hang_on_a_fifo_behind_a_symlinked_env_dir(tmp_path):
    ex = executor(tmp_path)
    outside = tmp_path / "outside-dir"
    link_dir(ex, outside)
    os.mkfifo(outside / f"{AGENT}.env")
    finished, _, error = in_thread(lambda: ContainerGit(ex, AGENT).environ())
    assert finished, "ContainerGit hung on a FIFO behind a symlinked env dir"
    assert error is None, error


# --- a docker exec timeout leaves no git running ---------------------------------

FAKE_DOCKER = r'''#!{python}
import os, sys
argv = sys.argv[1:]
if argv[:1] != ["exec"]:
    sys.exit(0)
rest = argv[1:]
workdir, env_file, detached_ok = None, None, True
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if flag in ("--workdir", "-w"):
        workdir = rest.pop(0)
    elif flag == "--env-file":
        env_file = rest.pop(0)
    elif flag in ("--user", "-u", "--env", "-e"):
        rest.pop(0)
env = dict(os.environ)
if env_file:
    for line in open(env_file).read().splitlines():
        k, _, v = line.partition("=")
        env[k] = v
command = rest[1:]
if workdir:
    os.chdir(workdir)
# As the real daemon: the command runs in the container, not as this
# client's child. Killing the client leaves it running.
pid = os.fork()
if pid == 0:
    os.setsid()
    os.execvpe(command[0], command, env)
with open({pids!r}, "a") as fh:
    fh.write(f"{{pid}}\n")
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
'''


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def test_sg_r7_a_docker_exec_timeout_leaves_no_git_running(tmp_path, monkeypatch):
    """`ContainerGit.run` with a timeout, on a git command that hangs (an
    alias sleeping in a shell git started): once the call is over, whatever
    raised or returned, neither git nor what it started is still running."""
    pids = tmp_path / "container-pids"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable, pids=str(pids)))
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    ex = executor(tmp_path)
    repo = ex.paths.root
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env_file = attacked_file(ex)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"PATH={os.environ['PATH']}\nHOME={tmp_path}\n"
                        f"GIT_CONFIG_NOSYSTEM=1\n")
    child = tmp_path / "alias-pid"
    hang = f"!echo $$ > {child}; exec sleep 60"

    started = time.monotonic()
    try:
        ContainerGit(ex, AGENT).run(repo, "-c", f"alias.hang={hang}", "hang",
                                    timeout=2)
    except subprocess.TimeoutExpired:
        pass
    elapsed = time.monotonic() - started
    recorded = [int(line) for line in pids.read_text().split()] if pids.exists() else []
    if child.exists():
        recorded.append(int(child.read_text().strip()))
    try:
        assert elapsed < 30, f"ContainerGit.run ignored its timeout ({elapsed:.0f}s)"
        assert len(recorded) == 2, f"fixture: the hang never started ({recorded})"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(p) for p in recorded):
            time.sleep(0.1)
        left = [p for p in recorded if alive(p)]
        assert not left, (f"a docker exec timeout left {len(left)} process(es) "
                          f"of the git call running in the container")
    finally:
        for p in recorded:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
