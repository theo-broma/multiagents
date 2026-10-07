"""TB2-R7 mirror failures do not block plan access or disappear silently."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from multiagents import gitops
from multiagents.scheduler.model import create_record
from multiagents.scheduler.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    node = create_record({"kind": "simple", "agent": "worker", "task": "work"}, "root")
    node.update(state="held", hold={"reason": "needs_info", "detail": "question"})
    with store.transaction() as db:
        store.save_node(db, node)
        store.transition(db, "held", node["id"], node["hold"])
    return store


def clear(store):
    with store.transaction() as db:
        node = next(iter(store.nodes(db).values()))
        node.update(state="open", hold=None, revision=node["revision"] + 1)
        store.save_node(db, node)
        store.transition(db, "relaunched", node["id"])


@pytest.mark.parametrize("seam", ["_write_beneath", "_open_file_beneath"])
def test_slow_mirror_io_leaves_database_available_and_latest_snapshot_wins(store, monkeypatch, seam):
    entered, release = threading.Event(), threading.Event()
    original = getattr(gitops, seam)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release mirror I/O"
        return original(*args, **kwargs)

    monkeypatch.setattr(gitops, seam, blocked)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(store.mirror)
        try:
            assert entered.wait(3)
            changed = pool.submit(clear, store)
            changed.result(timeout=2)
            # A mirror requested after the clear must not be overwritten by
            # the older snapshot when its delayed write finally finishes.
            latest = pool.submit(Store(store.paths.root).mirror)
        finally:
            release.set()
        first.result(timeout=5)
        latest.result(timeout=5)
    assert json.loads((store.paths.data / "attention.json").read_text())["holds"] == []


def test_attention_write_failure_is_reported_and_retry_repairs_committed_state(store, monkeypatch):
    store.mirror()
    path = store.paths.data / "attention.json"
    assert json.loads(path.read_text())["holds"]
    clear(store)

    def failed(*args, **kwargs):
        raise OSError("attention disk full")

    with monkeypatch.context() as patch:
        patch.setattr(gitops, "_write_beneath", failed)
        with pytest.raises(OSError, match="attention disk full"):
            store.mirror()
    with store.transaction(write=False) as db:
        assert next(iter(store.nodes(db).values()))["state"] == "open"
    store.mirror()
    assert json.loads(path.read_text())["holds"] == []
