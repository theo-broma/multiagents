"""Stopping a docker agent kills its processes, in an image without `pkill`.

Contract: context/specs/sandbox-git.md, Decisions "after implementers ag-3c75c1
and ag-b65538": the workspace image has no `pkill` (its Dockerfile installs no
`procps`), yet `DockerHandle.stop` and `_KILL_SCRIPT` use it. If stopping a
docker agent is silently broken, it is fixed (the spec names a `/proc` walk,
as `ContainerGit` uses; the mechanism is not asserted here).

The public ways a docker agent is stopped from the host (`ROUTES`):

- `DockerHandle.stop()` — the handle `DockerExecutor.start()` returns when it
  started the agent itself;
- `DockerExecutor.kill_detached()` — from the recorded pid file, for an agent
  this process did not start (a restart, a nested server). `_KILL_SCRIPT`
  documents that it ends "the group, the agent and its direct children";
- the `FollowHandle.stop()` of an agent started under the launch wrapper
  (`start(..., run_dir=...)`, as the runner starts every agent), which goes
  through `kill_detached` too.

What "killed" means here: once the stop returns (plus a short settling
window), neither the agent nor any process it started directly is running.
A child that left the agent's process group (`setsid`) is still a direct
child, and is what `pkill -P` was there for.

The fake `docker` on PATH behaves as the real one does in the ways that
matter (as in test_sandbox_git_env_files.py): `exec` runs its command in a
new session, not as a child the host can signal, and the command's PATH is
the CONTAINER's — a directory holding only the tools the image has, and no
`pkill`. Its `sh` is this machine's, dash on Ubuntu, as in the image.

Found while writing these (2026-09-27), and why the failures are not only
about `pkill`:

- dash's builtin `kill` rejects `--` ("Illegal number: -"), so
  `_KILL_SCRIPT`'s `kill -SIG -- -$a` never signals the group; the image has
  no `/bin/kill` to fall back on either.
- `kill $a` then ends the agent, and its children are reparented at once, so
  a `pkill -P $a` run after it finds nothing even where `pkill` exists.
  With `pkill` put into the fake image, the same cases fail the same way.
- The wrapped route (`start(..., run_dir=...)`, what the runner uses) ends
  the process group anyway: `kill $w` reaches the Python wrapper, which
  forwards the signal with `killpg`. That case is a green guard.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import multiagents.executor.docker as docker_mod
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths

AGENT = "ag-5709fe"
SETTLE = 5.0

# What the workspace image has that these scripts use. `kill` is a builtin of
# its `sh` (dash).
IMAGE_TOOLS = ("sh", "sleep", "cat", "cut", "sed", "setsid")

FAKE_DOCKER = r'''#!{python}
import os, sys
argv = sys.argv[1:]
if argv[:1] != ["exec"]:
    sys.exit(0)
rest = argv[1:]
workdir, env_file = None, None
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if flag in ("--workdir", "-w"):
        workdir = rest.pop(0)
    elif flag == "--env-file":
        env_file = rest.pop(0)
    elif flag in ("--user", "-u", "--env", "-e"):
        rest.pop(0)
env = {{}}
if env_file:
    for line in open(env_file).read().splitlines():
        k, _, v = line.partition("=")
        if k:
            env[k] = v
# The container's own PATH: whatever the host handed over names host
# directories, which the image does not have.
env["PATH"] = {container_path!r}
command = rest[1:]
pid = os.fork()
if pid == 0:
    os.setsid()
    if workdir:
        os.chdir(workdir)
    os.execvpe(command[0], command, env)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
'''


@pytest.fixture(autouse=True)
def _outside(monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container,
                                      "existed": True})


def image(tmp_path: Path, monkeypatch, *, pkill: bool) -> None:
    """A fake `docker` on the host's PATH, whose container has `IMAGE_TOOLS`
    (and `pkill`, only when asked: nothing here asks) and nothing else."""
    container_bin = tmp_path / "image-bin"
    container_bin.mkdir()
    for tool in IMAGE_TOOLS + (("pkill",) if pkill else ()):
        found = shutil.which(tool)
        if found is None:
            pytest.skip(f"fixture: this machine has no {tool}")
        (container_bin / tool).symlink_to(found)
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    docker = host_bin / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable,
                                         container_path=str(container_bin)))
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{host_bin}{os.pathsep}{os.environ['PATH']}")


def executor(tmp_path: Path) -> DockerExecutor:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths(root)
    paths.ensure()
    return DockerExecutor({"image": "img", "network": "bridge"}, paths=paths)


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def wait_for(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text().strip()
        except OSError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.05)
    raise AssertionError(f"fixture: {path.name} never appeared")


def agent_script(tmp_path: Path) -> list[str]:
    """An agent with two direct children: one in its process group (a tool
    it runs) and one that left it (`setsid`, as a daemonising tool does)."""
    in_group, left = tmp_path / "child-in-group", tmp_path / "child-left-group"
    return ["sh", "-c",
            f"sleep 300 & echo $! > {in_group}; "
            f"setsid sleep 300 & echo $! > {left}; wait"]


ROUTES = ["docker_handle_stop", "kill_detached", "wrapped_stop"]


async def started(ex: DockerExecutor, tmp_path: Path, route: str):
    """Start the agent as `route` does; `(handle, {name: pid})` once the agent
    and both its children run."""
    env = {"MULTIAGENTS_AGENT_ID": AGENT, "HOME": str(tmp_path)}
    run_dir = ex.paths.run_dir(AGENT) if route == "wrapped_stop" else None
    handle = await ex.start(agent_script(tmp_path), tmp_path, env, run_dir=run_dir)
    pids = {}
    for name in ("child-in-group", "child-left-group"):
        pids[name] = await asyncio.to_thread(wait_for, tmp_path / name)
    # The agent is the children's parent, whichever pid file recorded it.
    pids["agent"] = int(Path(f"/proc/{pids['child-in-group']}/stat").read_text()
                        .rsplit(")", 1)[1].split()[1])
    for name, pid in pids.items():
        assert alive(pid), f"fixture: the {name} is not running before the stop"
    return handle, pids


async def stop(ex: DockerExecutor, handle, route: str) -> None:
    """Stop the agent the way `route` does, from the host."""
    if route == "kill_detached":
        # An agent this process did not start (a restart, a nested server):
        # all that is left is the pid file.
        assert await asyncio.to_thread(ex.kill_detached, AGENT, 1), \
            "kill_detached reported failure"
    else:
        await handle.stop(grace=1)


def run_and_stop(tmp_path: Path, monkeypatch, route: str) -> dict[str, int]:
    """Start and stop the agent through `route`; the pids it had. The caller
    must `cleanup` them."""
    image(tmp_path, monkeypatch, pkill=False)
    ex = executor(tmp_path)
    box: dict = {"pids": {}}

    async def go():
        handle, pids = await started(ex, tmp_path, route)
        box["handle"], box["pids"] = handle, pids
        await stop(ex, handle, route)

    try:
        asyncio.run(go())
    except BaseException:
        cleanup(box.get("handle"), box["pids"])
        raise
    return box


def assert_ended(pids: dict[str, int], names: list[str], how: str) -> None:
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline and any(alive(pids[n]) for n in names):
        time.sleep(0.1)
    left = [n for n in names if alive(pids[n])]
    assert not left, f"after {how}, still running in the container: {left}"


def cleanup(handle, pids: dict[str, int]) -> None:
    for pid in [*pids.values(), getattr(handle, "pid", None)]:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_control_sg_r7_the_image_installs_no_pkill():
    """The premise, checked on the shipped Dockerfile: nothing it installs
    provides `pkill` (Debian/Ubuntu's `procps`)."""
    dockerfile = (Path(docker_mod.__file__).resolve().parents[1]
                  / "defaults" / "docker" / "Dockerfile")
    text = dockerfile.read_text()
    assert "procps" not in text and "pkill" not in text


def test_control_sg_r7_the_fake_container_has_no_pkill(tmp_path, monkeypatch):
    """The fixture models the image: a command run through `docker exec`
    finds no `pkill`, while the tools the stop scripts use are there."""
    image(tmp_path, monkeypatch, pkill=False)
    probe = subprocess.run(
        ["docker", "exec", "c", "sh", "-c",
         "command -v pkill && exit 9; for t in cat cut sleep; do "
         "command -v $t >/dev/null || exit 8; done; exit 0"],
        capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, (probe.returncode, probe.stdout, probe.stderr)


@pytest.mark.parametrize("route", ROUTES)
def test_sg_r7_stopping_a_docker_agent_ends_it_and_its_process_group(
        tmp_path, monkeypatch, route):
    """The agent and the child it runs in its own process group (a shell
    tool, a test runner) are gone once the stop returns."""
    box = run_and_stop(tmp_path, monkeypatch, route)
    try:
        assert_ended(box["pids"], ["agent", "child-in-group"], route)
    finally:
        cleanup(box.get("handle"), box["pids"])


@pytest.mark.parametrize("route", ROUTES)
def test_sg_r7_stopping_a_docker_agent_ends_a_direct_child_that_left_its_group(
        tmp_path, monkeypatch, route):
    """What `pkill -P` was in the stop scripts for: a direct child of the
    agent in a session of its own. `_KILL_SCRIPT` documents that it ends
    "the agent and its direct children"."""
    box = run_and_stop(tmp_path, monkeypatch, route)
    try:
        assert_ended(box["pids"], ["child-left-group"], route)
    finally:
        cleanup(box.get("handle"), box["pids"])
