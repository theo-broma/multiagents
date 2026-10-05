"""NC-R40 replay after confirmation but before publishing the stopped tree."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nc_fixture.m3_adv import Harness
from multiagents.scheduler import suspension
from multiagents.scheduler.engine import save_attempt


@pytest.mark.parametrize("tree_status", ["idle", "pending"])
def test_nc_r40_late_result_cannot_strand_a_confirmed_stop_on_restart(tmp_path, monkeypatch, tree_status):
    h = Harness(tmp_path, monkeypatch)
    try:
        node = h.record()
        h.save(node)
        attempt, run = h.launch(node["id"])
        runner = h.engine.runner
        runner.tree.update(run.id, status=tree_status, reason="window stopping", turn_started_at=10)
        attempt.update(window_stop=True, window_stop_started=True, turn_started_at=10)
        with h.service.store.transaction() as db:
            save_attempt(db, attempt)
        assert suspension.confirmed(h.service.store, run.id, runner.tree.get(run.id))
        directory = h.world.paths.run_dir(run.id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps({"status": "done", "turn_started_at": 10}))
        released = []

        async def dead(_):
            return True

        monkeypatch.setattr(runner, "_steer_predecessor", lambda _: SimpleNamespace(absent=False))
        monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
        monkeypatch.setattr(runner, "_release", released.append)
        current = h.journal()[attempt["attempt_id"]]
        assert asyncio.run(suspension.command(h.service.store, runner, current))
        current = h.journal()[attempt["attempt_id"]]
        assert current["state"] == "suspended" and not current.get("window_stop")
        assert not current.get("window_stop_started") and "window_completion" not in current
        assert h.nodes()[node["id"]]["generations"] == []
        assert runner.tree.get(run.id).status == "idle"
        assert runner.tree.get(run.id).reason == "window suspended"
        assert released == [run.id]
    finally:
        h.close()
