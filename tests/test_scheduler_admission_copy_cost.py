"""SC-R2/R3: admission preparation and launch copy only live records."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from nc_fixture.m3_adv import Harness
from multiagents.scheduler.engine import attempts, record_launch, save_attempt
from test_scheduler_copy_cost import CopyMeter


@pytest.mark.parametrize("operation", ["preparation", "record_launch", "admission_tick"])
def test_admission_copy_cost_is_bounded_by_live_work(tmp_path, monkeypatch, operation):
    costs = {}
    for history in (50, 5000):
        root = tmp_path / str(history)
        root.mkdir()
        with monkeypatch.context() as patch:
            h = Harness(root, patch)
            try:
                store = h.service.store
                with store.transaction() as db:
                    for i in range(history):
                        node = h.record()
                        node.update(id=f"old-node-{i:06}", state="done", outcome="completed")
                        store.save_node(db, node)
                        save_attempt(db, {"attempt_id": f"old-attempt-{i:06}",
                                          "node_id": node["id"], "run_id": f"old-run-{i:06}",
                                          "state": "recorded"})
                asyncio.run(h.engine.tick())
                live = h.record()
                h.save(live)
                attempt = {"attempt_id": "live-attempt", "node_id": live["id"],
                           "run_id": "live-run", "activation_id": "activation", "state": "claimed"}
                if operation == "record_launch":
                    with store.transaction() as db:
                        save_attempt(db, attempt)
                with store.transaction(write=False) as db:
                    store.nodes(db)
                    attempts(db)

                spawned = []

                async def admitted(*args, **kwargs):
                    return {"admitted": True}

                def spawn(claim):
                    spawned.append(claim)
                    with store.transaction() as db:
                        assert record_launch(store, db, claim, SimpleNamespace(id=claim["run_id"]))

                patch.setattr(h.engine.runner, "start", admitted)
                patch.setattr(h.engine, "spawn", spawn)
                with patch.context() as measured:
                    meter = CopyMeter(measured)
                    if operation == "preparation":
                        with store.transaction(write=False) as db:
                            original = store.node_records(db)
                        original[live["id"]]
                        prepared = copy.deepcopy(original)
                        result = h.results.prepare(prepared[live["id"]], prepared)
                        assert result["branch_node"] == live["id"]
                        assert prepared[live["id"]]["branch_tip"] == h.world.main_tip()
                        assert original[live["id"]].get("branch_tip") is None
                        assert prepared.rows is original.rows
                        assert prepared.indexes is original.indexes
                    elif operation == "record_launch":
                        with store.transaction() as db:
                            assert record_launch(store, db, attempt, SimpleNamespace(id=attempt["run_id"]))
                    else:
                        asyncio.run(h.engine.tick())
                        assert len(spawned) == 1
                    assert set(meter.records) <= {live["id"], "live-attempt",
                                                  *(a["attempt_id"] for a in spawned)}
                    costs[history] = (meter.dicts, meter.entries, meter.branches, meter.iterated)
                with store.transaction(write=False) as db:
                    current = store.nodes(db)[live["id"]]
                    assert current["state"] == ("open" if operation == "preparation" else "running")
                    if operation != "preparation":
                        assert len(current["runs"]) == 1
                        assert current["runs"][0]["run_id"] == (spawned[0]["run_id"] if spawned else "live-run")
            finally:
                h.close()
    print(f"SC-R2 {operation} dicts/entries/index branches/iterated: {costs}")
    for small, large in zip(costs[50], costs[5000]):
        assert large <= 2 * small, costs
