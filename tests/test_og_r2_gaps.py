"""OG-R2 gaps left after the OG merge (context/specs/opencode-go-rename.md).

Surfaces that must read the pre-rename name `opencode` as the route `opencode-go`,
loudly, and that did not when this file was written:

  1. `quota_handover.reserved_instance` in the global layer's project.yaml;
  2. an agent-level `quota_handover.reserved_instance` in agents.yaml;
  3. MCP `steer_agent(provider="opencode")`;
  4. "each load prints one deprecation warning": `multiagents doctor` on a config
     naming `opencode` once prints the warning once per offending line.

Placeholders only. Shared helpers are the OG ones in tests/support/og_support.py.
Silences (see the run result): the warning channel is every channel, and a file
loaded by several subsystems in one process is taken to be ONE load.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import og_support as og  # noqa: E402
from og_support import NEW, OLD, GO_MODEL  # noqa: E402

CLI_TIMEOUT = 60


def _agents_text(extra: str = "", provider: str = NEW) -> str:
    return ("agents:\n  helper:\n    description: a placeholder helper\n"
            f"    provider: {provider}\n    model: {GO_MODEL}\n{extra}")


# 1. quota_handover.reserved_instance ------------------------------------------

def test_og_r2_reserved_instance_in_the_global_layer_is_read_as_opencode_go_with_one_warning(
        tmp_path, capsys, caplog):
    from multiagents.paths import global_config_dir
    gdir = global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    gfile = gdir / "project.yaml"
    gfile.write_text(f"quota_handover:\n  reserved_instance: {OLD}\n")
    cfg, text = og.load(og.Project(tmp_path), capsys, caplog)
    assert cfg.project["quota_handover"]["reserved_instance"] == NEW
    [warning] = og.deprecations(text, about=gfile)
    assert og.named_line(warning, gfile) == og.line_of(gfile, "reserved_instance")
    assert NEW in warning


def test_og_r2_reserved_instance_warning_says_to_write_opencode_go(tmp_path, capsys, caplog):
    p = og.Project(tmp_path)
    project = p.write("project.yaml", f"quota_handover:\n  reserved_instance: {OLD}\n")
    _, text = og.load(p, capsys, caplog)
    [warning] = og.deprecations(text, about=project)
    assert f"{OLD}" in warning and NEW in warning
    assert og.named_line(warning, project) == og.line_of(project, "reserved_instance")


# 2. agent-level reserved_instance ----------------------------------------------

def test_og_r2_agent_level_reserved_instance_opencode_is_read_as_opencode_go_with_one_warning(
        tmp_path, capsys, caplog):
    p = og.Project(tmp_path)
    agents = p.write(
        "agents.yaml",
        _agents_text(f"    quota_handover:\n      reserved_instance: {OLD}\n"))
    cfg, text = og.load(p, capsys, caplog)
    assert cfg.agents["helper"].quota_handover["reserved_instance"] == NEW
    [warning] = og.deprecations(text, about=agents)
    assert og.named_line(warning, agents) == og.line_of(agents, "reserved_instance")


def test_og_r2_agent_level_reserved_instance_opencode_go_is_silent(tmp_path, capsys, caplog):
    p = og.Project(tmp_path)
    agents = p.write(
        "agents.yaml",
        _agents_text(f"    quota_handover:\n      reserved_instance: {NEW}\n"))
    cfg, text = og.load(p, capsys, caplog)
    assert cfg.agents["helper"].quota_handover["reserved_instance"] == NEW
    assert og.deprecations(text, about=agents) == []


# 3. MCP steer_agent(provider="opencode") ---------------------------------------

def test_og_r2_mcp_steer_agent_provider_opencode_acts_as_opencode_go(tmp_path, monkeypatch):
    """A run already on the route is steered plainly: `opencode` names it, it is
    not a switch to the CLI base (which is no route and has no model)."""
    import test_og_round7 as r7
    from multiagents import server
    w = r7.Renamed(tmp_path, monkeypatch)
    node = w.add_node()
    w.runner.tree.update(node.id, provider=r7.NEW)
    monkeypatch.setattr(server, "runner", lambda: w.runner)
    monkeypatch.setattr(server, "_node_id_refusal", lambda run, agent_id: None)
    reply = asyncio.run(server.steer_agent(r7.NODE, "carry on", provider=r7.OLD))
    if isinstance(reply, str):
        import json
        reply = json.loads(reply)
    result = reply.get("result", reply)
    error = str(result.get("error") or "")
    assert "allowed provider list" not in error and "no configured model" not in error, result
    assert result.get("steered") is True, result
    assert not r7._calls(w.old_probe)
    [argv] = r7._calls(w.new_probe)
    assert argv[argv.index("--provider") + 1] == r7.NEW


def test_og_r2_mcp_steer_agent_provider_opencode_go_is_unchanged(tmp_path, monkeypatch):
    import test_og_round7 as r7
    from multiagents import server
    w = r7.Renamed(tmp_path, monkeypatch)
    node = w.add_node()
    w.runner.tree.update(node.id, provider=r7.NEW)
    monkeypatch.setattr(server, "runner", lambda: w.runner)
    monkeypatch.setattr(server, "_node_id_refusal", lambda run, agent_id: None)
    reply = asyncio.run(server.steer_agent(r7.NODE, "carry on", provider=r7.NEW))
    result = reply.get("result", reply) if isinstance(reply, dict) else reply
    assert result.get("steered") is True, result


# 4. one warning per offending line, per load ------------------------------------

def _doctor(project: og.Project) -> str:
    out = subprocess.run(
        [sys.executable, "-m", "multiagents.cli", "--path", str(project.root), "doctor"],
        capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=project.root,
        env={**os.environ, "PATH": "/usr/bin:/bin"})
    return out.stdout + "\n" + out.stderr


def test_og_r2_doctor_prints_the_deprecation_once_per_offending_line(tmp_path):
    p = og.Project(tmp_path)
    agents = p.write("agents.yaml", _agents_text(provider=OLD))
    text = _doctor(p)
    found = og.deprecations(text, about=agents)
    assert len(found) == 1, f"expected one warning, got {len(found)}:\n" + "\n".join(found)
    assert og.named_line(found[0], agents) == og.line_of(agents, f"provider: {OLD}")


def test_og_r2_doctor_prints_two_offending_lines_as_two_warnings(tmp_path):
    p = og.Project(tmp_path)
    agents = p.write(
        "agents.yaml",
        _agents_text(provider=OLD)
        + "  second:\n    description: y\n"
        f"    provider: {OLD}\n    model: {GO_MODEL}\n")
    found = og.deprecations(_doctor(p), about=agents)
    assert sorted(og.named_line(w, agents) for w in found) == [
        og.line_of(agents, f"provider: {OLD}", 1), og.line_of(agents, f"provider: {OLD}", 2)]


def test_og_r2_doctor_on_a_migrated_config_prints_no_deprecation(tmp_path):
    p = og.Project(tmp_path)
    agents = p.write("agents.yaml", _agents_text())
    assert og.deprecations(_doctor(p), about=agents) == []
