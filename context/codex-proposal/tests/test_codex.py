"""Offline contract tests against the project's REAL Provider implementation."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import yaml

from multiagents.providers import Provider
from multiagents.tree import token_count

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "providers" / "codex.py"
spec = importlib.util.spec_from_file_location("codex_adapter", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def provider():
    return Provider.from_dict("codex", yaml.safe_load((ROOT / "providers.yaml").read_text())["providers"]["codex"])


@pytest.fixture
def fake(tmp_path, monkeypatch):
    path = tmp_path / "codex"
    path.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if "mcp" in sys.argv:
    print(os.environ.get("MCP_LIST", "[]"))
    sys.exit(0)
if "login" in sys.argv:
    print("Not logged in" if os.environ.get("AUTH_FAIL") else "Logged in using secret-key-never-print")
    sys.exit(int(os.environ.get("AUTH_FAIL", "0")))
Path(os.environ["CAPTURE"]).write_text(json.dumps({"argv":sys.argv[1:], "prompt":sys.stdin.read(), "cwd":os.getcwd()}))
for event in json.loads(os.environ.get("EVENTS", "[]")):
    print(json.dumps(event), flush=True)
sys.exit(int(os.environ.get("EXIT_CODE", "0")))
''')
    path.chmod(0o755)
    monkeypatch.setenv("MULTIAGENTS_CODEX_BIN", str(path))
    monkeypatch.setenv("CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.delenv("MULTIAGENTS_CAN_SPAWN", raising=False)
    return path


def invoke(*args, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, **kwargs)


@pytest.mark.parametrize("permission,sandbox", [("readonly", "read-only"), ("sandbox", "workspace-write"), ("full", "danger-full-access")])
@pytest.mark.parametrize("sid", [None, "0199a213-81c0-7800-8aa1-bbab2a035a53"])
def test_real_builder_to_native_cli(provider, fake, tmp_path, monkeypatch, permission, sandbox, sid):
    monkeypatch.setenv("EVENTS", json.dumps([
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 10}},
    ]))
    prompt = "--literal {task}\n$(do-not-execute) `no-shell`"
    argv = provider.build_command(prompt=prompt, model="test-model", workdir=str(tmp_path),
                                  permission=permission, session_id=sid, options={"effort": "high"})
    result = invoke(*argv[1:])
    assert result.returncode == 0, result.stderr
    captured = json.loads((tmp_path / "capture.json").read_text())
    assert captured["prompt"] == prompt
    assert captured["cwd"] == str(tmp_path)
    native = captured["argv"]
    assert f'sandbox_mode="{sandbox}"' in native
    assert 'approval_policy="never"' in native
    assert 'model_reasoning_effort="high"' in native
    index = native.index("exec")
    if sid:
        assert native[index + 1:index + 3] == ["resume", sid]
    else:
        assert "resume" not in native
    events = [provider.parse_line(line) for line in result.stdout.splitlines()]
    assert [e.text for e in events if e.kind == "text"] == ["done"]
    assert events[0].session_id == "thread-1"
    assert events[-1].status == "success"
    assert token_count(events[-1].tokens) == 110
    assert events[-1].tokens["input_tokens"] == 30


def test_mcp_rendering_preserves_identity_and_escaping(provider, tmp_path, fake):
    launch = provider.mcp_launch({"mcp_command": "/path with spaces/python", "mcp_args": ["-m", "multiagents"],
        "mcp_env": {"MULTIAGENTS_AGENT_ID": "a", "STRANGE": 'a"b\\c\n'}, "mcp_config": str(tmp_path / "mcp.json")})
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(launch["config"]))
    flags = bridge.mcp_flags(path)
    parsed = tomllib.loads(flags[1])["mcp_servers"]["multiagents"]
    assert parsed["required"] is True
    assert parsed["env"]["STRANGE"] == 'a"b\\c\n'
    assert parsed["env"]["MULTIAGENTS_AGENT_ID"] == "a"
    assert tomllib.loads(bridge.mcp_flags()[1])["mcp_servers"] == {}


def test_inherited_mcp_servers_explicitly_disabled(fake, monkeypatch):
    monkeypatch.setenv("MCP_LIST", '[{"name":"foreign.server","transport":{"command":"false"}},{"name":"multiagents","transport":{"url":"https://example.test/mcp"}}]')
    flags = bridge.mcp_flags()
    servers = tomllib.loads(flags[1])["mcp_servers"]
    assert servers["foreign.server"]["enabled"] is False
    assert servers["multiagents"]["enabled"] is False


def test_tool_once_and_real_arguments(provider):
    norm = bridge.Normalizer()
    norm.event({"type": "turn.started"})
    item = {"id": "c", "type": "command_execution", "command": "ls src", "status": "in_progress"}
    start = provider.parse_line(json.dumps(norm.event({"type": "item.started", "item": item})))
    end = provider.parse_line(json.dumps(norm.event({"type": "item.completed", "item": item})))
    assert start.kind == "tool" and start.args == {"command": "ls src"}
    assert end.kind == "step"
    assert start.loop_signature() is not None
    mcp = norm.event({"type": "item.started", "item": {"id": "m", "type": "mcp_tool_call",
                      "server": "multiagents", "tool": "agent_tree", "arguments": {"depth": 2}}})
    assert mcp["name"] == "mcp__multiagents__agent_tree"
    assert mcp["args"] == {"depth": 2}
    assert norm.event({"type": "future.event"})["kind"] == "raw"


@pytest.mark.parametrize("events,exitcode", [([], 0), ([], 7), ([{"type":"turn.failed", "error":{"message":"quota reached"}}], 0),
    ([{"type":"turn.completed", "usage":{}}], 3)])
def test_failures_are_not_success(fake, tmp_path, monkeypatch, events, exitcode):
    monkeypatch.setenv("EVENTS", json.dumps(events))
    monkeypatch.setenv("EXIT_CODE", str(exitcode))
    result = invoke("run", "--prompt", "test", "--workdir", str(tmp_path))
    assert result.returncode != 0
    assert json.loads(result.stdout.splitlines()[-1])["status"] == "failed"


def test_actions_are_honest_and_do_not_leak_auth(fake, monkeypatch):
    assert invoke("check").stdout.strip() == "Codex authenticated"
    monkeypatch.setenv("AUTH_FAIL", "1")
    assert invoke("check").returncode == 10
    assert json.loads(invoke("budget").stdout)["known"] is False
    assert invoke("compact").returncode == 64
    monkeypatch.setenv("MULTIAGENTS_COMPACT_CHECK", "1")
    assert invoke("compact").returncode == 64
    monkeypatch.setenv("MULTIAGENTS_BUDGET", '{"note":"not available"}')
    assert "not available" in invoke("usage").stdout


def test_docker_auth_profile_and_host_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "host"))
    monkeypatch.setenv("MULTIAGENTS_EXECUTOR", "docker")
    monkeypatch.setenv("MULTIAGENTS_PRIVATE_BACKING", str(tmp_path / "container"))
    monkeypatch.delenv("MULTIAGENTS_PROFILE", raising=False)
    assert bridge.profile("check") == tmp_path / "container"
    assert bridge.profile("run") == tmp_path / "host"
    monkeypatch.setenv("MULTIAGENTS_PROFILE", "host")
    assert bridge.profile("check") == tmp_path / "host"


def test_spawn_without_mcp_fails_closed(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_CAN_SPAWN", "1")
    result = invoke("run", "--prompt", "test", "--workdir", str(tmp_path))
    assert result.returncode != 0
    assert not (tmp_path / "capture.json").exists()


def test_model_cache_and_empty_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert invoke("models").returncode == 64
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "available-model", "display_name": "Available Model"},
        {"slug": "hidden", "visibility": "hide"}]}))
    assert invoke("models").stdout == "available-model\tAvailable Model\n"


def test_session_scan_matches_project_only(tmp_path):
    directory = tmp_path / "sessions" / "2026"
    directory.mkdir(parents=True)
    for sid, cwd in [("mine", str(tmp_path.resolve())), ("other", "/other/project")]:
        (directory / f"{sid}.jsonl").write_text(json.dumps({"type":"session_meta", "payload":{"id":sid,"cwd":cwd}}) + "\n")
    assert set(bridge.sessions(tmp_path, tmp_path)) == {"mine"}


def launch_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(tmp_path))
    monkeypatch.setenv("MULTIAGENTS_LAUNCH_STATE", str(tmp_path / "launch"))
    monkeypatch.setenv("MULTIAGENTS_ROLE", "orchestrator")
    monkeypatch.setenv("MULTIAGENTS_RESUME", "1")
    prompt = tmp_path / "brief.md"
    prompt.write_text("Role brief")
    monkeypatch.setenv("MULTIAGENTS_PROMPT_FILE", str(prompt))
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers":{"multiagents":{"command":"python3", "args":["-m", "multiagents"]}}}))
    monkeypatch.setenv("MULTIAGENTS_MCP_CONFIG", str(config))
    return tmp_path / "launch" / "codex-orchestrator-session"


def test_prepare_and_unattended_launch(fake, tmp_path, monkeypatch):
    marker = launch_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_UNATTENDED", "1")
    monkeypatch.setenv("MULTIAGENTS_NUDGE", "Continue now")
    monkeypatch.setenv("EVENTS", json.dumps([{"type":"thread.started","thread_id":"my-session"},
        {"type":"turn.completed", "usage":{}}]))
    assert invoke("prepare").returncode == 0
    assert not marker.exists()
    result = invoke("launch")
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "my-session"
    captured = json.loads((tmp_path / "capture.json").read_text())
    assert "Role brief" in captured["prompt"] and "Continue now" in captured["prompt"]
    assert "resume" not in captured["argv"]  # advisory marker alone never resumes
    directory = tmp_path / "home" / "sessions"
    directory.mkdir(parents=True)
    (directory / "session.jsonl").write_text(json.dumps({"type":"session_meta",
        "payload":{"id":"my-session","cwd":str(tmp_path)}}) + "\n")
    assert invoke("launch").returncode == 0
    captured = json.loads((tmp_path / "capture.json").read_text())
    assert captured["argv"][captured["argv"].index("resume") + 1] == "my-session"


def test_interactive_role_resume(fake, tmp_path, monkeypatch):
    marker = launch_environment(tmp_path, monkeypatch)
    monkeypatch.delenv("MULTIAGENTS_UNATTENDED", raising=False)
    calls = []
    def interactive(argv, cwd):
        calls.append(argv)
        directory = tmp_path / "home" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "session.jsonl").write_text(json.dumps({"type":"session_meta",
            "payload":{"id":"role-session","cwd":cwd}}) + "\n")
        return 0
    monkeypatch.setattr(bridge.subprocess, "call", interactive)
    assert bridge.launch() == 0
    assert marker.read_text() == "role-session"
    assert bridge.launch() == 0
    assert "resume" not in calls[0]
    assert calls[1][calls[1].index("resume") + 1] == "role-session"
    assert "--last" not in calls[1]


def test_failed_turn_reaches_existing_quota_detector(fake, tmp_path, monkeypatch):
    from multiagents.supervisor import looks_like_quota_failure
    monkeypatch.setenv("EVENTS", json.dumps([{"type":"turn.failed", "error":{"message":"rate limit exceeded"}}]))
    result = invoke("run", "--prompt", "test", "--workdir", str(tmp_path))
    assert looks_like_quota_failure("failed", result.stderr)
