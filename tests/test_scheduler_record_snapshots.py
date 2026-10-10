"""SC-R2/R3: snapshots track committed SQL writes without leaking mutations."""
import json
import sqlite3

import pytest

from multiagents.scheduler.store import Store


def test_record_snapshot_tracks_other_connections_and_deletion(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    with store.transaction() as db:
        store.save_node(db, {"id": "first", "state": "done", "runs": []})
    with store.transaction(write=False) as db:
        nodes = store.nodes(db)
        nodes["first"]["runs"].append("not saved")
    with sqlite3.connect(store.file) as db:
        db.execute("UPDATE nodes SET record=? WHERE id=?",
                   (json.dumps({"id": "first", "state": "open", "runs": []}), "first"))
    with store.transaction(write=False) as db:
        assert store.nodes(db)["first"] == {"id": "first", "state": "open", "runs": []}
    with sqlite3.connect(store.file) as db:
        db.execute("DELETE FROM nodes WHERE id='first'")
    with store.transaction(write=False) as db:
        assert store.nodes(db) == {}


def test_rolled_back_snapshots_are_never_published(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    with store.transaction() as db:
        store.save_node(db, {"id": "first", "state": "done"})
    with store.transaction(write=False) as db:
        assert store.nodes(db)["first"]["state"] == "done"
    with pytest.raises(RuntimeError), store.transaction() as db:
        store.save_node(db, {"id": "first", "state": "open"})
        assert store.nodes(db)["first"]["state"] == "open"
        raise RuntimeError("abort")
    with store.transaction() as db:
        store.save_node(db, {"id": "first", "state": "cancelled"})
    with store.transaction(write=False) as db:
        assert store.nodes(db)["first"]["state"] == "cancelled"


def test_existing_database_is_upgraded_automatically(tmp_path):
    store = Store(tmp_path)
    store.directory.mkdir(parents=True)
    with sqlite3.connect(store.file) as db:
        db.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        db.execute("INSERT INTO nodes VALUES ('first', ?)",
                   (json.dumps({"id": "first", "state": "done"}),))
    store.initialize()
    with store.transaction(write=False) as db:
        assert store.nodes(db)["first"]["state"] == "done"
    with sqlite3.connect(store.file) as db:
        db.execute("UPDATE nodes SET record=?", (json.dumps({"id": "first", "state": "open"}),))
    with store.transaction(write=False) as db:
        assert store.nodes(db)["first"]["state"] == "open"


def test_lazy_snapshot_copies_only_selected_live_records(tmp_path, monkeypatch):
    import multiagents.scheduler.store as store_module

    store = Store(tmp_path)
    store.initialize()
    with store.transaction() as db:
        for i in range(100):
            store.save_node(db, {"id": str(i), "state": "done", "runs": []})
        store.save_node(db, {"id": "live", "state": "held", "runs": []})
    with store.transaction(write=False) as db:
        store.node_records(db)  # warm the shared snapshot
    copied = []
    real_copy = store_module.copy.deepcopy

    def deepcopy(value, *args, **kwargs):
        if isinstance(value, dict) and "id" in value:
            copied.append(value["id"])
        return real_copy(value, *args, **kwargs)

    monkeypatch.setattr(store_module.copy, "deepcopy", deepcopy)
    with store.transaction(write=False) as db:
        view = store.node_records(db)
        held = list(view.selected("held"))
        assert [n["id"] for n in held] == ["live"]
        held[0]["runs"].append("unsaved")
        assert copied == ["live"]
    with store.transaction(write=False) as db:
        assert store.node_records(db)["live"]["runs"] == []


def test_terminal_window_can_be_reactivated_during_adoption(tmp_path, monkeypatch):
    import asyncio
    from nc_fixture.m3_adv import Harness
    from multiagents.scheduler import windows

    h = Harness(tmp_path, monkeypatch)
    try:
        zone = "Pacific/Kiritimati"
        node = h.record(window={"timezone": zone, "days": list(windows.DAYS),
                                "ranges": ["00:00-23:59"]})
        node.update(state="done", outcome="completed")
        h.save(node)
        monkeypatch.delitem(windows.ZONES, zone, raising=False)
        windows.evaluate.cache_clear()

        async def adopt(**kwargs):
            with h.service.store.transaction() as db:
                current = h.service.store.nodes(db)[node["id"]]
                current.update(state="open", outcome=None)
                h.service.store.save_node(db, current)
            return []

        async def refuse(*args, **kwargs):
            return {"admitted": False, "blocked": []}

        monkeypatch.setattr(h.engine.runner, "adopt", adopt)
        monkeypatch.setattr(h.engine.runner, "start", refuse)
        asyncio.run(h.engine.tick())
        assert h.nodes()[node["id"]]["state"] == "open"
    finally:
        h.close()


def test_legacy_run_marks_do_not_require_a_run_id(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    with store.transaction() as db:
        store.save_node(db, {"id": "legacy", "state": "open", "runs": [{"id": "prior-activation"}]})
    with store.transaction(write=False) as db:
        assert store.nodes(db)["legacy"]["runs"] == [{"id": "prior-activation"}]
