"""Adversarial tests attacking privilege boundaries and isolation in tooling defect 6.

Contract: context/specs/subagent-mcp.md (SM-R1..SM-R5).
Commit under attack: 9818b99.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h
from multiagents import budget as budget_mod
from multiagents import config as config_mod
from multiagents import paths
from multiagents import server
from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from test_subagent_mcp import Project, server_refs, _unavailable_events, USER_CONFIG


class TestSubagentMcpAdversary:

    def test_can_spawn_false_with_leftover_home_file_has_no_server(self, tmp_path, monkeypatch):
        """SM-R2: An agent with can_spawn: false must never be handed the multiagents server.

        If a leftover ~/.gemini/config/mcp_config.json exists from an earlier run in the
        same agent home (e.g. a previous spawner turn or reused home), the can_spawn: false
        launch must clean it up or ensure the CLI does not receive/load it.
        """
        project = Project(tmp_path, monkeypatch, "agy", "local")

        # Create a home directory that contains a leftover mcp_config.json from an earlier spawner run
        leaf_home = project.paths.home("reused-home")
        leaf_home.mkdir(parents=True, exist_ok=True)
        config_dir = leaf_home / ".gemini" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "mcp_config.json").write_text(json.dumps({
            "mcpServers": {
                "multiagents": {
                    "command": sys.executable,
                    "args": ["-m", "multiagents.server"],
                }
            }
        }))

        # Route spawns to this home directory
        monkeypatch.setattr(ProjectPaths, "home", lambda self, nid: leaf_home)

        _, leaf_call = project.spawn("leaf", task="leaf task")
        refs = server_refs(leaf_call)
        assert refs == [], (
            f"SM-R2 violation: can_spawn: false agent was handed the multiagents server "
            f"due to leftover config in home directory: {refs}"
        )

    def test_can_spawn_false_with_inherited_opencode_config_has_no_server(self, tmp_path, monkeypatch):
        """SM-R2: An agent without spawn rights must not receive the server via OPENCODE_CONFIG.

        If OPENCODE_CONFIG is passed through or present in the environment, a can_spawn: false
        agent must not inherit or load a multiagents MCP server configuration.
        """
        project = Project(tmp_path, monkeypatch, "opencode", "local")
        proj_cfg = project.root / ".multiagents" / "config" / "project.yaml"
        pdata = yaml.safe_load(proj_cfg.read_text())
        pdata["security"] = {"env_passthrough": ["OPENCODE_CONFIG"]}
        proj_cfg.write_text(yaml.safe_dump(pdata))

        fake_cfg = tmp_path / "inherited_opencode.json"
        fake_cfg.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "multiagents": {
                    "type": "local",
                    "enabled": True,
                    "command": [sys.executable, "-m", "multiagents.server"],
                }
            }
        }))
        monkeypatch.setenv("OPENCODE_CONFIG", str(fake_cfg))
        server._reset()

        _, leaf_call = project.spawn("leaf", task="leaf opencode task")
        refs = server_refs(leaf_call)
        assert refs == [], (
            f"SM-R2 violation: can_spawn: false agent loaded multiagents server from "
            f"inherited OPENCODE_CONFIG: {refs}"
        )

    def test_server_identity_cannot_be_overridden_by_provider_env(self, tmp_path, monkeypatch):
        """SM-R3: The server's MULTIAGENTS_AGENT_ID must strictly be the child's own ID.

        If provider.env attempts to set MULTIAGENTS_AGENT_ID, it must not override
        the server's identity attribution.
        """
        project = Project(tmp_path, monkeypatch, "claude", "local")
        cfg_file = project.root / ".multiagents" / "config" / "providers.yaml"
        data = yaml.safe_load(cfg_file.read_text())
        data["providers"]["claude"]["env"] = {"MULTIAGENTS_AGENT_ID": "ag-spoofed-attacker"}
        cfg_file.write_text(yaml.safe_dump(data))

        agent_id, call = project.spawn("spawner", task="spoof task")
        refs = server_refs(call)
        assert refs, f"no server in launch: {call['argv']}"

        for where, entry, _ in refs:
            env = entry.get("env") or entry.get("environment") or {}
            assert env.get("MULTIAGENTS_AGENT_ID") == agent_id, (
                f"SM-R3 violation: server MULTIAGENTS_AGENT_ID was spoofed to {env.get('MULTIAGENTS_AGENT_ID')!r} "
                f"instead of child agent_id {agent_id!r} ({where})"
            )

    def test_symlink_in_run_dir_does_not_overwrite_user_config(self, tmp_path, monkeypatch):
        """SM-R4: The user's configuration is never touched.

        If run_dir/mcp.json or run_dir/opencode.json is a symlink pointing to a user config,
        Runner._hand_server must not follow the symlink and overwrite the user file.
        """
        project = Project(tmp_path, monkeypatch, "claude", "local")
        user_claude_json = project.user_home / ".claude.json"
        original_content = user_claude_json.read_text()

        run_dir = project.paths.runs / "ag-symlink-test"
        run_dir.mkdir(parents=True, exist_ok=True)
        mcp_link = run_dir / "mcp.json"
        mcp_link.symlink_to(user_claude_json)

        cfg = config_mod.load(project.paths)
        runner = Runner(project.paths, cfg)
        provider = runner.providers["claude"]

        runner._hand_server("ag-symlink-test", provider, {"MULTIAGENTS_AGENT_ID": "ag-symlink-test"}, None, run_dir)

        after_content = user_claude_json.read_text()
        assert after_content == original_content, (
            "SM-R4 violation: Runner._hand_server followed a symlink in run_dir and overwrote ~/.claude.json!"
        )

    def test_multiagents_container_env_does_not_bypass_docker_exec_on_host(self, tmp_path, monkeypatch):
        """Privilege boundary: MULTIAGENTS_CONTAINER env var must not bypass docker exec when on host.

        DockerExecutor.inside() must only return True when the process is genuinely inside
        the container, not merely when an env var is set. When run on the host with
        MULTIAGENTS_CONTAINER set, it must not execute directly on host without docker exec.
        """
        project = Project(tmp_path, monkeypatch, "claude", "docker")
        cfg = config_mod.load(project.paths)
        runner = Runner(project.paths, cfg)
        dock_exec = runner.executor()

        # Simulate host process having MULTIAGENTS_CONTAINER set
        monkeypatch.setenv("MULTIAGENTS_CONTAINER", dock_exec.container)

        # On the host, inside() should be False!
        assert not dock_exec.inside(), (
            "DockerExecutor.inside() returned True on the host purely because "
            "MULTIAGENTS_CONTAINER was set in os.environ!"
        )

    def test_missing_interpreter_records_mcp_unavailable_on_all_providers(self, tmp_path, monkeypatch):
        """SM-R5: A missing/unresolvable server interpreter must record mcp_unavailable on all providers."""
        project = Project(tmp_path, monkeypatch, "opencode", "local")
        monkeypatch.setattr(paths, "server_command", lambda: ["/nonexistent/python3", "-m", "multiagents.server"])

        agent_id, _ = project.spawn("spawner", task="unresolvable interpreter task")
        events = project.events(agent_id)
        unavail = _unavailable_events(events)
        assert unavail, (
            f"SM-R5 violation: server interpreter /nonexistent/python3 failed to start under opencode, "
            f"but no mcp_unavailable event was recorded! Events: {events}"
        )

    def test_start_inside_shell_injection_via_agent_id(self, tmp_path):
        """Security: DockerExecutor._start_inside must not execute shell commands via unquoted pid_file."""
        base = tmp_path.resolve()
        proj_paths = ProjectPaths(base / "proj")
        proj_paths.ensure()
        dock_exec = DockerExecutor({}, proj_paths, {})

        injected_marker = base / "shell_injected.txt"
        bad_id = f'test"; touch "{injected_marker}"; echo "'
        env = {"MULTIAGENTS_AGENT_ID": bad_id}

        async def run():
            handle = await dock_exec._start_inside(["true"], base, env)
            await handle._proc.wait()

        asyncio.run(run())
        assert not injected_marker.exists(), (
            "Vulnerability: DockerExecutor._start_inside allowed arbitrary shell command injection "
            "via unescaped MULTIAGENTS_AGENT_ID into f'echo $$ > \"{pid_file}\"; exec \"$@\"'!"
        )
