"""NT review follow-ups (opus review of the merged NT sender), three defects:

1. **the sender stamps the store file after it reads it** — a pause cleared by
   another process (`notify test`) between the read and the stamp is absorbed:
   the sender remembers "paused" next to a stamp that already includes the
   clear, and never looks again. Contract: NT-R4 (a pause lasts only until the
   config changes or `notify test` succeeds), NT-R8 (its success clears the
   pause), NT-R3 (a message that is due is sent). The contract does not say
   how the sender notices a clear, so the tests hit the window through the
   store's transaction seam and look only at what the server receives.
2. **the hold-mark lookup swallows failure and does not chunk its ids** — with
   more held nodes than SQLite binds in one statement the lookup fails and is
   read as "nothing held". Contract: NT-R3 (one notification per hold; a
   notification for an event still true is not dropped), NT-R5 (the grouped
   message lists the first events and "+N more"). The contract is SILENT on
   the number of nodes it must be handled for, and on failure handling of the
   lookup itself; the tests pin only the two observable consequences that NT-R3
   already requires.
3. **cancelling a composite re-holds a child that is already held** — NT-R3 says
   one notification per hold; whether a cancel-induced re-hold of a held node
   is "a later hold, a new event" is NOT spelled out (NC-R80 / NC-R26 say what
   becomes of the node, not what is announced). The test asserts the reading
   the review reported as a defect.

Everything runs in this process over the real store, service and engine (the
`store_world` of the round-3 suite) and a fake ntfy server.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid

import pytest

from test_nt_part2_events import fake  # noqa: F401
from test_nt_part2_round3 import (  # noqa: F401
    accepted, deposit, outbox, scan, store_world, tree_of)

from multiagents import notify_sender
from multiagents.scheduler.engine import save_attempt
from multiagents.scheduler.model import create_record
from multiagents.scheduler.store import root_capability
from multiagents.tree import now

ARRIVE = 4.0            # a message that must be sent arrives within this many real seconds
SQL_VARIABLES = 999     # the bound-parameter limit of a stock SQLite before 3.32
MANY_HELD = 3000        # more held nodes than one statement can bind ids for


@pytest.fixture
def small_sql_limit(monkeypatch):
    """Every store connection from here on binds at most SQL_VARIABLES
    parameters per statement. This build allows 250000, others 32766 or 999;
    the cap makes 'more nodes than the limit' cheap and the same everywhere."""
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, SQL_VARIABLES)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)


def point_at(world, fake):
    world.project["notify"]["ntfy_url"] = fake.url
    world.write_config()


def pending_done(engine, ref="nd-aaaaaaaa"):
    with engine.store.transaction() as db:
        db.execute("INSERT INTO notify_outbox(kind, ref, data, enqueued_at) VALUES (?, ?, ?, ?)",
                   ("done", ref, json.dumps({"outcome": "done"}), time.time()))


def pump_until(sender, fake, count, timeout=ARRIVE):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sender.pump()
        if len(fake.requests) >= count:
            return True
        time.sleep(0.05)
    return len(fake.requests) >= count


def fire_after(monkeypatch, store, when, action):
    """Run `action` (once) right after the first store transaction for which
    `when(write)` is true has closed — i.e. a write by another process landing
    in the sender's window between one store access and the next."""
    real = store.transaction
    state = {"fired": False}

    @contextlib.contextmanager
    def wrapper(*args, **kwargs):
        write = kwargs.get("write", args[0] if args else True)
        with real(*args, **kwargs) as db:
            yield db
        if not state["fired"] and when(write):
            state["fired"] = True
            action()

    monkeypatch.setattr(store, "transaction", wrapper)
    return state


# ---------------------------------------------------------------------------
# 1. a state change landing between the sender's read and its stamp is not lost
# ---------------------------------------------------------------------------

def test_nt_r4_a_pause_cleared_between_the_sender_read_and_its_stamp_is_not_lost(
        store_world, fake, monkeypatch):
    world, service, engine = store_world
    point_at(world, fake)
    pending_done(engine)
    with engine.store.transaction() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('notify_paused', '1')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('notify_failure', 'HTTP 401')")
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  time.time)
    # `notify test` succeeds in another process right after the sender has read
    # the paused state.
    landed = fire_after(monkeypatch, engine.store, lambda write: not write,
                        lambda: notify_sender.clear_pause(engine.store))
    assert pump_until(sender, fake, 1), \
        "the pause was cleared (NT-R8) during the sender's read, and the pending message never went out"
    assert landed["fired"], "the control proves nothing: the window was never hit"


def test_nt_r4_a_pause_cleared_between_the_failed_send_and_its_stamp_is_not_lost(
        store_world, fake, monkeypatch):
    world, service, engine = store_world
    point_at(world, fake)
    fake.mode, fake.status = "status", 401
    pending_done(engine)
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  time.time)
    # `notify test` succeeds in another process right after the sender has
    # recorded the 401 pause.
    landed = fire_after(monkeypatch, engine.store,
                        lambda write: write and len(fake.requests) >= 1,
                        lambda: (setattr(fake, "mode", "accept"),
                                 notify_sender.clear_pause(engine.store)))
    assert pump_until(sender, fake, 1), "the first send never reached the server"
    assert pump_until(sender, fake, 2), \
        "the pause was cleared (NT-R8) right after the sender recorded it, and the pending message stayed queued"
    assert landed["fired"], "the control proves nothing: the window was never hit"


def test_nt_r4_control_a_pause_cleared_after_the_sender_has_settled_is_noticed(
        store_world, fake):
    """The ordinary path: not a race, and green before and after any fix."""
    world, service, engine = store_world
    point_at(world, fake)
    fake.mode, fake.status = "status", 401
    pending_done(engine)
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  time.time)
    assert pump_until(sender, fake, 1)
    for _ in range(3):
        sender.pump()
    assert len(fake.requests) == 1, "a paused sender kept sending"
    fake.mode = "accept"
    notify_sender.clear_pause(engine.store)
    assert pump_until(sender, fake, 2), "a pause cleared at rest was not noticed"


# ---------------------------------------------------------------------------
# 2. many held nodes: the hold lookup neither fails nor reads failure as "none"
# ---------------------------------------------------------------------------

def hold_many(engine, count):
    """`count` nodes held `input_conflict`, each with its `held` transition,
    written the way the engine writes a hold, in one transaction."""
    ids = []
    with engine.store.transaction() as db:
        for _ in range(count):
            node = create_record({"kind": "simple", "agent": "worker", "task": "work"}, "root")
            node.update(state="held", hold={"reason": "input_conflict", "detail": ""})
            engine.store.save_node(db, node)
            engine.store.transition(db, "input_conflict", node["id"], node["hold"])
            engine.store.transition(db, "held", node["id"], node["hold"])
            ids.append(node["id"])
    return ids


def held_rows(store):
    return [row for row in outbox(store) if row["kind"] == "held"]


def test_nt_r3_a_hold_of_very_many_nodes_is_announced_once_each_and_editing_one_announces_nothing(
        store_world, small_sql_limit):
    world, service, engine = store_world
    scan(world, service, engine)                       # activation summary
    accepted(engine.store)
    ids = hold_many(engine, MANY_HELD)
    scan(world, service, engine)
    assert sorted(row["ref"] for row in held_rows(engine.store)) == sorted(ids), \
        "not every hold was announced exactly once"
    accepted(engine.store)
    # `update_node` on a node that stays held bumps its revision, writes no new hold.
    with engine.store.transaction() as db:
        nodes = engine.store.nodes(db)
        edited = nodes[ids[len(ids) // 2]]
        edited.update(task="an edited task", revision=edited["revision"] + 1)
        engine.store.save_node(db, edited)
    scan(world, service, engine)
    assert held_rows(engine.store) == [], \
        "editing one of many held nodes re-announced a hold that was already announced (NT-R3)"


def test_nt_r3_pending_holds_of_very_many_nodes_are_not_dropped_as_stale(
        store_world, fake, small_sql_limit):
    """The sender must see that each of the many queued holds is still true. A
    failed lookup read as 'nothing held' would drop them all, silently."""
    world, service, engine = store_world
    point_at(world, fake)
    ids = hold_many(engine, MANY_HELD)
    with engine.store.transaction() as db:
        seqs = {json.loads(rec)["node_id"]: seq for seq, rec in db.execute(
            "SELECT seq, record FROM notifications") if json.loads(rec).get("kind") == "held"}
        for nid in ids:
            db.execute("INSERT INTO notify_outbox(kind, ref, data, enqueued_at) VALUES (?, ?, ?, ?)",
                       ("held", nid, json.dumps({"reason": "input_conflict",
                                                "held_seq": seqs[nid], "rev": 1}), time.time()))
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  time.time)
    assert pump_until(sender, fake, 1, timeout=ARRIVE), \
        "many holds that are all still true produced no message (dropped as stale?)"
    body = fake.requests[0].text()
    assert "+%d more" % (MANY_HELD - 10) in body, body[-200:]
    assert outbox(engine.store) == [], "the sent holds stayed queued"


# ---------------------------------------------------------------------------
# 3. cancelling a composite does not announce a child that is already held again
# ---------------------------------------------------------------------------

def test_nt_r3_cancelling_a_sequence_does_not_announce_an_already_held_child_again(store_world):
    world, service, engine = store_world
    scan(world, service, engine)
    accepted(engine.store)
    child = deposit(engine, state="open")
    parent = create_record({"kind": "sequence", "children": [child["id"]]}, "root")
    with engine.store.transaction() as db:
        engine.store.save_node(db, parent)
    with engine.store.transaction() as db:
        nodes = engine.store.nodes(db)
        nodes[child["id"]]["parent"] = parent["id"]
        engine.store.save_node(db, nodes[child["id"]])
        engine.hold(db, nodes[child["id"]], "input_conflict", "two inputs")
        # A launch that is still being handed over: the cancel cannot confirm
        # absence, so it takes the interim `termination_unconfirmed` branch.
        save_attempt(db, {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
                          "node_id": child["id"], "run_id": "ag-" + uuid.uuid4().hex[:6],
                          "state": "claimed", "launch_in_progress": True, "locks": [],
                          "retry_count": 1, "at": now()})
    scan(world, service, engine)
    assert [r["ref"] for r in held_rows(engine.store)] == [child["id"]], "the control hold went unannounced"
    accepted(engine.store)

    def holds():
        with engine.store.transaction(write=False) as db:
            return [json.loads(raw) for (raw,) in db.execute("SELECT record FROM notifications")
                    if json.loads(raw).get("node_id") == child["id"]
                    and json.loads(raw).get("kind") == "held"]

    before = len(holds())
    with engine.store.transaction(write=False) as db:
        revision = engine.store.nodes(db)[parent["id"]]["revision"]
    reply = service.request({"op": "cancel_node", "request_id": uuid.uuid4().hex,
                             "token": root_capability(world.root),
                             "args": {"id": parent["id"], "revision": revision}})
    assert reply.get("ok"), reply
    assert len(holds()) == before, \
        "cancel wrote a second `held` transition for a child that was already held"
    scan(world, service, engine)
    assert held_rows(engine.store) == [], \
        "cancel made the scan announce the already-held child a second time"
