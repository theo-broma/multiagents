"""Adversary, UD slice 2: the legacy `answer_question` MCP tool as a side door
around `authorize` (UD-C8, D-UD2.1).

Each test names the hole and its severity. They fail now and pass once fixed.
"""
import asyncio

import pytest

from support.ud2_support import as_role, as_run, call, world  # noqa: F401
from support.ud_support import D


def _parked(world, agent="ag-3", parent=None):
    """A parked agent with a resumable session and an open question; steer stubbed."""
    from multiagents.tree import Node
    world.tree.add(Node(id=agent, agent="a", provider="p", model="m", parent=parent,
                        depth=1 if parent is None else 2, session_id="ses-" + agent))
    return world.tree.add_question(agent, "store", "Postgres or SQLite?", "SQLite")


def _stub_steer(world, monkeypatch):
    steered = []

    async def fake_steer(agent_id, message, *a, **kw):
        steered.append(agent_id)
        return {"steered": True}
    monkeypatch.setattr(world.runner, "steer", fake_steer)
    monkeypatch.setattr(world.runner, "_transport_refusal", lambda provider: None)
    return steered


# MEDIUM — identity laundering. A root server whose MULTIAGENTS_ROLE is
# `initializer` answers an orchestrator-audience question through the legacy
# tool: server.answer_question hard-codes role "orchestrator" for every root
# caller (server.py, the `actor = ... "orchestrator" ... "mcp_root"` line), so
# authorize sees the orchestrator, lets it through, and the record says the
# orchestrator answered. answer_decision refuses the same initializer
# (test_c8_the_initializer_cannot_answer_even_an_orchestrator_decision).
def test_hole_initializer_answers_agent_question_as_orchestrator_via_legacy_tool(
        world, monkeypatch):
    q = _parked(world)
    steered = _stub_steer(world, monkeypatch)
    as_role(monkeypatch, "initializer")
    out = call("answer_question", question_id=q["id"], answer="postgres")
    rec = D.get(world.tree, q["id"])
    assert "forbidden" in str(out.get("error", "")).lower(), (out, rec["answer"])
    assert rec["status"] == "open" and steered == []


# MEDIUM — same door, D-UD2.1: "Any other role value is forbidden". A root
# server with an unknown MULTIAGENTS_ROLE is still treated as the orchestrator
# by the legacy tool (the decision tools refuse it).
@pytest.mark.parametrize("role", ["user", "bogus"])
def test_hole_unknown_root_role_answers_via_legacy_tool(world, monkeypatch, role):
    q = _parked(world)
    steered = _stub_steer(world, monkeypatch)
    as_role(monkeypatch, role)
    out = call("answer_question", question_id=q["id"], answer="postgres")
    rec = D.get(world.tree, q["id"])
    assert "forbidden" in str(out.get("error", "")).lower(), (out, rec["answer"])
    assert rec["status"] == "open" and steered == []


# MEDIUM — delegate scope (Slice 2 clarification "Delegates (amends UD-C8)"):
# a run may answer through the legacy tool only an orchestrator question
# "asked by one of its own descendants". authorize() takes `delegate` as
# `channel == "mcp_run"` (runner.py answer_question) and never looks at the
# asker, so a run answers its SIBLING's question and the sibling is resumed on
# the run's answer. Reproduced without the scheduler gate (scheduler off, a
# node with no node_id), where nothing else checks the subtree.
def test_hole_run_answers_a_non_descendants_question_via_legacy_tool(world, monkeypatch):
    from multiagents.tree import Node
    world.tree.add(Node(id="ag-7", agent="a", provider="p", model="m", parent=None,
                        depth=1, session_id="ses-7"))
    q = _parked(world, "ag-3")                     # a sibling of ag-7, not its descendant
    steered = _stub_steer(world, monkeypatch)
    as_run(monkeypatch, "ag-7")
    out = asyncio.run(world.runner.answer_question(
        q["id"], "drop the database", actor={"role": "agent", "agent": "ag-7",
                                             "channel": "mcp_run"}))
    rec = D.get(world.tree, q["id"])
    assert "forbidden" in str(out.get("error", "")).lower(), (out, rec["answer"])
    assert rec["status"] == "open" and steered == []


# LOW — D-UD2.1: "No tool accepts by, answered_by, asked_by, role or channel
# as an argument. One that is passed anyway is refused with invalid_argument."
# The stray-argument guard (server._reject_identity_arguments) covers only the
# five new tools; the legacy answer_question silently drops a forged
# `answered_by` and proceeds.
@pytest.mark.parametrize("arg,value", [("answered_by", "user"), ("role", "user"),
                                       ("channel", "cli"),
                                       ("by", {"role": "user", "agent": "", "channel": "cli"})])
def test_hole_legacy_answer_question_accepts_identity_arguments(world, monkeypatch, arg, value):
    q = _parked(world)
    steered = _stub_steer(world, monkeypatch)
    out = call("answer_question", question_id=q["id"], answer="postgres", **{arg: value})
    assert out.get("error") == "invalid_argument", out
    assert D.get(world.tree, q["id"])["status"] == "open" and steered == []
