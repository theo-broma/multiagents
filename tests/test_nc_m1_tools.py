"""NC-R1, NC-R9, NC-R13, NC-R55 (milestone M1): the node tools the orchestrator's
MCP server exposes (`server.py`), with the gate off, on with no scheduler, on
with one, and turned off again with plans pending.

Assumptions (also in the run report): the tools are named after the ops of
NC-R13; keyword names are the contract's field names (`kind`, `agent`, `task`,
`plan_revision`, `id`, `revision`, `cursor`, `timeout`); a tool returns the
RPC `result` on success and `{"error": <code>, ...}` otherwise. Tools may be
sync or async. The host MCP server (no MULTIAGENTS_AGENT_ID) presents the root
capability; a subagent's server presents only MULTIAGENTS_RPC_TOKEN.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, files_under, live, nc, off, stored, tname, tool  # noqa: E402,F401

from multiagents import cli, manifest, server  # noqa: E402
from multiagents.paths import state_root  # noqa: E402

TOOLS = ["create_node", "update_node", "cancel_node", "get_node", "list_nodes",
         "register_template", "list_templates", "wait_for_nodes", "ack_nodes",
         "scheduler_status"]


def calls(plan_revision=0):
    """One call of every node tool with plausible arguments."""
    return {
        "create_node": lambda: server.create_node(kind="simple", agent="worker", task="t",
                                                  plan_revision=plan_revision),
        "update_node": lambda: server.update_node("nd-00000000", 1, task="t"),
        "cancel_node": lambda: server.cancel_node("nd-00000000", 1),
        "get_node": lambda: server.get_node("nd-00000000"),
        "list_nodes": lambda: server.list_nodes(),
        "register_template": lambda: server.register_template("template: x\n"),
        "list_templates": lambda: server.list_templates(),
        "wait_for_nodes": lambda: server.wait_for_nodes(timeout=0),
        "ack_nodes": lambda: server.ack_nodes(0),
        "scheduler_status": lambda: server.scheduler_status(),
    }


def registered_tools() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def disk_footprint(sched) -> list[Path]:
    return [p for p in (*files_under(sched.state_dir), *files_under(sched.rpc_dir))]


# ================================================================== the tools exist

def test_nc_r13_every_node_tool_is_registered_with_the_mcp_server():
    assert set(TOOLS) <= registered_tools()


# ================================================================== NC-R1: gate off

def test_nc_r1_gate_off_every_node_tool_says_scheduler_disabled(off):
    for name, call in calls().items():
        result = tool(call)
        assert result.get("error") == "scheduler_disabled", (name, result)


def test_nc_r1_gate_off_a_node_tool_call_leaves_no_file_under_the_scheduler_dirs(off):
    for call in calls().values():
        tool(call)
    assert disk_footprint(off) == []
    assert not off.sock.exists()


def test_nc_r1_gate_off_with_no_pending_nodes_there_is_no_pending_count(off):
    result = tool(server.list_nodes)
    assert result.get("error") == "scheduler_disabled"
    assert not result.get("pending_nodes")


def test_nc_r1_gate_off_is_the_default_when_the_project_says_nothing(off):
    off.set_scheduler(None)
    assert tool(server.list_nodes).get("error") == "scheduler_disabled"


def test_nc_r2_the_gate_is_read_through_global_then_project(off, monkeypatch):
    gdir = Path(os.environ["MULTIAGENTS_CONFIG_DIR"])
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "project.yaml").write_text("scheduler:\n  enabled: true\n")
    off.set_scheduler(None)                          # project silent: the global gate applies
    assert tool(server.list_nodes).get("error") == "scheduler_unavailable"
    off.set_scheduler({"enabled": False})            # project overrides
    assert tool(server.list_nodes).get("error") == "scheduler_disabled"


# ================================================================== NC-R1: gate on, no scheduler

def test_nc_r1_gate_on_without_a_scheduler_every_node_tool_says_unavailable(nc):
    for name, call in calls().items():
        result = tool(call)
        assert result.get("error") == "scheduler_unavailable", (name, result)


def test_nc_r1_gate_on_without_a_scheduler_there_is_no_direct_fallback_and_no_side_effect(nc):
    for call in calls().values():
        tool(call)
    assert disk_footprint(nc) == []
    assert not nc.sock.exists()


def test_nc_r1_a_tool_call_never_starts_the_scheduler(nc):
    tool(server.scheduler_status)
    tool(server.list_nodes)
    assert not nc.sock.exists()


# ================================================================== NC-R13: gate on, running

def test_nc_r13_the_orchestrator_creates_reads_edits_and_cancels_through_the_tools(live):
    base = tool(server.list_nodes)
    node = tool(lambda: server.create_node(kind="simple", agent="worker", task="via tool",
                                           plan_revision=base["plan_revision"]))
    assert "error" not in node, node
    assert re.fullmatch(r"nd-[0-9a-f]{8}", node["id"])
    assert "root" in str(node["created_by"]).lower()
    assert tool(lambda: server.get_node(node["id"]))["task"] == "via tool"
    assert [n["id"] for n in tool(server.list_nodes)["nodes"]] == [node["id"]]
    edited = tool(lambda: server.update_node(node["id"], node["revision"], task="retargeted"))
    assert "error" not in edited and edited["task"] == "retargeted"
    cancelled = tool(lambda: server.cancel_node(node["id"], edited["revision"]))
    assert cancelled["state"] == "cancelled"
    assert live.get(node["id"])["state"] == "cancelled"        # the RPC sees the same plan


def test_nc_r13_a_tool_reports_conflict_and_invalid_like_the_rpc(live):
    node = live.create()
    stale = tool(lambda: server.update_node(node["id"], node["revision"] + 3, task="x"))
    assert stale.get("error") == "conflict" and "current_revision" in stale
    bad = tool(lambda: server.create_node(kind="simple", agent="ghost", task="t",
                                          plan_revision=live.plan_revision()))
    assert bad.get("error") == "invalid" and bad.get("problems")


def test_nc_r13_wait_and_ack_tools_share_the_durable_cursor(live):
    live.create(task="a")
    first = tool(lambda: server.wait_for_nodes(timeout=1))
    assert [tname(t) for t in first["transitions"]][-1] == "created"
    ack = tool(lambda: server.ack_nodes(first["next_cursor"]))
    assert "error" not in ack
    live.restart(hard=True)
    second = tool(lambda: server.wait_for_nodes(timeout=1))
    assert [tname(t) for t in second["transitions"]] == ["scheduler_started"]


def test_nc_r13_scheduler_status_tool_reports_the_live_pid(live):
    result = tool(server.scheduler_status)
    assert result["pid"] == live.pid()


def test_nc_r13_list_templates_is_answerable_with_nothing_registered(live):
    result = tool(server.list_templates)
    assert "error" not in result


@pytest.mark.parametrize("hard", [False, True])
def test_nc_r8_the_tools_reconnect_after_a_scheduler_restart(live, hard):
    node = live.create(task="persist")
    assert tool(server.list_nodes)["nodes"]
    live.restart(hard=hard)
    assert tool(lambda: server.get_node(node["id"]))["task"] == "persist"


# ================================================================== NC-R9 through the tools

def test_nc_r9_a_subagents_server_without_a_token_does_not_fall_back_to_root(live, monkeypatch):
    live.create(task="exists")
    before = live.snapshot()
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-sub")
    monkeypatch.delenv("MULTIAGENTS_RPC_TOKEN", raising=False)
    server._reset()
    plan = before["plan_revision"]
    for name, call in calls(plan).items():
        result = tool(call)
        assert result.get("error") == "unauthenticated", (name, result)
    assert live.snapshot() == before


def test_nc_r9_a_subagents_server_acts_with_its_run_token_and_stays_in_scope(live, monkeypatch):
    mine = live.create(task="the run's node")
    outsider = live.create(task="elsewhere")
    token = live.issue("run-1", mine["id"])
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run-1")
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    server._reset()
    plan = tool(server.list_nodes)["plan_revision"]
    made = tool(lambda: server.create_node(kind="simple", agent="worker", task="child",
                                           parent=mine["id"], plan_revision=plan))
    assert "error" not in made, made
    assert "run-1" in str(live.get(made["id"])["created_by"])
    plan = tool(server.list_nodes)["plan_revision"]
    refused = tool(lambda: server.create_node(kind="simple", agent="worker", task="out",
                                              parent=outsider["id"], plan_revision=plan))
    assert refused.get("error") == "forbidden", refused
    assert tool(lambda: server.get_node(outsider["id"])).get("error") == "forbidden"


def test_nc_r9_a_revoked_token_through_the_tool_is_unauthenticated(live, monkeypatch):
    mine = live.create(task="the run's node")
    token = live.issue("run-1", mine["id"])
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run-1")
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    server._reset()
    assert "error" not in tool(lambda: server.get_node(mine["id"]))
    live.revoke("run-1")
    assert tool(lambda: server.get_node(mine["id"])).get("error") == "unauthenticated"


# ================================================================== NC-R55: gate off, plans pending

@pytest.fixture
def doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.auth_mod, "check_all", lambda *a: {})
    monkeypatch.setattr(cli, "_driver_host_states", lambda *a: {})
    monkeypatch.setattr(cli, "read_all", lambda *a: {})
    monkeypatch.setattr(cli, "_report_agents", lambda *a: 0)
    monkeypatch.setattr(cli, "find_shadowing", lambda *a: [])
    monkeypatch.setattr(manifest, "cli_dependencies_section", lambda *a: 0)

    def run(sched):
        capsys.readouterr()
        rc = cli.cmd_doctor(argparse.Namespace(path=str(sched.root), clear=None, force=False))
        out = capsys.readouterr().out
        match = re.search(r"^(\d+) problem\(s\)$", out, re.M)
        return rc, int(match.group(1)) if match else 0, out

    return run


@pytest.fixture
def pending(live):
    """Three nodes, one cancelled; the scheduler stopped and the gate turned off."""
    a = live.create(task="a")
    b = live.create(task="b")
    c = live.create(task="c")
    live.ok("cancel_node", {"id": c["id"], "revision": c["revision"]})
    live.stop()
    live.set_gate(False)
    return live, a, b, c


def test_nc_r55_the_tools_report_how_many_non_terminal_nodes_are_pending(pending):
    live, a, b, c = pending
    for name, call in calls().items():
        result = tool(call)
        assert result.get("error") == "scheduler_disabled", (name, result)
        assert result.get("pending_nodes") == 2, (name, result)


def test_nc_r55_cancelled_and_done_nodes_are_not_pending(live):
    a = live.create(task="a")
    live.ok("cancel_node", {"id": a["id"], "revision": a["revision"]})
    live.stop()
    live.set_gate(False)
    result = tool(server.list_nodes)
    assert result.get("error") == "scheduler_disabled"
    assert not result.get("pending_nodes")


def test_nc_r55_the_scheduler_does_not_start_with_the_gate_off_and_nodes_pending(pending):
    live, *_ = pending
    out = live.cli("scheduler", "start", timeout=30)
    assert "invalid choice" not in out.stderr and "usage:" not in out.stderr, out.stderr
    assert not live.sock.exists()


def test_nc_r55_a_node_tool_call_with_pending_nodes_creates_nothing(pending):
    live, *_ = pending
    result = tool(lambda: server.create_node(kind="simple", agent="worker", task="new",
                                             plan_revision=0))
    assert result.get("error") == "scheduler_disabled"
    live.set_gate(True)
    live.start()
    assert len(live.snapshot()["nodes"]) == 3


def test_nc_r55_doctor_reports_one_problem_naming_the_pending_nodes(pending, doctor):
    live, a, b, c = pending
    _, with_pending, out = doctor(live)
    # the same project with nothing pending
    other = live.tmp / "second"
    from nc_harness import Sched
    clean = Sched(other, live.monkeypatch, enabled=False)
    try:
        _, baseline, _ = doctor(clean)
    finally:
        clean.close()
    live.monkeypatch.setenv("MULTIAGENTS_PROJECT", str(live.root))
    assert with_pending == baseline + 1, out
    assert a["id"] in out and b["id"] in out
    assert c["id"] not in out                # a cancelled node is not pending


def test_nc_r55_with_no_pending_nodes_doctor_has_nothing_to_say_about_the_gate(live, doctor):
    a = live.create(task="a")
    live.ok("cancel_node", {"id": a["id"], "revision": a["revision"]})
    live.stop()
    live.set_gate(False)
    _, problems, out = doctor(live)
    assert a["id"] not in out


def test_nc_r55_turning_the_gate_back_on_resumes_plans_cursors_and_the_log(pending):
    live, a, b, c = pending
    live.set_gate(True)
    live.start()
    assert sorted(n["id"] for n in live.snapshot()["nodes"]) == sorted(
        [a["id"], b["id"], c["id"]])
    assert live.get(c["id"])["state"] == "cancelled"
    names = live.names()
    assert names[:4] == ["scheduler_started", "created", "created", "created"]
    assert names.count("scheduler_started") == 2 and names.count("scheduler_stopped") == 1


def test_nc_r55_the_acknowledged_cursor_is_retained_across_a_gate_off_period(live):
    a = live.create(task="a")
    top = live.ok("wait_for_nodes", {"timeout": 1})["next_cursor"]
    live.ok("ack_nodes", {"cursor": top})
    live.stop()
    live.set_gate(False)
    live.set_gate(True)
    live.start()
    names = [tname(t) for t in live.ok("wait_for_nodes", {"timeout": 1})["transitions"]]
    assert names == ["scheduler_stopped", "scheduler_started"]
