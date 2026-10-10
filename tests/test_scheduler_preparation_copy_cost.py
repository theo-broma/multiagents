"""SC-R2/R3: speculative admission edits share untouched cached history."""
import copy

from test_scheduler_copy_cost import CopyMeter, seeded_store


def test_preparation_snapshot_copies_only_locally_accessed_records(tmp_path, monkeypatch):
    costs = {}
    for history in (50, 5000):
        store = seeded_store(tmp_path / str(history), history)
        with store.transaction(write=False) as db:
            original = store.nodes(db)
        original["live"]["runs"].append({"run_id": "prior"})
        with monkeypatch.context() as patch:
            meter = CopyMeter(patch)
            prepared = copy.deepcopy(original)
            assert prepared == original
            prepared["live"]["runs"].append({"run_id": "prepared"})
            assert original["live"]["runs"] == [{"run_id": "prior"}]
            assert set(meter.records) == {"live"}
            costs[history] = (meter.dicts, meter.entries, meter.branches, meter.iterated)
        with store.transaction(write=False) as db:
            assert store.nodes(db)["live"]["runs"] == []
    print(f"SC-R2 preparation dicts/entries/index branches/iterated: {costs}")
    for small, large in zip(costs[50], costs[5000]):
        assert large <= 2 * small, costs


def test_preparation_snapshot_preserves_additions_deletions_and_copy_memo(tmp_path):
    store = seeded_store(tmp_path, 5)
    with store.transaction(write=False) as db:
        original = store.nodes(db)
    del original["old-node-000000"]
    original["added"] = {"id": "added", "state": "open", "runs": []}
    original["added"]["view"] = original
    prepared = copy.deepcopy(original)
    assert "old-node-000000" not in prepared
    assert prepared["added"]["view"] is prepared
    prepared["added"]["runs"].append("prepared")
    prepared["old-node-000001"]["runs"].append("prepared")
    assert original["added"]["runs"] == []
    assert original["old-node-000001"]["runs"] == []
    del prepared["live"]
    assert "live" in original
