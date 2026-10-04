"""Adversary round 2 (M1, NC-R9/NC-R76): token selection in the MCP server.

Property: a server running as a run (MULTIAGENTS_AGENT_ID set) never reads
the root capability, through any tool. The node tools hold; `start_agent`'s
gate-on branch calls `scheduler.status()`, which reads the root capability
and presents it, whatever the server's identity.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import live, nc, tool  # noqa: E402,F401
from multiagents import server  # noqa: E402


def _no_root_reads(monkeypatch):
    from multiagents import scheduler
    reads = []
    real = scheduler.root_capability

    def recording(*args, **kwargs):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler, "root_capability", recording)
    return reads


def test_adv2_a_run_servers_start_agent_never_reads_the_root_capability(live, monkeypatch):
    node = live.create(task="the run's node")
    token = live.issue("run-1", node["id"], {"read", "delegate"})
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run-1")
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    reads = _no_root_reads(monkeypatch)
    monkeypatch.setattr(server, "runner", lambda: (_ for _ in ()).throw(
        AssertionError("no direct-launch fallback")))
    result = tool(lambda: server.start_agent("worker", "delegated work"))
    assert not reads, (f"start_agent in a server with MULTIAGENTS_AGENT_ID=run-1 read the root "
                       f"capability {len(reads)} time(s); it replied {result}")


def test_adv2_a_run_server_without_a_token_never_reads_the_root_capability(live, monkeypatch):
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "run-1")
    monkeypatch.delenv("MULTIAGENTS_RPC_TOKEN", raising=False)
    reads = _no_root_reads(monkeypatch)
    monkeypatch.setattr(server, "runner", lambda: (_ for _ in ()).throw(
        AssertionError("no direct-launch fallback")))
    result = tool(lambda: server.start_agent("worker", "delegated work"))
    assert not reads, f"read the root capability; replied {result}"
