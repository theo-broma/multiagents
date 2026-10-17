"""Shared helpers for the user-decisions slice 2 tests (UD-C7..UD-C10).

Black-box: the MCP tools are called through `server.mcp.call_tool` (the same
door a client uses, so an argument the signature does not declare is seen
exactly as a client would send it), the module through `multiagents.decisions`,
and the world is a project laid out as `ProjectPaths` lays it out, so the
tree is `<root>/.multiagents/tree.json` and a ref path is relative to `<root>`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from support.ud_support import D, IMPL, OPTS  # noqa: F401  (re-exported)

ROOT_ORCH = {"role": "orchestrator", "agent": "", "channel": "mcp_root"}
ROOT_INIT = {"role": "initializer", "agent": "", "channel": "mcp_root"}
RUN = {"role": "agent", "agent": "ag-7", "channel": "mcp_run"}
CLI = {"role": "user", "agent": "", "channel": "cli"}
MONITOR = {"role": "user", "agent": "", "channel": "monitor"}

ENV_KEYS = ("MULTIAGENTS_AGENT_ID", "MULTIAGENTS_ROLE", "MULTIAGENTS_RPC_TOKEN",
            "MULTIAGENTS_RPC_SOCKET", "MULTIAGENTS_NODE_PERMISSIONS")

IDENTITY_ARGS = {"by": {"role": "user", "agent": "", "channel": "cli"},
                 "answered_by": "user", "asked_by": {"role": "orchestrator", "agent": ""},
                 "role": "user", "channel": "cli"}


class World:
    """A project on disk with a runner whose tree lives where the server's does."""

    def __init__(self, root: Path, runner):
        self.root = root
        self.runner = runner
        self.tree = runner.tree

    def write(self, rel: str, data: bytes | str = "draft v1\n") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    @property
    def snapshots(self) -> Path:
        return self.root / ".multiagents" / "decisions" / "snapshots"

    def git(self, *args: str) -> str:
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    def init_repo(self) -> str:
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "hello\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        return self.git("rev-parse", "HEAD")


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def clean_env(monkeypatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def as_role(monkeypatch, role: str | None) -> None:
    """The root MCP server of `role`: no run capability."""
    clean_env(monkeypatch)
    if role is not None:
        monkeypatch.setenv("MULTIAGENTS_ROLE", role)


def as_run(monkeypatch, agent: str = "ag-7", token: bool = False, role: str | None = None) -> None:
    """An MCP server that carries a run capability."""
    clean_env(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", agent)
    if token:
        monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", "tok-" + "x" * 8)
    if role is not None:
        monkeypatch.setenv("MULTIAGENTS_ROLE", role)


def make_world(tmp_path: Path, monkeypatch) -> World:
    import multiagents.server as srv
    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner
    root = tmp_path / "proj"
    root.mkdir()
    paths = ProjectPaths(root)
    paths.ensure()
    run = Runner(paths, Config(project={}, providers={}, agents={}, models={},
                               instruction_dirs=[]))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                      # paths are the project's, not the cwd's
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(paths.root))
    monkeypatch.setattr(srv, "runner", lambda: run)
    monkeypatch.setattr(srv, "_project_paths", lambda: paths)
    as_role(monkeypatch, "orchestrator")
    return World(paths.root, run)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def call(name: str, **args) -> dict:
    """Call an MCP tool as a client does; a tool error comes back as
    {"error": "tool_error", "text": ...} so each test fails on its own assertion."""
    import multiagents.server as srv
    try:
        res = asyncio.run(srv.mcp.call_tool(name, args))
    except Exception as exc:
        return {"error": "tool_error", "text": f"{type(exc).__name__}: {exc}"}
    text = res.content[0].text if res.content else ""
    try:
        out = json.loads(text)
    except ValueError:
        return {"error": "tool_error", "text": text}
    return out if isinstance(out, dict) else {"result": out}


def ask_args(**over) -> dict:
    base = dict(topic="Run D merge", title="Merge run D?",
                context="Run D finished green. Merge it into main?",
                implications=dict(IMPL), mode="single",
                options=[dict(o) for o in OPTS])
    base.update(over)
    return base


def ask(**over) -> dict:
    return call("ask_user", **ask_args(**over))


def succeeded(out: dict) -> dict:
    assert "error" not in out, out
    return out


def refused(out: dict, code: str, **fields) -> dict:
    assert out.get("error") == code, out
    for key, value in fields.items():
        assert out.get(key) == value, (key, out)
    return out


def find(out, key):
    """`key` anywhere in a nested tool result."""
    if isinstance(out, dict):
        if key in out:
            return out[key]
        for v in out.values():
            got = find(v, key)
            if got is not None:
                return got
    return None


def snapshot_of(tree) -> dict:
    return {r["id"]: r for r in D.list(tree, limit=200)["decisions"]}


def n_events(tree) -> int:
    from support.ud_support import events
    return len(events(tree))


def make(world: World, *, by=ROOT_ORCH, audience="user", mode="single", **over) -> dict:
    """A decision asked by `by`, created through the module."""
    from support.ud_support import kwargs
    return D.create(world.tree, **kwargs(mode, by=by, audience=audience, **over))
