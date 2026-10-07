"""Additional TB2-R7 guards for hold episodes and attached delivery."""
from __future__ import annotations

import json
import threading
import time

import pytest

from multiagents import driver
from multiagents.scheduler import attention
from multiagents.scheduler.model import create_record
from multiagents.scheduler.store import Store, encode
from test_phase0_interactive_compact import Session


def hold(store, reason, node=None, *, subject="root"):
    node = node or create_record({"kind": "simple", "agent": "worker", "task": "work"}, subject)
    node.update(state="held", hold={"reason": reason, "detail": "resolve this cause"},
                revision=node["revision"] + 1)
    with store.transaction() as db:
        store.save_node(db, node)
        store.transition(db, "held", node["id"], node["hold"])
    store.mirror()
    return node


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    return store


@pytest.mark.parametrize("reason", [
    "rate_limited", "disposal_failed", "admission:refused", "integration_conflict",
    "result_capture_failed", "run_failed", "session_lost", "termination_unconfirmed",
    "future_reason",
])
def test_root_action_reasons_are_signalled(store, reason):
    node = hold(store, reason)
    pending = attention.pending(store)
    assert [h["node_id"] for h in pending] == [node["id"]]
    assert reason in attention.message(pending)


@pytest.mark.parametrize("reason", ["child_held", "stopped_by_orchestrator"])
def test_automatic_or_root_caused_holds_are_excluded(store, reason):
    hold(store, reason)
    assert attention.pending(store) == []
    assert json.loads((store.paths.data / "attention.json").read_text())["holds"] == []


def test_window_confirmation_excludes_only_its_own_hold_revision(store):
    node = hold(store, "termination_unconfirmed")
    attempt = {"node_id": node["id"], "window_stop": True, "window_hold_revision": node["revision"]}
    with store.transaction() as db:
        db.execute("INSERT INTO attempts VALUES (?, ?)", ("attempt", encode(attempt)))
    assert attention.pending(store) == []
    node["revision"] += 1
    with store.transaction() as db:
        store.save_node(db, node)
    assert attention.pending(store)[0]["node_id"] == node["id"]


def test_delivery_is_independent_of_ack_and_a_new_episode_is_sent_again(store):
    node = hold(store, "needs_info")
    first = attention.pending(store)
    attention.delivered(store, first)
    assert attention.pending(store) == []
    with store.transaction() as db:
        assert int(store.meta(db, "ack")) == 0
    node.update(state="open", hold=None, revision=node["revision"] + 1)
    with store.transaction() as db:
        store.save_node(db, node)
    hold(store, "needs_info", node)
    assert attention.pending(store)[0]["mark"] != first[0]["mark"]


def test_a_cleared_hold_is_not_delivered(store):
    node = hold(store, "run_failed")
    node.update(state="open", hold=None)
    with store.transaction() as db:
        store.save_node(db, node)
    store.mirror()
    assert attention.pending(store) == []
    assert json.loads((store.paths.data / "attention.json").read_text())["holds"] == []


def test_attached_signal_waits_for_the_turn_boundary_and_resumes_once(tmp_path, monkeypatch, capsys):
    session = Session(tmp_path, monkeypatch, capsys, compact_at=0)
    session.config.project["scheduler"] = {"enabled": True}
    session.provider.transcript["turn_state"] = [
        {"when": {"type": "system", "subtype": "turn_duration"}, "idle": True},
        {"when": {"type": "user"}, "idle": False},
    ]
    session.reading(100)
    monkeypatch.setattr(driver, "_keep_scheduler", lambda *_: None)
    store = Store(session.root)
    store.initialize()
    box = {}

    def publish():
        deadline = time.monotonic() + 5
        while not session.calls("launch") and time.monotonic() < deadline:
            time.sleep(0.02)
        box["node"] = hold(store, "input_conflict")
        box["message"] = attention.message(attention.pending(store))

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        code, _ = session.run([
            {"life": 6, "appends": [[0.8, [{"type": "system", "subtype": "turn_duration"}]]]},
            {"life": 0.5, "appends": [[0.1, [{"type": "user", "message": {"content": "received"}}]]]},
        ])
    finally:
        thread.join(8)
    assert not thread.is_alive()
    assert code == 0
    launches = session.calls("launch")
    assert len(launches) == 2
    assert "MULTIAGENTS_RESUME_PROMPT" not in launches[0]["env"]
    assert launches[1]["env"]["MULTIAGENTS_RESUME_PROMPT"] == box["message"]
    assert launches[1]["env"]["MULTIAGENTS_RESUME"] == "1"
    assert session.stops()[0]["t"] >= session.calls("append")[0]["t"]
    assert not session.calls("compact")
    assert attention.pending(store) == []


def test_a_hold_and_a_simultaneous_wait_error_are_both_returned(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from multiagents import server

    entry = {"node_id": "nd-child", "hold": {"reason": "run_failed"}}
    replies = [{"holds": [], "transitions": []}, {"holds": [entry], "transitions": []}]

    async def inline_thread(fn, *args):
        return fn(*args)

    async def native_wait(*args):
        return {"error": "lost_connection"}

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(server, "_node_rpc", lambda *_: replies.pop(0))
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}))
    result = asyncio.run(server._wait_with_holds(run, ["ag-current"], 1))
    assert result["error"] == "lost_connection"
    assert entry in result["changed"]


def test_subagent_hold_wait_does_not_leak_a_created_node_outside_its_subtree(store):
    from multiagents.scheduler.rpc import Service
    from multiagents.scheduler.store import issue_run_capability

    parent = create_record({"kind": "group"}, "root")
    mine = create_record({"kind": "simple", "agent": "worker", "task": "mine",
                          "parent": parent["id"]}, "run-owner")
    parent["children"] = [mine["id"]]
    with store.transaction() as db:
        store.save_node(db, parent)
    mine = hold(store, "input_conflict", mine)
    # A node created by this run, then detached/reparented by root, is no
    # longer readable through the run's subtree capability.
    outsider = hold(store, "needs_info", subject="run-owner")
    token = issue_run_capability(store.paths.root, "run-owner", parent["id"], {"read"})
    service = Service(store.paths.root, "now")
    result = service.wait({"token": token}, None, {"node_ids": [], "timeout": 0})
    assert [h["node_id"] for h in result["holds"]] == [mine["id"]]
    assert outsider["id"] not in json.dumps(result["holds"])
