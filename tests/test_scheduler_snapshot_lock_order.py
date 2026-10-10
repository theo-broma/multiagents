"""SC-R3: snapshot cache locks never span SQLite operations."""
import sqlite3

import pytest

from multiagents.scheduler.store import Store, encode, records


@pytest.mark.parametrize("table", ["nodes", "attempts", "notifications"])
def test_snapshot_refresh_does_not_hold_cache_lock_during_sql(tmp_path, monkeypatch, table):
    store = Store(tmp_path)
    store.initialize()
    connect = sqlite3.connect

    def unlocked():
        assert not store.snapshot_lock._is_owned(), "SQLite called while snapshot_lock is held"

    class Cursor(sqlite3.Cursor):
        def execute(self, *args, **kwargs):
            unlocked()
            return super().execute(*args, **kwargs)

        def __next__(self):
            unlocked()
            return super().__next__()

        def fetchone(self):
            unlocked()
            return super().fetchone()

    class Connection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            return self.cursor(factory=Cursor).execute(*args, **kwargs)

        def commit(self):
            unlocked()
            return super().commit()

        def rollback(self):
            unlocked()
            return super().rollback()

        def close(self):
            unlocked()
            return super().close()

    def guarded_connect(*args, **kwargs):
        unlocked()
        return connect(*args, **kwargs, factory=Connection)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    key, id = ("seq", 1) if table == "notifications" else ("id", "record")
    with store.transaction() as db:
        db.execute(f"INSERT INTO {table} ({key}, record) VALUES (?, ?)",
                   (id, encode({"state": "done"})))
        assert records(db, table, lazy=True)[id]["state"] == "done"  # cold cache
    with store.transaction(write=False) as db:
        before = records(db, table, lazy=True)
        assert before[id]["state"] == "done"  # unchanged cache

    # A separate connection advances the trigger-maintained revision.
    with sqlite3.connect(store.file) as db:
        db.execute(f"UPDATE {table} SET record=? WHERE {key}=?",
                   (encode({"state": "open"}), id))
    with store.transaction(write=False) as db:
        assert records(db, table, lazy=True)[id]["state"] == "open"  # incremental refresh
    assert before[id]["state"] == "done"

    with pytest.raises(RuntimeError, match="abort"), store.transaction() as db:
        db.execute(f"UPDATE {table} SET record=? WHERE {key}=?",
                   (encode({"state": "held"}), id))
        assert records(db, table, lazy=True)[id]["state"] == "held"
        raise RuntimeError("abort")
    with store.transaction(write=False) as db:
        assert records(db, table, lazy=True)[id]["state"] == "open"
    with store.transaction() as db:
        db.execute(f"DELETE FROM {table} WHERE {key}=?", (id,))
        assert not records(db, table, lazy=True)
    with store.transaction(write=False) as db:
        assert not records(db, table, lazy=True)
