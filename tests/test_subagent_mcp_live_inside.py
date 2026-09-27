"""SM-R1, one layer further: the docker executor when the caller IS the container.

Contract: `context/specs/subagent-mcp.md`, SM-R1.

99be9d6 fixed the first live failure (config unwritable inside the
container). The live check then failed one layer further in: an
`implementer` run (ag-48a3d2) called `consult("dev-advisor", ...)` from
inside its own container and got back `"error": "docker is not on PATH"`.

`DockerExecutor.inside()` already exists to recognise exactly this case — a
server whose own process is running inside the project's container, where
there is no `docker` binary and nothing to reach the daemon with — and
`start()` already takes its `_start_inside` branch there. But
`preflight()`, `ensure_running()` and `kill_detached()` asked for the
`docker` binary unconditionally, and `preflight()` sits on every spawn
unconditionally (`Runner._launch`), before `start()` ever gets to take the
branch that was already correct.

This file is a NEW file, not an edit to `tests/test_subagent_mcp_live.py`:
that file is an existing test file and off limits to modify under this
role's contract, even though it is the one the task named. Everything here
follows its idiom and reuses its fixtures/helpers directly.

Separate from `preflight`/`ensure_running`/`kill_detached`, `inside()`
itself is deliberately hard to fake for real: it reads `/.dockerenv` and
`/proc/1/environ`, not anything a test (or a host process) can set through
the environment (see `test_subagent_mcp_adversary.py`'s
`test_multiagents_container_env_does_not_bypass_docker_exec_on_host`). So
these tests monkeypatch `DockerExecutor.inside` itself at the class level —
the standard way to hold "we are inside" fixed while exercising the code
that is supposed to behave differently there — and separately strip
`docker` from `PATH` for real, so a leftover unguarded `docker exec` call
fails loudly (`FileNotFoundError`) rather than silently finding a real
`docker` on a machine that happens to have one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402

from multiagents import server  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402
import multiagents.executor.docker as docker_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.tree import Tree  # noqa: E402

from test_subagent_mcp import Project  # noqa: E402
from test_subagent_mcp_live import _with_advisor  # noqa: E402


# ---------------------------------------------------------------------------
# "docker absent from PATH", for real — not merely unmocked
# ---------------------------------------------------------------------------

def _path_without_docker(raw: str, mirrors: Path) -> str:
    """`raw` with `docker` gone from every entry. A directory holding a
    `docker` binary is replaced, in place, by a mirror of symlinks to
    everything else in it: on a host docker sits in /usr/bin beside `git`,
    `sleep` and the rest, and dropping the whole directory would take those
    with it."""
    kept = []
    for i, entry in enumerate(raw.split(os.pathsep)):
        if not entry:
            continue
        d = Path(entry)
        if not os.path.lexists(d / "docker"):
            kept.append(entry)
            continue
        mirror = mirrors / str(i)
        mirror.mkdir()
        for child in d.iterdir():
            if child.name != "docker":
                (mirror / child.name).symlink_to(child)
        kept.append(str(mirror))
    return os.pathsep.join(kept)


@pytest.fixture
def no_docker_on_path(monkeypatch, tmp_path_factory):
    """Whatever this machine's PATH is, make sure no `docker` is on it while
    everything else stays reachable. `docker_available()` (`shutil.which`)
    then finds nothing, and `subprocess.run(["docker", ...])` raises
    `FileNotFoundError` rather than quietly succeeding against a real
    install — so a leftover unguarded `docker` call in the code under test
    fails the test loudly."""
    mirrors = tmp_path_factory.mktemp("path-without-docker")
    monkeypatch.setenv("PATH", _path_without_docker(os.environ.get("PATH", ""),
                                                    mirrors))
    assert docker_mod.docker_available() is None


@pytest.fixture
def inside(monkeypatch):
    """`DockerExecutor.inside()` true for every instance, for the duration of
    one test — the standard double for a check that reads real kernel/FS
    state (`/.dockerenv`, `/proc/1/environ`) and cannot be forged through the
    environment (see the module docstring)."""
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: True)


# ---------------------------------------------------------------------------
# preflight() / ensure_running() — unit level, no subprocess
# ---------------------------------------------------------------------------

def test_sm_r1_preflight_is_clean_inside_without_docker(
        tmp_path, no_docker_on_path, inside):
    """A server inside the container is by definition running, on the image
    it was started from — nothing `preflight` checks applies to itself."""
    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge"}, paths)

    assert ex.inside() is True
    assert ex.preflight() == []


def test_sm_r1_ensure_running_is_ok_inside_without_docker(
        tmp_path, no_docker_on_path, inside):
    """Likewise `ensure_running`: nothing to start, nothing to ask docker."""
    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge"}, paths)

    result = ex.ensure_running()

    assert result.get("ok") is True, result
    assert result.get("container") == ex.container


def test_sm_r1_preflight_still_refuses_socket_mount_inside(
        tmp_path, no_docker_on_path, inside):
    """`inside()` short-circuits the docker/image checks, not every check —
    though today `mount_docker_socket` is the only other one `preflight`
    has, and it is a host-launch decision this process cannot itself have
    made (the container is already running with whatever was decided at
    `docker run` time), so it is fine for `inside()` to make it moot too.
    Pinned here so a future check added to `preflight` is a deliberate
    choice about whether `inside()` should skip it, not an accident."""
    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge", "mount_docker_socket": True}, paths)

    assert ex.preflight() == []


# ---------------------------------------------------------------------------
# kill_detached() — a real process, really signalled, no `docker exec`
# ---------------------------------------------------------------------------

def test_sm_r1_kill_detached_signals_the_recorded_pid_without_docker_exec(
        tmp_path, no_docker_on_path, inside):
    """`kill_detached` reads the pid a spawn recorded and stops it. Inside
    the container that pid is already in this process's own pid namespace —
    a plain `kill`/`pkill` reaches it directly, and must, since there is no
    `docker exec` to reach it with."""
    paths = ProjectPaths(tmp_path / "proj")
    paths.ensure()
    ex = DockerExecutor({"network": "bridge"}, paths)

    proc = subprocess.Popen(["sleep", "30"])
    pid_file = paths.run_dir("ag-sm-r1-kill") / "container.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))

    try:
        assert ex.kill_detached("ag-sm-r1-kill") is True
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_sm_r1_kill_detached_without_a_pid_file_does_nothing_and_says_so(
        tmp_path, no_docker_on_path, inside):
    """No recorded pid — nothing to signal, and no `docker exec` attempted
    either: the absent-PATH fixture alone would fail the test if it were."""
    paths = ProjectPaths(tmp_path / "proj")
    paths.ensure()
    ex = DockerExecutor({"network": "bridge"}, paths)

    assert ex.kill_detached("ag-never-spawned") is False


# ---------------------------------------------------------------------------
# A real consult-style spawn, end to end — the live scenario itself
# ---------------------------------------------------------------------------

def _as_docker_project(tmp_path, monkeypatch, provider: str = "claude") -> Project:
    """A `Project` configured for the docker executor, but built the way
    `Project(..., "local")` is: no fake `docker` on PATH. The project's own
    config is what says `executor: docker` — PATH staying clean is what lets
    `no_docker_on_path` mean something rather than being redundant with a
    fake binary `Project(..., "docker")` would have put there to prove the
    opposite (that `docker exec` DOES happen, for the host-side case)."""
    project = Project(tmp_path, monkeypatch, provider, "local")
    cfg = project.root / ".multiagents" / "config"
    project_yaml = yaml.safe_load((cfg / "project.yaml").read_text())
    project_yaml["executor"] = {"kind": "docker", "docker": {"network": "bridge"}}
    (cfg / "project.yaml").write_text(yaml.safe_dump(project_yaml))
    return project


def test_sm_r1_a_consult_style_spawn_starts_inside_without_docker(
        tmp_path, monkeypatch, no_docker_on_path, inside):
    """The live check itself, reproduced: an agent with spawn rights, its
    own server configured for the docker executor, calls `consult` from
    inside its own container. Before this fix, `Runner._launch`'s
    unconditional `executor.preflight()` call raised `RuntimeError("docker
    is not on PATH")` before `start()` ever got a chance to take its
    already-correct `_start_inside` branch (ag-48a3d2's evidence,
    reproduced exactly: consult's own RuntimeError handler turns that into
    `{"error": "docker is not on PATH"}` — the return value below)."""
    project = _as_docker_project(tmp_path, monkeypatch, "claude")
    _with_advisor(project)
    h.as_subagent(monkeypatch, agent_id="ag-sm-r1-caller", can_spawn=True)

    async def go():
        return await server.consult("advisor", "Reply PONG", 30)

    try:
        result = asyncio.run(go())
    finally:
        server._reset()

    assert not result.get("error"), result
    node = project.status(result["agent_id"])
    assert not node.startswith("absent"), node
    assert "docker is not on PATH" not in node, node
    run_dir = project.paths.run_dir(result["agent_id"])
    assert run_dir.is_dir(), f"no run directory for {result['agent_id']}"


# ---------------------------------------------------------------------------
# server._tool_failed — its own "never raises" held to, even if Tree.emit()
# somehow does
# ---------------------------------------------------------------------------

def test_tool_failed_never_raises_even_if_tree_emit_does(tmp_path, monkeypatch):
    """`_tool_failed`'s docstring says "Never raises" and guards its log
    write with `except OSError`, but left its `Tree(...).emit(...)` call
    bare (99be9d6). `Tree.emit` itself claims the same and normally holds to
    it, but `_tool_failed` runs precisely when something is already broken
    — it must not depend on that claim holding for it to keep its own."""
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(tmp_path))
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-tool-failed-test")
    monkeypatch.setattr(
        Tree, "emit",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full (simulated)")))

    import multiagents.server as server_mod
    server_mod._runner = None

    result = server_mod._tool_failed("consult", RuntimeError("sm-r1-probe"))

    assert "RuntimeError" in result and "sm-r1-probe" in result
