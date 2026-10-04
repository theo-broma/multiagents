"""Round-two guards for dependency inheritance, status identity and appends."""
from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from nc_harness import code, live, nc, problems, tool  # noqa: E402,F401
from multiagents import scheduler, server  # noqa: E402


@pytest.mark.parametrize("op", ["create", "update"])
def test_nc_r6_m27_extra_keys_in_dependency_references_are_invalid(live, op):
    source = live.create(task="source")
    target = live.create(task="target")
    before, transitions = live.snapshot(), live.transitions()
    fields = {"depends_on": [{"node": source["id"], "require": "success", "unexpected": True}]}
    if op == "create":
        reply = live.create_raw({"kind": "simple", "agent": "worker", "task": "new", **fields})
    else:
        reply = live.update_raw(target["id"], **fields)
    assert code(reply) == "invalid", reply
    assert any("depends_on" in str(problem) for problem in problems(reply))
    assert live.snapshot() == before
    assert live.transitions() == transitions


def test_nc_r6_nested_sequence_prerequisites_do_not_inherit_completion_edges(live):
    first = live.create()
    leaf = live.create()
    inner = live.create(kind="group", children=[leaf["id"]])
    second = live.create(kind="sequence", children=[inner["id"]])
    sequence = live.create(kind="sequence", children=[first["id"], second["id"]])
    assert live.get(sequence["id"])["children"] == [first["id"], second["id"]]
    assert live.update(first["id"], task="a valid edit")["task"] == "a valid edit"


def test_nc_r6_an_ancestors_input_prerequisite_gates_its_descendants(live):
    source = live.create()
    leaf = live.create()
    live.create(kind="group", children=[leaf["id"]], inputs=[{"node": source["id"]}])
    before = live.snapshot()
    assert code(live.update_raw(source["id"], depends_on=[{"node": leaf["id"]}])) == "invalid"
    assert live.snapshot() == before


@pytest.mark.parametrize("agent_id", ["run", ""])
@pytest.mark.parametrize("token", [None, ""])
def test_nc_r76_start_agent_without_a_run_token_is_unauthenticated(live, monkeypatch, agent_id, token):
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", agent_id)
    if token is None:
        monkeypatch.delenv("MULTIAGENTS_RPC_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)

    def no_root(*args):
        raise AssertionError("a run server must never read the root capability")

    monkeypatch.setattr(scheduler, "root_capability", no_root)
    assert tool(lambda: server.start_agent("worker", "delegated")) == {"error": "unauthenticated"}


def test_nc_r76_start_agent_sends_only_the_supplied_run_token(live, monkeypatch):
    own = live.create()
    token = live.issue("real-subject", own["id"], {"read", "delegate"})
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "an-untrusted-caller-label")
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    real_call, calls = scheduler.call, []

    def call(root, op, args, credential, **kwargs):
        calls.append((op, credential))
        return real_call(root, op, args, credential, **kwargs)

    def no_root(*args):
        raise AssertionError("a run server must never read the root capability")

    monkeypatch.setattr(scheduler, "call", call)
    monkeypatch.setattr(scheduler, "root_capability", no_root)
    assert tool(lambda: server.start_agent("worker", "delegated")) == {"error": "not_implemented"}
    assert calls == [("scheduler_status", token)]
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", "not-issued")
    assert tool(lambda: server.start_agent("worker", "delegated")) == {"error": "unauthenticated"}
    assert calls[-1] == ("scheduler_status", "not-issued")


@pytest.mark.parametrize("lock", [fcntl.LOCK_SH, fcntl.LOCK_EX])
def test_nc_r14_shared_and_exclusive_public_locks_cannot_suppress_appends(live, lock):
    with live.events.open("ab") as held:
        fcntl.flock(held, lock)
        live.create(task="append independently of advisory locks")
        assert live.event_kinds() == ["scheduler_started", "created"]
