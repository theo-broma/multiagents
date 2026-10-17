"""Adversary, UD slice 2: a run capability present but the agent id stripped
or emptied reaches `mcp_root` (D-UD2.1).

D-UD2.1: `mcp_run` is "an MCP server or scoped RPC call that carries a run
capability". server._decision_caller looks only at the truthiness of
MULTIAGENTS_AGENT_ID, so a server carrying MULTIAGENTS_RPC_TOKEN (only runs
are ever issued one; a root uses the root capability file) or an empty
MULTIAGENTS_AGENT_ID is the orchestrator. _node_rpc in the same file treats
the presence of the variable as the run marker, so the two disagree.

LOW: reaching it needs control of the server's environment, which a local
agent with a shell already has in other ways. Stated because the task asks for
the stripped-capability case explicitly.
"""
import pytest

from support.ud2_support import ask, clean_env, world  # noqa: F401
from support.ud_support import D


@pytest.mark.parametrize("env", [
    {"MULTIAGENTS_AGENT_ID": ""},
    {"MULTIAGENTS_AGENT_ID": "", "MULTIAGENTS_RPC_TOKEN": "tok-xxxxxxxx"},
    {"MULTIAGENTS_RPC_TOKEN": "tok-xxxxxxxx"},
], ids=["empty-id", "empty-id+token", "token-no-id"])
def test_hole_run_capability_without_agent_id_is_mcp_root(world, monkeypatch, env):
    clean_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out = ask()
    assert out.get("error") == "forbidden", out
    assert D.list(world.tree)["decisions"] == []
