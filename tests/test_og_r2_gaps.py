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


# 5. MCP node tools and catalog tools: the remaining OG-R2 argument surfaces ------
#
# Review ag-fcc6f8: `pins.provider` of create_node / update_node / relaunch_node,
# `check_model_catalog(provider=...)` and `update_model_catalog(provider=...)` read
# the pre-rename name raw. Each is checked by what it does (the pin a node ends up
# with, the catalog read, the baseline written) and by the deprecation line on the
# MCP server's own stderr.

import json  # noqa: E402

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_harness import live, nc, tool  # noqa: E402,F401
from nc_fixture.m4_world import M4World  # noqa: E402

STATE_WAIT = 15      # one simple node's single activation, under a loaded machine


def _stderr_deprecations(capsys) -> list[str]:
    return og.deprecations(capsys.readouterr().err)


def _pins_of(sched_or_world, node_id: str) -> dict:
    return sched_or_world.get(node_id)["pins"]


# -- pins.provider on create_node / update_node ---------------------------------

def test_og_r2_mcp_create_node_pins_provider_opencode_is_stored_as_opencode_go(live, capsys):
    """Guard: the scheduler already canonicalises the pin."""
    from multiagents import server
    revision = live.plan_revision()
    made = tool(lambda: server.create_node(
        kind="simple", agent="worker", task="t", plan_revision=revision,
        pins={"provider": OLD}))
    assert "error" not in made, made
    assert live.get(made["id"])["pins"]["provider"] == NEW


def test_og_r2_mcp_create_node_pins_provider_opencode_warns_on_stderr(live, capsys):
    from multiagents import server
    revision = live.plan_revision()
    capsys.readouterr()
    made = tool(lambda: server.create_node(
        kind="simple", agent="worker", task="t", plan_revision=revision,
        pins={"provider": OLD}))
    assert "error" not in made, made
    [warning] = _stderr_deprecations(capsys)
    assert OLD in warning and NEW in warning


def test_og_r2_mcp_create_node_pins_provider_opencode_go_is_unchanged_and_silent(live, capsys):
    from multiagents import server
    revision = live.plan_revision()
    capsys.readouterr()
    made = tool(lambda: server.create_node(
        kind="simple", agent="worker", task="t", plan_revision=revision,
        pins={"provider": NEW, "effort": "high"}))
    assert "error" not in made, made
    assert live.get(made["id"])["pins"] == {"provider": NEW, "effort": "high"}
    assert _stderr_deprecations(capsys) == []


def test_og_r2_mcp_create_node_other_pins_alongside_opencode_are_kept(live, capsys):
    from multiagents import server
    revision = live.plan_revision()
    made = tool(lambda: server.create_node(
        kind="simple", agent="worker", task="t", plan_revision=revision,
        pins={"provider": OLD, "model": GO_MODEL, "effort": "low"}))
    assert "error" not in made, made
    assert live.get(made["id"])["pins"] == {
        "provider": NEW, "model": GO_MODEL, "effort": "low"}


def test_og_r2_mcp_update_node_pins_provider_opencode_is_stored_as_opencode_go(live, capsys):
    """Guard: the scheduler already canonicalises the pin."""
    from multiagents import server
    node = live.create()
    edited = tool(lambda: server.update_node(node["id"], node["revision"],
                                             pins={"provider": OLD}))
    assert "error" not in edited, edited
    assert live.get(node["id"])["pins"]["provider"] == NEW


def test_og_r2_mcp_update_node_pins_provider_opencode_warns_on_stderr(live, capsys):
    from multiagents import server
    node = live.create()
    capsys.readouterr()
    edited = tool(lambda: server.update_node(node["id"], node["revision"],
                                             pins={"provider": OLD}))
    assert "error" not in edited, edited
    [warning] = _stderr_deprecations(capsys)
    assert OLD in warning and NEW in warning


def test_og_r2_mcp_update_node_pins_provider_opencode_go_is_silent(live, capsys):
    from multiagents import server
    node = live.create()
    capsys.readouterr()
    edited = tool(lambda: server.update_node(node["id"], node["revision"],
                                             pins={"provider": NEW}))
    assert "error" not in edited, edited
    assert live.get(node["id"])["pins"]["provider"] == NEW
    assert _stderr_deprecations(capsys) == []


# -- pins.provider on relaunch_node ----------------------------------------------

@pytest.fixture
def settled(tmp_path, monkeypatch):
    """A simple node that has run to `done`, so it can be relaunched."""
    from multiagents import server
    from nc_fixture.m4_world import commit_entry
    world = M4World(tmp_path, monkeypatch)
    fx = world.provider("fxw")
    world.agent("wk", "fxw", writes=True)
    fx.queue(commit_entry("f1.txt", "v1\n", "round 1"), commit_entry("f2.txt", "v2\n", "round 2"))
    world.start_scheduler()
    node = world.simple("W", "wk", prose="do the work")
    world.wait_state(node, "done", timeout=STATE_WAIT)
    server._reset()
    yield world, node
    world.close()


def test_og_r2_mcp_relaunch_node_pins_provider_opencode_is_stored_as_opencode_go(settled):
    from multiagents import server
    world, node = settled
    revision = world.get(node)["revision"]
    reply = tool(lambda: server.relaunch_node(node, revision, pins={"provider": OLD}))
    assert "error" not in reply, reply
    assert world.get(node)["pins"]["provider"] == NEW


def test_og_r2_mcp_relaunch_node_pins_provider_opencode_warns_on_stderr(settled, capsys):
    from multiagents import server
    world, node = settled
    revision = world.get(node)["revision"]
    capsys.readouterr()
    reply = tool(lambda: server.relaunch_node(node, revision, pins={"provider": OLD}))
    assert "error" not in reply, reply
    [warning] = _stderr_deprecations(capsys)
    assert OLD in warning and NEW in warning


def test_og_r2_mcp_relaunch_node_pins_provider_opencode_go_is_silent(settled, capsys):
    from multiagents import server
    world, node = settled
    revision = world.get(node)["revision"]
    capsys.readouterr()
    reply = tool(lambda: server.relaunch_node(node, revision, pins={"provider": NEW}))
    assert "error" not in reply, reply
    assert world.get(node)["pins"]["provider"] == NEW
    assert _stderr_deprecations(capsys) == []


# -- check_model_catalog / update_model_catalog ----------------------------------

GO_V1 = {"models": {"glm-5.1": {"tool_call": True, "limit": {"context": 200000}},
                    "kimi-k2": {"tool_call": True, "limit": {"context": 128000}}}}
GO_V2 = {"models": {"glm-5.1": {"tool_call": True, "limit": {"context": 200000}}}}
ZEN = {"models": {f"zen-{i}": {"tool_call": True} for i in range(5)}}


class _Network:
    """The public catalog, as the one HTTP fetch of `models.opencode.ai` returns it:
    the Go slice under `opencode-go` and a different one, Zen, under `opencode`."""

    def __init__(self, monkeypatch):
        import io
        import urllib.request
        self.go = GO_V1
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda *a, **k: io.BytesIO(json.dumps(
                {"opencode-go": self.go, "opencode": ZEN}).encode()))


@pytest.fixture
def catalog_world(tmp_path, monkeypatch):
    """A project for the server's runner, the network stubbed at urlopen (the
    suite-wide fixture that refuses `fetch_remote` is undone: the fetch itself
    is the real one)."""

    from multiagents import catalog, server
    from multiagents.paths import global_config_dir
    og_project = og.Project(tmp_path)
    og_project.write("agents.yaml", _agents_text())
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(og_project.root))
    for var in ("MULTIAGENTS_AGENT_ID", "MULTIAGENTS_RPC_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    server._reset()
    real_fetch = _real_fetch_remote()
    monkeypatch.setattr(catalog, "fetch_remote", real_fetch)
    net = _Network(monkeypatch)
    yield net, global_config_dir() / "catalog"
    server._reset()


def _real_fetch_remote():
    """The unpatched `catalog.fetch_remote`: the autouse fixture replaced it."""
    import urllib.request
    from multiagents import catalog

    def fetch(provider, url=catalog.CATALOG_URL):
        with urllib.request.urlopen(url) as response:
            payload = json.load(response)
        if provider not in payload:
            raise KeyError(provider)
        return payload[provider]
    return fetch


def test_og_r2_mcp_update_model_catalog_opencode_records_the_opencode_go_baseline(
        catalog_world):
    from multiagents import server
    net, catalog_dir = catalog_world
    reply = tool(lambda: server.update_model_catalog(provider=OLD))
    assert reply.get("ok") is True, reply
    assert reply["provider"] == NEW and reply["models"] == 2
    recorded = json.loads((catalog_dir / f"{NEW}.json").read_text())
    assert set(recorded["data"]["models"]) == set(GO_V1["models"])
    assert not (catalog_dir / f"{OLD}.json").exists()


def test_og_r2_mcp_update_model_catalog_opencode_warns_on_stderr(catalog_world, capsys):
    from multiagents import server
    capsys.readouterr()
    tool(lambda: server.update_model_catalog(provider=OLD))
    [warning] = _stderr_deprecations(capsys)
    assert OLD in warning and NEW in warning


def test_og_r2_mcp_update_model_catalog_opencode_go_is_unchanged_and_silent(
        catalog_world, capsys):
    from multiagents import server
    net, catalog_dir = catalog_world
    capsys.readouterr()
    reply = tool(lambda: server.update_model_catalog(provider=NEW))
    assert reply.get("ok") is True and reply["provider"] == NEW, reply
    assert (catalog_dir / f"{NEW}.json").is_file()
    assert _stderr_deprecations(capsys) == []


def test_og_r2_mcp_check_model_catalog_opencode_checks_the_opencode_go_baseline(
        catalog_world):
    from multiagents import server
    net, catalog_dir = catalog_world
    assert tool(lambda: server.update_model_catalog(provider=NEW)).get("ok") is True
    net.go = GO_V2                                   # kimi-k2 disappears upstream
    reply = tool(lambda: server.check_model_catalog(provider=OLD))
    assert reply.get("ok") is True, reply
    assert reply["provider"] == NEW
    assert not reply.get("first_run"), reply        # compared against the Go baseline
    assert reply["changed"] == 1 and "kimi-k2" in json.dumps(reply["changes"])
    assert reply["models"] == 1


def test_og_r2_mcp_check_model_catalog_opencode_warns_on_stderr(catalog_world, capsys):
    from multiagents import server
    net, catalog_dir = catalog_world
    assert tool(lambda: server.update_model_catalog(provider=NEW)).get("ok") is True
    capsys.readouterr()
    tool(lambda: server.check_model_catalog(provider=OLD))
    [warning] = _stderr_deprecations(capsys)
    assert OLD in warning and NEW in warning


def test_og_r2_mcp_check_model_catalog_opencode_go_is_unchanged_and_silent(
        catalog_world, capsys):
    from multiagents import server
    net, catalog_dir = catalog_world
    assert tool(lambda: server.update_model_catalog(provider=NEW)).get("ok") is True
    net.go = GO_V2
    capsys.readouterr()
    reply = tool(lambda: server.check_model_catalog(provider=NEW))
    assert reply["provider"] == NEW and reply["changed"] == 1, reply
    assert _stderr_deprecations(capsys) == []


def test_og_r2_mcp_check_model_catalog_default_provider_is_opencode_go_and_silent(
        catalog_world, capsys):
    from multiagents import server
    net, catalog_dir = catalog_world
    capsys.readouterr()
    reply = tool(server.check_model_catalog)
    assert reply["provider"] == NEW, reply
    assert _stderr_deprecations(capsys) == []
