"""SM-R1 as it failed live, and what a subagent's server says when a tool fails.

Contract: `context/specs/subagent-mcp.md`, SM-R1.

The SM-R1 tests in `test_subagent_mcp.py` start the server a child was handed
and list its tools. Listing touches nothing; the first real call builds the
runner. Live (ag-c65ee1, docker executor) that first call was
`consult("dev-advisor", ...)`, and it came back as a bare
`Error executing tool consult`: building the runner seeds the config layers,
and inside the container neither is writable. The machine's config directory
is not mounted (its parent is root-owned), and the project's
`.multiagents/config` is mounted read-only over the writable root.

These tests give the child's server exactly that environment — the entry it
was handed, with its config directory unwritable and its project config
read-only — and make a real call over MCP stdio.
"""

from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import multiagents  # noqa: E402
from test_subagent_mcp import Project, server_refs  # noqa: E402

# The server the child was handed runs whatever `multiagents` its interpreter
# imports, which is not necessarily this checkout. Pin it to the code under test.
SOURCE = str(Path(multiagents.__file__).resolve().parents[1])

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the permission bits this simulates the container with")


def _with_advisor(project: Project) -> None:
    cfg = project.root / ".multiagents" / "config"
    agents = yaml.safe_load((cfg / "agents.yaml").read_text())
    agents["agents"]["advisor"] = {"provider": project.provider, "model": "m",
                                   "conversational": True, "can_spawn": False,
                                   "description": "x", "instructions": ""}
    (cfg / "agents.yaml").write_text(yaml.safe_dump(agents))


def _as_in_the_container(project: Project, tmp_path: Path, env: dict) -> dict:
    """The child's server environment, with config writable nowhere."""
    locked = tmp_path / "root-owned"
    locked.mkdir()
    locked.chmod(0o555)
    cfg = project.root / ".multiagents" / "config"
    for dirpath, _dirs, files in os.walk(cfg):
        for name in files:
            Path(dirpath, name).chmod(0o444)
    for dirpath, _dirs, _files in sorted(os.walk(cfg), reverse=True):
        Path(dirpath).chmod(0o555)
    return {**env, "MULTIAGENTS_CONFIG_DIR": str(locked / ".config" / "multiagents")}


@pytest.fixture
def unlock(tmp_path):
    yield
    for dirpath, dirs, _files in os.walk(tmp_path):
        for name in dirs:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(p.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)


def _entry(project: Project) -> tuple[list[str], dict, str, str]:
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, f"no multiagents server in the launch: {call['argv']}"
    _, entry, _ = refs[0]
    command = entry.get("command")
    argv = ([str(c) for c in command] if isinstance(command, list)
            else [str(command)] + [str(a) for a in entry.get("args") or []])
    env = {**call["env"], **{k: str(v) for k, v in (entry.get("env")
                                                     or entry.get("environment") or {}).items()}}
    env["PYTHONPATH"] = SOURCE
    return argv, env, entry.get("cwd") or call["cwd"], agent_id


def _call_tool(argv, env, cwd, tool: str, arguments: dict,
               timeout: float = 120.0) -> dict:
    """Initialize, then call one tool; the JSON-RPC result of the call."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=cwd, text=True)
    deadline = time.monotonic() + timeout

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def reply(want_id):
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want_id:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "sm-test", "version": "0"}}})
        assert reply(1) is not None, "the server did not answer initialize"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}})
        answer = reply(2)
        assert answer is not None, f"no reply to {tool}"
        return answer.get("result") or {"error": answer.get("error")}
    finally:
        proc.kill()
        try:
            stderr = proc.communicate(timeout=10)[1]
        except Exception:
            stderr = ""
        _call_tool.stderr = stderr


def _text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content") or [])


def test_sm_r1_a_child_consults_through_its_server_with_config_unwritable(
        tmp_path, monkeypatch, unlock):
    """The live check, minus the container: the child's own server answers
    `consult` when neither config layer can be written to."""
    project = Project(tmp_path, monkeypatch, "claude", "local")
    _with_advisor(project)
    argv, env, cwd, agent_id = _entry(project)
    env = _as_in_the_container(project, tmp_path, env)

    result = _call_tool(argv, env, cwd, "consult",
                        {"agent": "advisor", "message": "Reply PONG", "timeout": 60})

    assert not result.get("isError"), (
        f"consult from {agent_id}'s server failed: {_text(result)}\n"
        f"stderr: {_call_tool.stderr[-2000:]}")
    payload = result.get("structuredContent") or json.loads(_text(result))
    payload = payload.get("result", payload)
    assert not payload.get("error"), payload
    node = project.status(payload["agent_id"])
    assert node != "absent", payload
    assert (project.paths.run_dir(payload["agent_id"])).is_dir()


PROBE = """
import multiagents.server as s
class ProbeFailure(Exception):
    pass
def boom():
    raise ProbeFailure("sm-probe-4c1: the runner could not be built")
s.runner = boom
s.main()
"""


def test_sm_r1_a_tool_that_raises_in_a_childs_server_says_what_raised(
        tmp_path, monkeypatch):
    """A crash inside a child's tool reaches the agent as its type and message,
    lands in the event log the same way, and leaves its traceback under the
    child's run directory — not the SDK's bare `Error executing tool X`."""
    project = Project(tmp_path, monkeypatch, "claude", "local")
    argv, env, cwd, agent_id = _entry(project)

    result = _call_tool([argv[0], "-c", PROBE], env, cwd, "consult",
                        {"agent": "advisor", "message": "hello"})

    assert result.get("isError"), result
    text = _text(result)
    assert "ProbeFailure" in text and "sm-probe-4c1" in text, text

    events = [e for e in project.events(agent_id)
              if "ProbeFailure" in json.dumps(e) and "sm-probe-4c1" in json.dumps(e)]
    assert events, f"no event for {agent_id} names the exception: {project.events(agent_id)}"
    assert any(e.get("tool") == "consult" for e in events), events

    run_dir = project.paths.run_dir(agent_id)
    traces = [p for p in run_dir.rglob("*") if p.is_file()
              and "Traceback" in p.read_text(errors="replace")
              and "ProbeFailure" in p.read_text(errors="replace")]
    assert traces, f"no traceback under {run_dir}: {sorted(p.name for p in run_dir.iterdir())}"
