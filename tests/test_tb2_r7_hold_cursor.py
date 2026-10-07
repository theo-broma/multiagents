"""TB2-R7: a hold-only wake must not skip unread journal transitions."""
import asyncio
from types import SimpleNamespace

import pytest

from multiagents import server
from multiagents.scheduler.model import create_record
from multiagents.scheduler.rpc import Service
from multiagents.scheduler.store import root_capability


@pytest.mark.parametrize("hold_on_poll", [1, 2])
@pytest.mark.parametrize("native_finishes", [False, True])
def test_hold_wakeup_preserves_unread_node_transitions(tmp_path, monkeypatch,
                                                     hold_on_poll, native_finishes):
    service = Service(tmp_path, "now")
    service.store.initialize()
    monkeypatch.setattr(service, "configuration", lambda: SimpleNamespace(limits={}))
    request = {"token": root_capability(tmp_path)}
    node = create_record({"kind": "simple", "agent": "worker", "task": "work"}, "root")
    with service.store.transaction() as db:
        service.store.save_node(db, node)
        service.store.transition(db, "created", node["id"])
    cursor = 0
    polls = 0

    def rpc(op, args):
        nonlocal polls
        assert op == "wait_for_nodes"
        polls += 1
        if polls == hold_on_poll:
            node.update(state="held", hold={"reason": "run_failed", "detail": "retry"})
            with service.store.transaction() as db:
                service.store.save_node(db, node)
                service.store.transition(db, "held", node["id"], node["hold"])
        return service.wait(request, None, {**args, "timeout": 0})

    async def inline_thread(fn, *args):
        return fn(*args)

    async def native_wait(*args):
        if native_finishes:
            journal = service.wait(request, None, {"timeout": 0})
            return {**journal, "changed": journal["transitions"]}
        await asyncio.Future()

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(server, "_node_rpc", rpc)
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}))
    reply = asyncio.run(server._wait_with_holds(run, None, 1))
    assert reply["changed"][0]["node_id"] == node["id"]
    # A consumer advances only when a reply carries a journal cursor.
    cursor = reply.get("next_cursor", cursor)
    journal = service.wait(request, None, {"cursor": cursor, "timeout": 0})
    # Every transition is either in this reply or still unread, never both:
    # a skipped one is lost, a returned-and-unread one comes back twice.
    returned = [c for c in reply["changed"] if "kind" in c]
    unread = journal["transitions"]
    assert sorted(t["kind"] for t in returned + unread) == ["created", "held"]
    assert not [t for t in returned if t in unread]
    with service.store.transaction(write=False) as db:
        assert int(service.store.meta(db, "ack")) == 0


def test_native_node_wait_keeps_its_journal_cursor(monkeypatch):
    async def inline_thread(fn, *args):
        return fn(*args)

    async def native_wait(*args):
        return {"next_cursor": 4, "transitions": [{"kind": "done"}], "changed": []}

    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(server, "_node_rpc", lambda *_: {
        "next_cursor": 9, "transitions": [], "holds": []})
    monkeypatch.setattr(server, "_wait_for_agents", native_wait)
    run = SimpleNamespace(config=SimpleNamespace(project={"scheduler": {"enabled": True}}))
    reply = asyncio.run(server._wait_with_holds(run, None, 1))
    assert reply["next_cursor"] == 4
    assert reply["transitions"] == [{"kind": "done"}]
