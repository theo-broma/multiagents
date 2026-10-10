"""SC-R1..R3: live reads and launches must not copy terminal history."""
import copy
import json
import random
import sqlite3
from types import SimpleNamespace

import pytest

from multiagents.scheduler import snapshots
from multiagents.scheduler.engine import attempts, record_launch, save_attempt
from multiagents.scheduler.store import RecordView, Store


def seeded_store(root, history):
    store = Store(root)
    store.initialize()
    with store.transaction() as db:
        for i in range(history):
            nid = f"old-node-{i:06}"
            store.save_node(db, {"id": nid, "state": "done", "runs": [], "parent": None})
            save_attempt(db, {"attempt_id": f"old-attempt-{i:06}", "node_id": nid,
                              "run_id": f"old-run-{i:06}", "state": "recorded"})
        store.save_node(db, {"id": "live", "state": "open", "revision": 1,
                             "runs": [], "parent": None})
        save_attempt(db, {"attempt_id": "live-attempt", "node_id": "live", "run_id": "live-run",
                          "activation_id": "activation", "state": "claimed"})
    with store.transaction(write=False) as db:
        store.nodes(db)
        attempts(db)
    return store


class CopyMeter:
    def __init__(self, monkeypatch):
        self.dicts = self.entries = self.branches = self.iterated = 0
        self.records = []
        real_dict = copy._deepcopy_dispatch[dict]
        real_branch = snapshots.branch
        real_iter = RecordView.__iter__

        def deepcopy_dict(value, memo, *args):
            self.dicts += 1
            self.entries += len(value)
            if "id" in value or "attempt_id" in value:
                self.records.append(value.get("id", value.get("attempt_id")))
            return real_dict(value, memo, *args)

        def branch(*args, **kwargs):
            self.branches += 1
            return real_branch(*args, **kwargs)

        def iter_records(view):
            for id in real_iter(view):
                self.iterated += 1
                yield id

        monkeypatch.setattr(RecordView, "__iter__", iter_records)
        monkeypatch.setitem(copy._deepcopy_dispatch, dict, deepcopy_dict)
        monkeypatch.setattr(snapshots, "branch", branch)


@pytest.mark.parametrize("operation", ["nodes", "attempts", "launch", "status"])
def test_live_copy_cost_is_bounded_by_live_work(tmp_path, monkeypatch, operation):
    costs = {}
    for history in (50, 5000):
        store = seeded_store(tmp_path / str(history), history)
        with monkeypatch.context() as patch:
            meter = CopyMeter(patch)
            with store.transaction() as db:
                if operation == "nodes":
                    store.nodes(db)
                elif operation == "attempts":
                    attempts(db)
                elif operation == "status":
                    assert store.nodes(db).state_counts(["open", "done"]) == {"open": 1, "done": history}
                else:
                    assert record_launch(store, db, {"attempt_id": "live-attempt", "node_id": "live",
                                                   "activation_id": "activation"}, SimpleNamespace(id="live-run"))
            costs[history] = (meter.dicts, meter.entries, meter.branches, meter.iterated)
            assert all(not id.startswith("old-") for id in meter.records)
    print(f"SC-R2 {operation} dicts/entries/index branches/iterated: {costs}")
    for small, large in zip(costs[50], costs[5000]):
        assert large <= 2 * small, costs


def test_incremental_indexes_preserve_earlier_views_and_other_connection_changes(tmp_path):
    store = seeded_store(tmp_path, 50)
    with store.transaction(write=False) as db:
        before = store.nodes(db)
        old_attempts = attempts(db)
    with sqlite3.connect(store.file) as db:
        node = {"id": "live", "state": "held", "runs": [{"run_id": "replacement"}],
                "window": {"timezone": "Pacific/Kiritimati"}}
        db.execute("UPDATE nodes SET record=? WHERE id='live'", (json.dumps(node),))
        db.execute("UPDATE attempts SET record=? WHERE id='live-attempt'", (json.dumps({
            "attempt_id": "live-attempt", "node_id": "live", "run_id": "replacement",
            "state": "suspended", "alias_id": "alias"}),))
    with store.transaction(write=False) as db:
        nodes, journal = store.nodes(db), attempts(db)
        assert [n["id"] for n in nodes.selected("held")] == ["live"]
        assert not list(nodes.selected("open"))
        assert set(nodes.indexes["timezones"]) == {"Pacific/Kiritimati"}
        assert set(nodes.indexes["last_runs"]["replacement"]) == {"live"}
        assert not list(journal.matching("run_id", "live-run"))
        assert [a["attempt_id"] for a in journal.matching("alias_id", "alias")] == ["live-attempt"]
        assert "replacement" in journal.run_ids and "live-run" not in journal.run_ids
    assert before["live"]["state"] == "open"
    assert old_attempts["live-attempt"]["state"] == "claimed"
    with sqlite3.connect(store.file) as db:
        db.execute("DELETE FROM nodes WHERE id='live'")
        db.execute("DELETE FROM attempts WHERE id='live-attempt'")
    with store.transaction(write=False) as db:
        assert not list(store.nodes(db).selected("held"))
        assert not store.nodes(db).indexes["timezones"]
        assert not store.nodes(db).indexes["last_runs"]
        assert not list(attempts(db).matching("alias_id", "alias"))
        assert "replacement" not in attempts(db).run_ids
    assert nodes["live"]["state"] == "held"
    assert journal["live-attempt"]["state"] == "suspended"


def test_snapshot_map_matches_dict_through_updates_deletes_and_preserved_versions():
    rng = random.Random(8291)
    mapping, expected, saved = snapshots.SnapshotMap(), {}, []
    for i in range(2000):
        key = rng.randrange(300)
        if rng.randrange(3):
            mapping, expected = mapping.set(key, i), {**expected, key: i}
        else:
            mapping = mapping.discard(key)
            expected.pop(key, None)
        assert dict(mapping) == expected
        assert len(mapping) == len(expected)
        if mapping:
            assert mapping.last() == (max(expected), expected[max(expected)])
        if i % 100 == 0:
            saved.append((mapping, expected.copy()))
    for old, values in saved:
        assert dict(old) == values
    assert mapping.get(None) is None


@pytest.mark.parametrize("operation", ["get_node", "scheduler_status"])
def test_rpc_live_reads_do_not_copy_or_iterate_terminal_history(tmp_path, monkeypatch, operation):
    from nc_fixture.m3_adv import Harness

    costs = {}
    for history in (50, 5000):
        root = tmp_path / str(history)
        root.mkdir()
        with monkeypatch.context() as patch:
            h = Harness(root, patch)
            try:
                store = h.service.store
                live = h.record()
                with store.transaction() as db:
                    for i in range(history):
                        node = h.record()
                        node.update(state="done", outcome="completed")
                        store.save_node(db, node)
                        save_attempt(db, {"attempt_id": f"old-{i}", "node_id": node["id"],
                                          "run_id": f"old-run-{i}", "state": "recorded"})
                    store.save_node(db, live)
                    token = store.meta(db, "root_token")
                with store.transaction(write=False) as db:
                    store.nodes(db)
                    attempts(db)
                with patch.context() as measured:
                    meter = CopyMeter(measured)
                    reply = h.service.request({"op": operation, "args": {"id": live["id"]}
                                               if operation == "get_node" else {},
                                               "token": token, "request_id": "read"})
                    assert reply["ok"], reply
                    assert set(meter.records) <= {live["id"]}
                    costs[history] = (meter.dicts, meter.entries, meter.branches, meter.iterated)
            finally:
                h.close()
    print(f"SC-R2 {operation} copy/index/iteration cost: {costs}")
    for small, large in zip(costs[50], costs[5000]):
        assert large <= 2 * small, costs


def test_idle_tick_python_copy_work_is_independent_of_history(tmp_path, monkeypatch):
    import asyncio
    from nc_fixture.m3_adv import Harness

    costs = {}
    for history in (50, 5000):
        root = tmp_path / str(history)
        root.mkdir()
        with monkeypatch.context() as patch:
            h = Harness(root, patch)
            try:
                with h.service.store.transaction() as db:
                    for i in range(history):
                        node = h.record()
                        node.update(state="done", outcome="completed")
                        h.service.store.save_node(db, node)
                        save_attempt(db, {"attempt_id": f"old-{i}", "node_id": node["id"],
                                          "run_id": f"old-run-{i}", "state": "recorded"})
                asyncio.run(h.engine.tick())
                with patch.context() as measured:
                    meter = CopyMeter(measured)
                    asyncio.run(h.engine.tick())
                    assert not meter.records
                    costs[history] = (meter.dicts, meter.entries, meter.branches, meter.iterated)
            finally:
                h.close()
    print(f"SC-R2 idle tick copy/index/iteration cost: {costs}")
    for small, large in zip(costs[50], costs[5000]):
        assert large <= 2 * small, costs


def test_notification_latest_and_shared_run_indexes_handle_deletions(tmp_path):
    from multiagents.scheduler.store import records

    store = seeded_store(tmp_path, 5)
    with store.transaction() as db:
        first = store.transition(db, "held", "live")
        last = store.transition(db, "held", "live")
        save_attempt(db, {"attempt_id": "another", "node_id": "live", "run_id": "live-run",
                          "state": "claimed"})
    with store.transaction(write=False) as db:
        old = records(db, "notifications", lazy=True)
        assert old.indexes["latest"]["live"] == (last["seq"], last["at"])
        assert attempts(db).run_ids == {"live-run", *(f"old-run-{i:06}" for i in range(5))}
    with store.transaction() as db:
        db.execute("DELETE FROM notifications WHERE seq=?", (last["seq"],))
        db.execute("DELETE FROM attempts WHERE id='live-attempt'")
    with store.transaction(write=False) as db:
        assert records(db, "notifications", lazy=True).indexes["latest"]["live"] == (first["seq"], first["at"])
        assert "live-run" in attempts(db).run_ids
    with store.transaction() as db:
        db.execute("DELETE FROM notifications WHERE seq=?", (first["seq"],))
        db.execute("DELETE FROM attempts WHERE id='another'")
    with store.transaction(write=False) as db:
        assert not records(db, "notifications", lazy=True).indexes["latest"]
        assert "live-run" not in attempts(db).run_ids
    assert old.indexes["latest"]["live"] == (last["seq"], last["at"])


def test_record_view_comparison_does_not_copy_or_enumerate_history(tmp_path, monkeypatch):
    store = seeded_store(tmp_path, 5000)
    with store.transaction(write=False) as db:
        old = store.nodes(db)
    with store.transaction() as db:
        current = store.nodes(db)["live"]
        current["state"] = "held"
        store.save_node(db, current)
    with store.transaction(write=False) as db:
        new = store.nodes(db)
    with monkeypatch.context() as patch:
        meter = CopyMeter(patch)
        assert old != new
        new["live"]["state"] = "open"
        assert old == new
        assert set(meter.records) == {"live"}
        assert meter.iterated == 0
    with store.transaction() as db:
        db.execute("DELETE FROM nodes WHERE id='live'")
        store.save_node(db, {"id": "live", "state": "open", "revision": 1,
                             "runs": [], "parent": None})
    with store.transaction(write=False) as db:
        assert store.nodes(db) == old  # dict equality ignores creation order


def test_snapshot_map_equivalence_matches_dict_with_rotations_and_ignored_keys():
    assert not snapshots.SnapshotMap().set("node", {}).equivalent(snapshots.SnapshotMap().set(1, {}))
    rng = random.Random(5721)
    for _ in range(200):
        keys = list(range(rng.randrange(1, 100)))
        rng.shuffle(keys)
        left = snapshots.SnapshotMap()
        for key in keys:
            left = left.set(key, str(key))
        rng.shuffle(keys)
        right = snapshots.SnapshotMap()
        for key in keys:
            right = right.set(key, str(key))
        assert left.equivalent(right)
        key = rng.choice(keys)
        assert not left.equivalent(right.set(key, "changed"))
        assert left.equivalent(right.set(key, "changed"), ignore={key})
        assert left.equivalent(right.discard(key), ignore={key})
        assert not left.equivalent(right.discard(key))


def test_record_view_repr_shows_snapshot_contents_without_copying_records(tmp_path, monkeypatch):
    import ast

    store = seeded_store(tmp_path, 50)
    with store.transaction(write=False) as db:
        view = store.nodes(db)
    with monkeypatch.context() as patch:
        meter = CopyMeter(patch)
        rendered = repr(view)
        assert ast.literal_eval(rendered) == view
        assert meter.dicts == 0
    with store.transaction(write=False) as db:
        assert repr(store.nodes(db)) == rendered
    with store.transaction() as db:
        current = store.nodes(db)["live"]
        current["state"] = "held"
        store.save_node(db, current)
    with store.transaction(write=False) as db:
        assert repr(store.nodes(db)) != rendered
    assert repr(view) == rendered
