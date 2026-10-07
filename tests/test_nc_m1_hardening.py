"""Additional NC contracts at the client and capability boundaries."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from nc_harness import code, live, nc, tool  # noqa: E402,F401
from multiagents import config, server  # noqa: E402
from multiagents.paths import ProjectPaths, global_config_dir  # noqa: E402


SRC = str(Path(__file__).resolve().parents[1] / "src")
LISTING = (
    "import asyncio, json, sys; sys.path.insert(0, sys.argv[1]);"
    "from multiagents import server;"
    "print(json.dumps([t.model_dump(by_alias=True) for t in asyncio.run(server.mcp.list_tools())],"
    " default=str))"
)
LIST_TIMEOUT = 60


def root_tool_infos(tmp_path) -> list[dict]:
    """The root orchestrator's tool list. The registry is fixed when the server
    module is imported, from the environment, so list it in a fresh interpreter
    that carries no agent identity or permissions."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("MULTIAGENTS_", "CLAUDE_"))}
    clean["HOME"] = str(tmp_path)
    run = subprocess.run([sys.executable, "-c", LISTING, SRC], cwd=tmp_path, env=clean,
                         capture_output=True, text=True, timeout=LIST_TIMEOUT)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_nc_r60_run_update_replay_survives_a_later_cancellation(live):
    owner = live.create()
    token = live.issue("run-owner", owner["id"])
    child = live.create(token=token)
    args = {"id": child["id"], "revision": child["revision"], "task": "changed"}
    first = live.rpc("update_node", args, token, request_id="edit-before-cancel")
    assert first["ok"]
    live.cancel_raw(child["id"])
    replay = live.rpc("update_node", args, token, request_id="edit-before-cancel")
    assert replay == first
    assert live.get(child["id"])["state"] == "cancelled"


def test_nc_r3_transport_mount_is_read_only_so_clients_cannot_replace_the_socket(nc):
    from multiagents.executor.docker import DockerExecutor
    from multiagents.providers import load_providers
    paths = ProjectPaths(nc.root)
    cfg = config.load(paths, seed=False)
    executor = DockerExecutor({}, paths, load_providers(cfg.providers), global_config_dir())
    mounts = dict(executor.mounts())
    assert mounts[nc.rpc_dir] is True


def test_nc_r4_simple_children_are_owned_by_delegation_not_client_fields(live):
    before = live.snapshot()
    reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "t", "children": []})
    assert code(reply) == "invalid"
    assert live.snapshot() == before


def test_nc_r76_partial_tool_edits_preserve_a_window_and_null_clears_it(live):
    window = {"days": ["mon"], "ranges": ["09:00-17:00"]}
    node = live.create(window=window)
    result = tool(lambda: server.update_node(node["id"], node["revision"], task="edited"))
    assert result["window"] == window
    result = tool(lambda: server.update_node(node["id"], result["revision"], window=None))
    assert result["window"] is None
    reply = tool(lambda: server.update_node(node["id"], result["revision"], window=""))
    assert reply["error"] == "invalid"


def test_nc_r76_the_window_tool_schema_accepts_only_an_object_or_null(tmp_path):
    info = next(t for t in root_tool_infos(tmp_path) if t["name"] == "update_node")
    schema = info.get("inputSchema", info.get("input_schema"))
    window = schema["properties"]["window"]
    assert {choice["type"] for choice in window["anyOf"]} == {"object", "null"}


def test_nc_r71_root_is_not_a_run_subject(live):
    node = live.create()
    with pytest.raises(ValueError):
        live.issue("root", node["id"])


def test_nc_r77_acknowledging_a_future_cursor_is_refused(live):
    live.create()
    before = live.ok("wait_for_nodes", {"timeout": 0})
    reply = live.rpc("ack_nodes", {"cursor": before["next_cursor"] + 1})
    assert code(reply) == "invalid_cursor"
    assert live.ok("wait_for_nodes", {"timeout": 0}) == before


def test_nc_r72_edits_and_cancellations_do_not_move_the_plan_revision(live):
    node = live.create()
    revision = live.plan_revision()
    edited = live.update(node["id"], task="new")
    cancelled = live.ok("cancel_node", {"id": node["id"], "revision": edited["revision"]})
    assert edited["plan_revision"] == cancelled["plan_revision"] == live.plan_revision() == revision


def test_nc_r76_an_empty_agent_id_environment_does_not_grant_root(live, monkeypatch):
    node = live.create()
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "")
    monkeypatch.delenv("MULTIAGENTS_RPC_TOKEN", raising=False)
    from multiagents import scheduler

    def root_must_not_be_read(*args):
        raise AssertionError("an agent server must never read the root capability")

    monkeypatch.setattr(scheduler, "root_capability", root_must_not_be_read)
    assert tool(lambda: server.get_node(node["id"]))["error"] == "unauthenticated"


def test_nc_r1_gate_on_start_agent_without_service_never_calls_runner(nc, monkeypatch):
    def no_runner():
        raise AssertionError("no direct-launch fallback")

    monkeypatch.setattr(server, "runner", no_runner)
    assert tool(lambda: server.start_agent("worker", "waiting"))["error"] == "scheduler_unavailable"


def test_nc_r50_concurrent_cli_starts_all_observe_one_ready_scheduler(nc):
    import json
    replies = nc.in_threads([lambda: nc.cli("scheduler", "start") for _ in range(8)])
    pid = nc.pid()
    assert all(reply.returncode == 0 for reply in replies), replies
    assert {json.loads(reply.stdout)["pid"] for reply in replies} == {pid}


def test_nc_r60_replayed_mcp_request_keeps_its_rpc_request_identity(live, monkeypatch):
    from types import SimpleNamespace
    args = {"kind": "simple", "agent": "worker", "task": "one deposit",
            "plan_revision": live.plan_revision(),
            "ctx": SimpleNamespace(request_id="deposit-retry")}
    first = server.create_node(**args)
    assert "error" not in first
    second = server.create_node(**args)
    assert second == first
    assert len(live.snapshot()["nodes"]) == 1


def test_nc_r8_a_lost_rpc_reply_is_retried_without_a_second_deposit(live, monkeypatch):
    from multiagents import scheduler
    base = live.plan_revision()
    real_socket = scheduler.socket.socket
    first = True

    class DroppedReader:
        def __init__(self, reader):
            self.reader = reader

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.reader.__exit__(*args)

        def readline(self):
            assert self.reader.readline(), "the host committed and replied"
            raise ConnectionResetError("reply lost after commit")

    class DroppedSocket:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.sock.__exit__(*args)

        def makefile(self, *args):
            return DroppedReader(self.sock.makefile(*args))

    def connect(*args, **kwargs):
        nonlocal first
        sock = real_socket(*args, **kwargs)
        if first:
            first = False
            return DroppedSocket(sock)
        return sock

    monkeypatch.setattr(scheduler.socket, "socket", connect)
    node = server.create_node(kind="simple", agent="worker", task="one deposit", plan_revision=base)
    assert "error" not in node
    assert [n["id"] for n in live.snapshot()["nodes"]] == [node["id"]]


def test_nc_r1_enabling_the_gate_cannot_fall_back_after_an_unrelated_reload_error(nc, monkeypatch):
    nc.set_gate(False)
    server.list_agents()  # Establish the last valid legacy configuration.
    assert server._runner is not None

    async def no_direct_launch(*args, **kwargs):
        raise AssertionError("the fresh scheduler gate must still prevent direct launch")

    monkeypatch.setattr(server._runner, "start", no_direct_launch)
    nc.p.project["scheduler"]["enabled"] = True
    nc.p.cap("acme", {"usd": "unlimited"})  # Invalid provider config, valid gate.
    result = tool(lambda: server.start_agent("worker", "planned work"))
    assert result["error"] == "scheduler_unavailable"


def test_nc_r3_event_mirroring_never_follows_a_project_symlink_to_host_data(live):
    victim = live.tmp / "host-data.txt"
    victim.write_text("host-owned data\n")
    live.events.unlink()
    live.events.symlink_to(victim)
    live.create()
    assert victim.read_text() == "host-owned data\n"
    assert not live.events.is_symlink()


def test_nc_r14_non_object_json_in_the_public_event_log_does_not_stop_startup(nc):
    nc.events.write_text('null\n[]\n"old event"\n')
    nc.start()
    nc.create()
    assert nc.names() == ["scheduler_started", "created"]


def test_nc_r14_a_replay_repairs_the_event_mirror_after_an_io_failure(live):
    from types import SimpleNamespace
    args = {"kind": "simple", "agent": "worker", "task": "durable deposit",
            "plan_revision": live.plan_revision(),
            "ctx": SimpleNamespace(request_id="retry-after-io")}
    live.events.unlink()
    live.events.mkdir()  # The RPC effect can commit while its mirror cannot append.
    server.create_node(**args)
    live.events.rmdir()
    node = server.create_node(**args)
    assert "error" not in node
    assert len(live.snapshot()["nodes"]) == 1
    assert live.event_kinds() == ["scheduler_started", "created"]
