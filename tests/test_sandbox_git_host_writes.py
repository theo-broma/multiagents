"""SG-R7 (remainder) — the host writes nothing under `.multiagents` through a
link, and blocks on no FIFO there.

Contract: context/specs/sandbox-git.md.

- SG-R7 (Decisions "after implementer ag-ce8b54 on SG-R3"): files the host
  writes or reads for an agent never sit where the container can write, or
  are created with no-follow semantics, never through a pre-existing
  symlink, and read bounded and non-blocking.
- Decisions "after implementers ag-3c75c1 and ag-b65538": every file or
  directory the HOST creates or writes under `.multiagents` is written with
  the same no-follow primitives. That includes the run-dir `mkdir`s, the pid
  file (`_pid_file`), and `scratch()`.

`.multiagents` is writable from the container (SG-R2 keeps its runtime state
writable), so an agent can replace any path in it, or any directory on the
way to it, with a symlink to a host location, or with a FIFO. Each attack
here plants such a thing at `<project>/.multiagents/...`, pointing at a
directory or file under `tmp_path/outside`, then drives the smallest public
entry point that writes there. The assertion is always the same: nothing is
created, changed or removed at the outside location, and nothing hangs.
Whether the host refuses (raises) or goes ahead somewhere safe is its choice
— refusing is allowed, so each attack tolerates an exception.

Covered, by entry point:

- `DockerExecutor.start(argv, cwd, env)` — creates the agent's run dir for
  its pid file.
- `DockerExecutor.start(..., run_dir=...)` (the launch wrapper, as the runner
  starts every agent) — creates the run dir, clears `exit_status`, and
  terminates `output.ndjson`'s last line.
- `DockerHandle.stop()` — reads the pid file the agent's shell wrote.
- `ContainerGit.scratch()` — a directory in the agent's run dir.
- `Runner` spawning a docker agent (through `server.start_agent`) — its run
  dir and everything written in it (prompt, command, lock, stream log).
- `Tree` — `events.jsonl`, `tree.json` and its `.tmp`, `.bak` and `.lock`
  companions, all under `.multiagents`: the spec's "every file". These go
  beyond the three named examples; they are here because "every" covers
  them, and a FIFO as `events.jsonl` blocks every event the host emits.

Each group has a clean control, which passes today, showing the write the
attack redirects really happens. FIFO cases where today's code happens not to
block are kept as guards (said so in each), because the obvious no-follow
rewrite — `O_NOFOLLOW` without `O_NONBLOCK` — would make them hang.

Not covered: the per-agent run dir of a NEW spawn replaced by a link — its id
is chosen at spawn time, so the agent cannot plant it; the `runs` directory
above it covers that route.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents import server
from multiagents.executor.docker import ContainerGit, DockerExecutor
from multiagents.paths import ProjectPaths
from multiagents.tree import Node, Tree

AGENT = "ag-51d7a3"
HOST_BYTES = b"HOST_SECRET=do-not-touch"      # no trailing newline, on purpose
JOIN = 10.0


@pytest.fixture(autouse=True)
def _outside(monkeypatch):
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)


@pytest.fixture
def docker_up(tmp_path, monkeypatch):
    """The container taken as up, and a `docker` that does nothing and
    exits 0: nothing here needs the agent to run."""
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container,
                                      "existed": True})
    bindir = tmp_path / "host-bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


# --- helpers -------------------------------------------------------------------

def executor(tmp_path: Path) -> DockerExecutor:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    return DockerExecutor({"image": "img", "network": "bridge"}, paths=paths)


def outside(tmp_path: Path, name: str = "outside") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def listing(directory: Path) -> list[str]:
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


def replace_with_link(path: Path, target: Path) -> None:
    """What the agent does from the container: whatever is at `path` goes,
    and a symlink to `target` takes its place."""
    if path.is_symlink() or path.is_file() or path.is_fifo():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=target.is_dir())


def replace_with_fifo(path: Path) -> None:
    if path.is_symlink() or path.is_file() or path.is_fifo():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


def unblock(fifo: Path) -> None:
    """Release a thread stuck opening `fifo`, whichever end it wanted."""
    for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
        try:
            os.close(os.open(fifo, flags))
        except OSError:
            pass


def in_thread(fn, fifo: Path | None = None):
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
    finished = not thread.is_alive()
    if not finished and fifo is not None:
        unblock(fifo)
        thread.join(2)
    return finished, box.get("result"), box.get("error")


def attempt(fn):
    """`fn()`; refusing, by raising, is an allowed answer to an attack."""
    try:
        return fn()
    except Exception:                          # noqa: BLE001
        return None


def env() -> dict[str, str]:
    return {"MULTIAGENTS_AGENT_ID": AGENT}


def start(ex: DockerExecutor):
    return asyncio.run(ex.start(["true"], ex.paths.root, env()))


def start_wrapped(ex: DockerExecutor):
    return asyncio.run(ex.start(["true"], ex.paths.root, env(),
                                run_dir=ex.paths.run_dir(AGENT), deadline=0))


# --- start(): the run dir the pid file goes in -----------------------------------

def test_control_sg_r7_start_creates_the_agents_run_dir(tmp_path, docker_up):
    ex = executor(tmp_path)
    start(ex)
    run_dir = ex.paths.run_dir(AGENT)
    assert run_dir.is_dir() and not run_dir.is_symlink()


def test_sg_r7_start_creates_nothing_behind_a_symlinked_runs_dir(tmp_path, docker_up):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    replace_with_link(ex.paths.runs, target)
    attempt(lambda: start(ex))
    assert listing(target) == [], \
        f"start() created {listing(target)} in a host dir behind .multiagents/runs"


# --- start(run_dir=...): the launch wrapper's run dir ------------------------------

def test_control_sg_r7_wrapped_start_writes_in_the_run_dir(tmp_path, docker_up):
    """The writes the attacks below redirect: `exit_status` is cleared and
    an unterminated last line of `output.ndjson` is terminated."""
    ex = executor(tmp_path)
    run_dir = ex.paths.run_dir(AGENT)
    run_dir.mkdir(parents=True)
    (run_dir / "output.ndjson").write_bytes(b'{"a": 1}')
    (run_dir / "exit_status").write_text("0\n")
    start_wrapped(ex)
    assert (run_dir / "output.ndjson").read_bytes() == b'{"a": 1}\n'
    assert not (run_dir / "exit_status").exists()


def test_sg_r7_wrapped_start_touches_nothing_behind_a_symlinked_run_dir(
        tmp_path, docker_up):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    (target / "output.ndjson").write_bytes(HOST_BYTES)
    (target / "exit_status").write_bytes(HOST_BYTES)
    replace_with_link(ex.paths.run_dir(AGENT), target)
    attempt(lambda: start_wrapped(ex))
    assert listing(target) == ["exit_status", "output.ndjson"], \
        f"start() created or removed files in a host dir: {listing(target)}"
    assert (target / "output.ndjson").read_bytes() == HOST_BYTES, \
        "start() wrote into a host file through a symlinked run dir"
    assert (target / "exit_status").read_bytes() == HOST_BYTES


def test_sg_r7_wrapped_start_creates_nothing_behind_a_symlinked_runs_dir(
        tmp_path, docker_up):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    replace_with_link(ex.paths.runs, target)
    attempt(lambda: start_wrapped(ex))
    assert listing(target) == [], \
        f"start() created {listing(target)} in a host dir behind .multiagents/runs"


def test_sg_r7_wrapped_start_does_not_write_through_a_symlinked_output_file(
        tmp_path, docker_up):
    ex = executor(tmp_path)
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(ex.paths.run_dir(AGENT) / "output.ndjson", host_file)
    attempt(lambda: start_wrapped(ex))
    assert host_file.read_bytes() == HOST_BYTES, \
        "start() wrote into a host file through a symlinked output.ndjson"


def test_sg_r7_wrapped_start_does_not_hang_on_a_fifo_output_file(tmp_path, docker_up):
    """Guard: passes today (the file is opened read-write, which a FIFO does
    not block); a write-only no-follow open would hang here."""
    ex = executor(tmp_path)
    fifo = ex.paths.run_dir(AGENT) / "output.ndjson"
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: start_wrapped(ex), fifo)
    assert finished, "start() hung on a FIFO planted as output.ndjson"


# --- DockerHandle.stop(): the pid file ---------------------------------------------

def stop_after(ex: DockerExecutor, plant) -> None:
    """Start the agent, let `plant` change its run dir, then stop it."""
    async def go():
        handle = await ex.start(["true"], ex.paths.root, env())
        plant()
        await handle.stop(grace=0)
    asyncio.run(go())


def test_control_sg_r7_docker_handle_stop_returns_with_a_pid_file(tmp_path, docker_up):
    ex = executor(tmp_path)
    pid_file = ex.paths.run_dir(AGENT) / "container.pid"
    finished, _, error = in_thread(
        lambda: stop_after(ex, lambda: pid_file.write_text("4242\n")))
    assert finished and error is None, error


def test_sg_r7_docker_handle_stop_does_not_hang_on_a_fifo_pid_file(tmp_path, docker_up):
    ex = executor(tmp_path)
    fifo = ex.paths.run_dir(AGENT) / "container.pid"
    finished, _, _ = in_thread(lambda: stop_after(ex, lambda: replace_with_fifo(fifo)),
                               fifo)
    assert finished, "DockerHandle.stop() hung on a FIFO planted as the pid file"


def test_sg_r7_docker_handle_stop_does_not_hang_on_a_fifo_behind_a_symlinked_run_dir(
        tmp_path, docker_up):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    fifo = target / "container.pid"

    def plant():
        replace_with_link(ex.paths.run_dir(AGENT), target)
        os.mkfifo(fifo)

    finished, _, _ = in_thread(lambda: stop_after(ex, plant), fifo)
    assert finished, "DockerHandle.stop() hung on a FIFO behind a symlinked run dir"


# --- ContainerGit.scratch() --------------------------------------------------------

def use_scratch(ex: DockerExecutor, watch: Path | None = None) -> dict:
    """Enter and leave `scratch()`, writing a file in it as a hook's trace
    would. What `watch` held while it was open, and the directory used."""
    seen: dict = {}
    with ContainerGit(ex, AGENT).scratch() as where:
        where = Path(where)
        (where / "trace").write_text("t\n")
        seen["dir"] = where
        if watch is not None:
            seen["during"] = listing(watch)
    return seen


def test_control_sg_r7_scratch_is_a_usable_directory_removed_after(tmp_path):
    ex = executor(tmp_path)
    seen = use_scratch(ex)
    assert not seen["dir"].exists()


def test_sg_r7_scratch_creates_nothing_behind_a_symlinked_run_dir(tmp_path):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    replace_with_link(ex.paths.run_dir(AGENT), target)
    seen = attempt(lambda: use_scratch(ex, target)) or {}
    assert seen.get("during", []) == [], \
        f"scratch() created {seen['during']} in a host dir behind the run dir"
    assert listing(target) == []


def test_sg_r7_scratch_creates_nothing_behind_a_symlinked_runs_dir(tmp_path):
    ex = executor(tmp_path)
    target = outside(tmp_path)
    replace_with_link(ex.paths.runs, target)
    seen = attempt(lambda: use_scratch(ex, target)) or {}
    assert seen.get("during", []) == [], \
        f"scratch() created {seen['during']} in a host dir behind .multiagents/runs"
    assert listing(target) == []


def test_sg_r7_scratch_does_not_hang_on_a_fifo_run_dir(tmp_path):
    """Guard: passes today (`mkdir` does not open what is there)."""
    ex = executor(tmp_path)
    fifo = ex.paths.run_dir(AGENT)
    replace_with_fifo(fifo)
    finished, _, _ = in_thread(lambda: attempt(lambda: use_scratch(ex)), fifo)
    assert finished, "scratch() hung on a FIFO planted as the run dir"


# --- the runner, spawning a docker agent -------------------------------------------

def spawn(project) -> tuple[str | None, dict]:
    """`server.start_agent` for the leaf; `(agent id or None, the result)`
    once the run is over. A refusal is a result, not a failure."""
    from test_subagent_mcp import TASK

    async def go():
        try:
            result = await server.start_agent("leaf", TASK)
        except Exception as exc:               # noqa: BLE001 — refusing is allowed
            return None, {"error": repr(exc)}
        agent_id = result.get("agent_id") if isinstance(result, dict) else None
        deadline = time.monotonic() + 30
        while agent_id and time.monotonic() < deadline:
            node = server.runner().tree.get(agent_id)
            if node is not None and node.status not in ("pending", "running"):
                break
            await asyncio.sleep(0.1)
        return agent_id, result

    try:
        return asyncio.run(go())
    finally:
        server._reset()


def docker_project(tmp_path, monkeypatch):
    """test_subagent_mcp's docker project, but with a `docker` whose `exec`
    runs nothing and fails: the agent never starts, so every file that
    appears is one the HOST wrote. (Its own fake runs the wrapper on this
    machine's filesystem, where the container's writes through a link would
    be indistinguishable from the host's.)"""
    from test_subagent_mcp import Project
    project = Project(tmp_path, monkeypatch, "claude", "docker")
    bindir = tmp_path / "noop-docker-bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/sh\n[ "$1" = exec ] && exit 1\nexit 0\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container,
                                      "existed": True})
    return project


def test_control_sg_r7_a_docker_spawn_writes_its_run_dir_under_multiagents(
        tmp_path, monkeypatch):
    project = docker_project(tmp_path, monkeypatch)
    agent_id, result = spawn(project)
    assert agent_id, result
    run_dir = project.paths.run_dir(agent_id)
    assert run_dir.is_dir() and not run_dir.is_symlink()
    assert (run_dir / "prompt.md").is_file(), sorted(os.listdir(run_dir))


def test_sg_r7_a_docker_spawn_creates_nothing_behind_a_symlinked_runs_dir(
        tmp_path, monkeypatch):
    project = docker_project(tmp_path, monkeypatch)
    project.paths.ensure()
    target = outside(tmp_path)
    replace_with_link(project.paths.runs, target)
    spawn(project)
    assert listing(target) == [], \
        f"the runner wrote {listing(target)} in a host dir behind .multiagents/runs"


# --- the tree and the event log ----------------------------------------------------

def tree(tmp_path: Path) -> tuple[Tree, ProjectPaths]:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    paths = ProjectPaths(root)
    paths.ensure()
    return Tree(paths.tree_file, paths.events_file), paths


def add_node(t: Tree, node_id: str = AGENT) -> None:
    t.add(Node(id=node_id, agent="leaf", provider="p", model="m", parent=None,
               depth=1, task="t"))


def test_control_sg_r7_tree_writes_under_multiagents(tmp_path):
    t, paths = tree(tmp_path)
    add_node(t, "ag-000001")
    add_node(t, "ag-000002")
    t.emit(AGENT, "probe")
    assert AGENT in paths.events_file.read_text()
    assert "ag-000002" in paths.tree_file.read_text()
    assert t.get("ag-000001") is not None


def test_sg_r7_emit_does_not_append_through_a_symlinked_event_log(tmp_path):
    t, paths = tree(tmp_path)
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(paths.events_file, host_file)
    t.emit(AGENT, "probe")
    assert host_file.read_bytes() == HOST_BYTES, \
        "an event was appended to a host file through a symlinked events.jsonl"


def test_sg_r7_emit_does_not_hang_on_a_fifo_event_log(tmp_path):
    t, paths = tree(tmp_path)
    replace_with_fifo(paths.events_file)
    finished, _, _ = in_thread(lambda: t.emit(AGENT, "probe"), paths.events_file)
    assert finished, "emitting an event hung on a FIFO planted as events.jsonl"


def test_sg_r7_a_tree_write_does_not_create_a_host_file_through_the_lock(tmp_path):
    t, paths = tree(tmp_path)
    target = outside(tmp_path)
    replace_with_link(paths.tree_file.with_suffix(".lock"), target / "planted")
    attempt(lambda: add_node(t))
    assert listing(target) == [], \
        f"a tree write created {listing(target)} through a symlinked tree.lock"


def test_sg_r7_a_tree_write_does_not_write_through_its_temp_file(tmp_path):
    t, paths = tree(tmp_path)
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(paths.tree_file.with_suffix(".tmp"), host_file)
    attempt(lambda: add_node(t))
    assert host_file.read_bytes() == HOST_BYTES, \
        "a tree write wrote a host file through a symlinked tree.tmp"


def test_sg_r7_a_tree_write_does_not_write_through_its_backup(tmp_path):
    t, paths = tree(tmp_path)
    add_node(t, "ag-000001")                   # a tree to back up
    host_file = outside(tmp_path) / "host-file"
    host_file.write_bytes(HOST_BYTES)
    replace_with_link(paths.tree_file.with_name(paths.tree_file.name + ".bak"),
                      host_file)
    attempt(lambda: add_node(t))
    assert host_file.read_bytes() == HOST_BYTES, \
        "a tree write copied the tree over a host file through a symlinked .bak"
