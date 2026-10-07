"""SM-R1, a third layer: an inside spawn drops the network/auth-proxy env.

Contract: `context/specs/subagent-mcp.md`, SM-R1.

e347e9e and a46ae76 got `consult` as far as actually starting a child inside
the container. The live check then failed one layer further in: ag-37349e's
`dev-advisor` (agy, launched via `DockerExecutor._start_inside`) failed
immediately with `Eligibility check failed: ... dial tcp: lookup
daily-cloudcode-pa.googleapis.com on 127.0.0.11:53: server misbehaving` — it
tried to reach the internet directly instead of through the egress proxy.

The cause: `run_args` puts `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (and
`ANTHROPIC_BASE_URL` when the auth proxy is on) on the container itself at
`docker run` time. A host-launched spawn reaches them for free, because
`docker exec` inherits the container's own environment. `_start_inside`
instead hands its child to `LocalExecutor.start`, which passes `env=` to
`asyncio.create_subprocess_exec` — that REPLACES the subprocess environment
rather than inheriting this process's, and `build_env` (SM-R4, deny-by-default)
builds `env` from a clean slate that never included these names. So a child
spawned from inside the container got none of them.

This file is a NEW file, not an edit to `tests/test_subagent_mcp_live_inside.py`
or `tests/test_subagent_mcp_live.py`: both are existing test files and off
limits to modify under this role's contract. It follows their idiom and
reuses their fixtures directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.executor.docker import DockerExecutor, NETWORK_ENV_KEYS  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402

from test_subagent_mcp_live_inside import inside, no_docker_on_path  # noqa: E402,F401


DUMP_ENV = (
    "import json, os, sys\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    json.dump(dict(os.environ), f)\n"
)


async def _run_and_capture(ex: DockerExecutor, cwd: Path, env: dict) -> dict:
    """Start `argv` through `ex.start`, wait for it, and return the child's
    own `os.environ` as it actually received it."""
    out = cwd / "env.json"
    argv = [sys.executable, "-c", DUMP_ENV, str(out)]
    handle = await ex.start(argv, cwd, env)
    await handle.wait()
    return json.loads(out.read_text())


def test_inside_spawn_passes_the_containers_network_env(
        tmp_path, monkeypatch, no_docker_on_path, inside):
    """The exact bug: `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`/`ANTHROPIC_BASE_URL`
    are set in the running process's own environment (as `run_args` would have
    set them on the container at creation) and must reach a child started
    from inside — the same thing a host `docker exec` gets for free by
    inheriting the container's environment."""
    for key in NETWORK_ENV_KEYS:
        monkeypatch.setenv(key, f"http://proxy.internal:8888/{key}")

    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge"}, paths)
    cwd = tmp_path / "work"
    cwd.mkdir()

    child_env = asyncio.run(_run_and_capture(ex, cwd, env={"PATH": os.environ["PATH"]}))

    for key in NETWORK_ENV_KEYS:
        assert child_env.get(key) == f"http://proxy.internal:8888/{key}", (key, child_env)


def test_inside_spawn_still_blocks_a_credential_in_the_process_env(
        tmp_path, monkeypatch, no_docker_on_path, inside):
    """The fix must not become "inherit this process's whole environment" —
    that would hand a spawned child whatever credential the container
    process happens to hold, exactly what `build_env`'s deny-by-default
    (SM-R4) exists to prevent. Only the network/auth-proxy names travel."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    for key in NETWORK_ENV_KEYS:
        monkeypatch.setenv(key, "http://proxy.internal:8888")

    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge"}, paths)
    cwd = tmp_path / "work"
    cwd.mkdir()

    child_env = asyncio.run(_run_and_capture(ex, cwd, env={"PATH": os.environ["PATH"]}))

    assert "ANTHROPIC_API_KEY" not in child_env, child_env
    for key in NETWORK_ENV_KEYS:
        assert child_env.get(key) == "http://proxy.internal:8888", (key, child_env)


def test_inside_spawn_explicit_env_wins_over_the_containers_own(
        tmp_path, monkeypatch, no_docker_on_path, inside):
    """A value already present in the launch's own `env` — identity or
    provider config, built by `build_env` before `executor.start` is ever
    called — is configuration, not ambient inheritance, and must not be
    overwritten by whatever the container process happens to have."""
    monkeypatch.setenv("HTTPS_PROXY", "http://from-process-env:8888")

    paths = ProjectPaths(tmp_path / "proj")
    ex = DockerExecutor({"network": "bridge"}, paths)
    cwd = tmp_path / "work"
    cwd.mkdir()

    child_env = asyncio.run(_run_and_capture(
        ex, cwd, env={"PATH": os.environ["PATH"], "HTTPS_PROXY": "http://explicit:9999"}))

    assert child_env.get("HTTPS_PROXY") == "http://explicit:9999", child_env
