"""Completion replay retains the target recorded before sibling integration."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness


@pytest.mark.parametrize("after_move", [False, True])
def test_nc_r5_r61_completion_replay_keeps_its_target_when_a_sibling_advances(tmp_path, monkeypatch, after_move):
    h = Harness(tmp_path, monkeypatch)
    try:
        group, (work, sibling) = h.tree("group", 2)
        sequence = h.record(kind="sequence", children=[work["id"]], parent=group["id"])
        work["parent"] = sequence["id"]
        group["children"] = [sequence["id"], sibling["id"]]
        h.save(group, sequence, work)
        attempt, run = h.launch(work["id"])
        h.commit(run, {"work.txt": "sequence\n"})
        h.engine.finished(attempt, run)
        with h.service.store.transaction() as db:
            node = h.service.store.nodes(db)[sequence["id"]]
            h.engine.complete(db, node, "approved")
        intent = h.nodes()[sequence["id"]]["completion_pending"]
        if after_move:
            h.results.move(intent["ref"], intent["generation"]["commit"], "")
        attempt, run = h.launch(sibling["id"])
        h.commit(run, {"sibling.txt": "group sibling\n"})
        h.engine.finished(attempt, run)
        assert h.nodes()[group["id"]]["branch_tip"] != intent["generation"]["commit"]
        asyncio.run(h.engine.reconcile())
        asyncio.run(h.engine.reconcile())
        done = h.nodes()[sequence["id"]]
        assert done["state"] == "done" and done["outcome"] == "approved"
        assert "completion_pending" not in done
        assert done["generations"] == [intent["generation"]]
        assert h.results.tip(intent["ref"]) == intent["generation"]["commit"]
    finally:
        h.close()
