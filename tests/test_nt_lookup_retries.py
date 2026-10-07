"""NT-R3/R4: small SQLite limits and transient hold lookup failures."""
from __future__ import annotations

import sqlite3
import time

from multiagents import notify_sender

from test_nt_part2_events import fake  # noqa: F401
from test_nt_part2_round3 import (  # noqa: F401
    accepted, outbox, scan, store_world, tree_of)
from test_nt_review_followups import held_rows, hold_many, point_at, pump_until


def limit_connections(monkeypatch, *, fail_query=None):
    real = sqlite3.connect
    state = {"queries": 0, "failed": False}

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if "MAX(seq)" in sql and "notifications" in sql:
                state["queries"] += 1
                if state["queries"] == fail_query:
                    state["failed"] = True
                    raise sqlite3.OperationalError("hold lookup temporarily unavailable")
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", Connection)
        db = real(*args, **kwargs)
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 7)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    return state


def marks(store):
    with store.transaction(write=False) as db:
        return dict(db.execute("SELECT key, value FROM meta WHERE key='notify_seq'"
                               " OR key LIKE 'notify_held:%'"))


def test_nt_r3_holds_send_once_with_a_limit_below_the_normal_batch_size(
        store_world, fake, monkeypatch):
    world, service, engine = store_world
    point_at(world, fake)
    limit_connections(monkeypatch)
    scan(world, service, engine)
    accepted(engine.store)
    ids = hold_many(engine, 30)
    scan(world, service, engine)
    assert sorted(row["ref"] for row in held_rows(engine.store)) == sorted(ids)
    assert all(row["data"]["held_seq"] is not None for row in held_rows(engine.store))

    instant = time.time() + world.project["notify"]["min_interval_seconds"]
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  lambda: instant)
    assert pump_until(sender, fake, 1)
    assert "+20 more" in fake.requests[0].text()
    assert outbox(engine.store) == []

    with engine.store.transaction() as db:
        edited = engine.store.nodes(db)[ids[0]]
        edited.update(task="edited task", revision=edited["revision"] + 1)
        engine.store.save_node(db, edited)
    scan(world, service, engine)
    assert outbox(engine.store) == []


def test_nt_r4_a_failed_scan_lookup_preserves_marks_and_retries_later(
        store_world, monkeypatch):
    world, service, engine = store_world
    scan(world, service, engine)
    accepted(engine.store)
    ids = hold_many(engine, 20)
    scan(world, service, engine)
    accepted(engine.store)
    with engine.store.transaction() as db:
        edited = engine.store.nodes(db)[ids[0]]
        edited.update(task="edited task", revision=edited["revision"] + 1)
        engine.store.save_node(db, edited)
    new_ids = hold_many(engine, 1)
    before = marks(engine.store)
    fault = limit_connections(monkeypatch, fail_query=2)

    scan(world, service, engine)
    assert fault["failed"]
    assert outbox(engine.store) == []
    assert marks(engine.store) == before

    scan(world, service, engine)
    assert [row["ref"] for row in held_rows(engine.store)] == new_ids


def test_nt_r4_a_failed_sender_lookup_keeps_pending_holds_for_retry(
        store_world, fake, monkeypatch):
    world, service, engine = store_world
    point_at(world, fake)
    scan(world, service, engine)
    accepted(engine.store)
    ids = hold_many(engine, 20)
    scan(world, service, engine)
    before = outbox(engine.store)
    fault = limit_connections(monkeypatch, fail_query=2)
    instant = time.time() + world.project["notify"]["min_interval_seconds"]
    sender = notify_sender.Sender(service, engine.store, tree_of(world), world.root.name,
                                  lambda: instant)

    sender.pump()
    assert fault["failed"]
    assert outbox(engine.store) == before
    assert fake.requests == []

    assert pump_until(sender, fake, 1)
    assert "+%d more" % (len(ids) - 10) in fake.requests[0].text()
    assert outbox(engine.store) == []
