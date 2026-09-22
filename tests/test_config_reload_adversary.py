"""Adversary tests for P0-R5: MCP server config reload (`bug-2138e6`).

Attacks:
- Replacing a config file with a broken symlink or directory symlink silently
  reverts the server to shipped defaults instead of keeping the previous config.
- Concurrent tool calls racing a file edit emit multiple duplicate
  `config_reload` events to `events.jsonl` and duplicate announcements.
- Reverting a broken config file with restored mtime (e.g. `git checkout` or
  backup copy) fails to clear `_failed` and fails to announce the restoration.
- A file modified within the same timestamp tick with identical size is silently
  ignored and never reloads.
- An edit landing while `load_config` executes corrupts `_loaded` fingerprint
  via dict unpacking order, triggering a phantom reload on the subsequent idle call.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from test_phase0_config_reload import Project, project  # noqa: E402
from multiagents import server  # noqa: E402


def test_broken_symlink_config_does_not_fall_back_to_defaults(project):
    """P0-R5.4: Replacing a config file with a broken symlink must not silently

    fall back to shipped defaults. It must keep the previous config in force
    and report the load error.
    """
    initial_depth = server.list_agents()["max_depth"]
    assert initial_depth == 7

    target_yaml = project.config / "project.yaml"
    target_yaml.unlink()
    target_yaml.symlink_to(project.config / "nonexistent_target.yaml")

    result = server.list_agents()

    # The previous config limits must stay in force; must NEVER fall back to defaults (3)
    limits = server.runner().config.limits
    assert limits.get("max_depth") == 7, (
        f"P0-R5.4 violation: broken symlink caused fallback to defaults: {limits}"
    )
    # The load error must be reported
    assert result.get("config_reload", {}).get("load_error"), (
        f"P0-R5.4 violation: broken symlink was treated as successful reload: {result}"
    )


def test_directory_symlink_config_does_not_fall_back_to_defaults(project):
    """P0-R5.4: Replacing a config file with a symlink to a directory must not

    silently fall back to defaults.
    """
    initial_depth = server.list_agents()["max_depth"]
    assert initial_depth == 7

    target_yaml = project.config / "project.yaml"
    target_yaml.unlink()
    target_yaml.symlink_to(project.config / "agents")

    result = server.list_agents()

    limits = server.runner().config.limits
    assert limits.get("max_depth") == 7, (
        f"P0-R5.4 violation: directory symlink caused fallback to defaults: {limits}"
    )
    assert result.get("config_reload", {}).get("load_error"), (
        f"P0-R5.4 violation: directory symlink treated as successful reload: {result}"
    )


def test_concurrent_tool_calls_emit_exactly_one_reload_event(project):
    """P0-R5.8: A single reload must append exactly ONE event to events.jsonl,

    even when multiple concurrent MCP tool calls race to reload.
    """
    server.list_agents()
    baseline = len(project.event_lines())

    # Edit project.yaml to trigger reload
    project.project["limits"]["max_depth"] = 9
    project.write_project()

    # 5 concurrent tool calls racing through server.list_agents()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(server.list_agents) for _ in range(5)]
        results = [f.result() for f in futures]

    events = project.event_lines()[baseline:]
    assert len(events) == 1, (
        f"P0-R5.8 violation: concurrent tool calls emitted {len(events)} events "
        f"for a single config edit: {events}"
    )

    notices = [r.get("config_reload") for r in results if r.get("config_reload")]
    # P0-R5.2: the announcement belongs to the triggering call only
    assert len(notices) == 1, (
        f"P0-R5.2 violation: {len(notices)} concurrent calls each received reload announcement"
    )


def test_reverting_broken_config_with_restored_mtime_clears_error_and_announces(project):
    """P0-R5.4 / P0-R5.2: Reverting a broken file by restoring original bytes and mtime

    (e.g. `git checkout` or backup copy) must clear the load error and be announced.
    """
    server.list_agents()
    target_yaml = project.config / "project.yaml"
    orig_bytes = target_yaml.read_bytes()
    orig_mtime = target_yaml.stat().st_mtime_ns

    # Break file
    project.write("project.yaml", "limits: [unclosed\n")
    err_res = server.list_agents()
    assert err_res.get("config_reload", {}).get("load_error")

    # Restore exact bytes and mtime as git checkout / cp -p would
    target_yaml.write_bytes(orig_bytes)
    os.utime(target_yaml, ns=(orig_mtime, orig_mtime))

    res = server.list_agents()
    # Must not retain stale _load_error or fail to announce fix
    assert not res.get("config_reload", {}).get("load_error"), (
        "Reverting to original file left stale load_error in effect"
    )
    assert server._failed is None, (
        f"server._failed was not cleared after reverting broken file: {server._failed}"
    )


def test_same_tick_and_size_change_is_detected(project):
    """P0-R5.1: Changing a limit in-place without altering byte length or mtime

    (e.g. on coarse-timestamp filesystems, rapid automated scripts, or container mounts)
    must not run indefinitely on stale config.
    """
    assert server.list_agents()["max_depth"] == 7
    target_yaml = project.config / "project.yaml"
    st = target_yaml.stat()

    # Change 7 to 9: exact same byte length
    content = target_yaml.read_text().replace("max_depth: 7", "max_depth: 9")
    assert len(content) == st.st_size
    target_yaml.write_text(content)
    os.utime(target_yaml, ns=(st.st_mtime_ns, st.st_mtime_ns))

    result = server.list_agents()
    assert result["max_depth"] == 9, (
        f"P0-R5.1 violation: file was edited to max_depth: 9 but server kept stale value {result['max_depth']}"
    )


def test_midload_edit_causes_phantom_reload_on_subsequent_idle_call(project, monkeypatch):
    """P0-R5.5 / P0-R5.8: An edit landing while load_config is running must not

    cause a phantom reload and duplicate event on the next idle tool call.
    """
    server.list_agents()
    target_yaml = project.config / "project.yaml"

    orig_load = server.load_config
    def midload_edit(paths):
        p = paths.config / "project.yaml"
        p.write_text(p.read_text().replace("max_depth: 88", "max_depth: 99"))
        os.utime(p, (time.time() + 100, time.time() + 100))
        return orig_load(paths)

    # Initial edit to 88
    target_yaml.write_text(target_yaml.read_text().replace("max_depth: 7", "max_depth: 88"))
    os.utime(target_yaml, (time.time() + 50, time.time() + 50))

    monkeypatch.setattr(server, "load_config", midload_edit)
    r1 = server.list_agents()
    assert r1["max_depth"] == 99

    # Second call with NO edits made on disk: must NOT reload or announce again
    monkeypatch.setattr(server, "load_config", orig_load)
    baseline_events = len(project.event_lines())
    r2 = server.list_agents()
    assert r2.get("config_reload") is None, (
        f"Phantom reload announced on idle call: {r2.get('config_reload')}"
    )
    assert len(project.event_lines()) == baseline_events, "Phantom reload event emitted to log"
