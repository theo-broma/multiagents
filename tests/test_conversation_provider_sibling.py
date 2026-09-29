"""CX-C28, the sibling case (context/specs/codex-provider.md, "Decisions on
the CX-C28 tester's questions"): a standing conversation on another instance
of the roster provider's family is resumed on that instance with the roster's
model, since a family shares model ids — never replaced, and never routed
through an empty fallback."""
from __future__ import annotations

import asyncio

from multiagents.config import AgentSpec
from multiagents.tree import Node

from test_conversation_provider_change import (OLD_NODE, OLD_SESSION,
                                               _calls, _events, _fake_cli,
                                               _flag, h)


def test_cx_c28_a_sibling_instance_is_resumed_with_the_rosters_model(tmp_path, monkeypatch):
    acme, acme_probe = _fake_cli(tmp_path, "acme")
    acme2, acme2_probe = _fake_cli(tmp_path, "acme2")
    acme2["family"] = "acme"
    spec = AgentSpec("advisor", "acme", "acme-large", conversational=True)
    runner = h.make_runner(tmp_path / "proj", monkeypatch,
                           agents={"advisor": spec},
                           providers={"acme": acme, "acme2": acme2})
    worktree = runner.paths.worktree(OLD_NODE)
    worktree.mkdir(parents=True)
    runner.tree.add(Node(
        id=OLD_NODE, agent="advisor", provider="acme2", model="acme-large",
        parent=None, depth=1, status="idle", session_id=OLD_SESSION,
        worktree=str(worktree), conversation=True, turns=1))

    result = asyncio.run(runner.consult("advisor", "next", timeout=60))

    assert not _calls(acme_probe), f"acme ran instead of the sibling: {result}"
    calls = _calls(acme2_probe)
    assert len(calls) == 1, f"the sibling was not resumed: {calls}; {result}"
    assert _flag(calls[0], "--resume") == OLD_SESSION
    assert _flag(calls[0], "--model") == "acme-large"
    assert result.get("agent_id") == OLD_NODE
    assert not _events(runner, "conversation_replaced")
