"""RR-R3/R4: launch consumption survives supervision and shutdown gaps."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_nc_m2_guard_recovery import local, deposit, claim, read
from multiagents.scheduler.engine import confirm_recovery_launch, save_attempt
from multiagents.tree import Node


@pytest.mark.parametrize("kind", ["local", "docker"])
@pytest.mark.parametrize("evidence", ["wrapper_only", "wrong_executor", "invalid_pid", "launched"])
def test_rr_r3_consumption_requires_the_provider_launch_and_is_durable(local, kind, evidence):
    world, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    source = {"attempt": "source-attempt", "commit": "source-commit"}
    node["recovery"] = dict(source, input_commit="original-input", pending=True)
    attempt.update(recovery_source=source, launch_evidence={"executor": {"kind": kind}})
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
        save_attempt(db, attempt)
    run_dir = world.paths.run_dir(attempt["run_id"])
    run_dir.mkdir(parents=True)
    (run_dir / "wrapper.pid").write_text("123\n")
    correct = "agent.pid" if kind == "local" else "container.pid"
    wrong = "container.pid" if kind == "local" else "agent.pid"
    if evidence != "wrapper_only":
        (run_dir / (wrong if evidence == "wrong_executor" else correct)).write_text(
            "0\n" if evidence == "invalid_pid" else "456 namespace\n")
    before, _ = read(engine)
    before = copy.deepcopy(before[node["id"]])
    confirm_recovery_launch(engine.store, engine.paths, attempt)
    nodes, journal = read(engine)
    recovered = nodes[node["id"]]
    if evidence == "launched":
        assert recovered["recovery"]["pending"] is False
        assert recovered["recovery"]["consumed_by"] == {
            "attempt": attempt["attempt_id"], "run_id": attempt["run_id"]}
        assert recovered["recovery_history"] == [recovered["recovery"]]
        assert journal[attempt["attempt_id"]]["recovery_source"] == source
        assert journal[attempt["attempt_id"]]["recovery_consumed"] is True
        confirm_recovery_launch(engine.store, engine.paths, attempt)
        assert read(engine)[0][node["id"]] == recovered
    else:
        assert recovered == before
        assert not journal[attempt["attempt_id"]].get("recovery_consumed")


@pytest.mark.parametrize("state", ["claimed", "launched", "suspended"])
def test_rr_r4_quota_cleanup_cannot_stop_or_release_a_managed_launch(local, monkeypatch, state):
    _, engine = local
    node = deposit(engine)
    attempt = claim(engine, node, state=state)
    runner = engine.runner
    monkeypatch.setattr(runner, "_scheduler_supervisor", False)
    runner.tree.add(Node(id=attempt["run_id"], agent="worker", provider="fx", model="fx/m1",
                         parent=None, depth=1, status="pending", node_id=node["id"],
                         attempt_id=attempt["attempt_id"]))
    runner._reserve_launch(attempt["run_id"], "fx", "startup-token", runner.executor())
    runner.tree.update(attempt["run_id"], slot_owner={"token": "slot-token"})
    before = copy.deepcopy(runner.tree.read())
    before_attempt = copy.deepcopy(read(engine)[1][attempt["attempt_id"]])
    handle = SimpleNamespace(stop=AsyncMock())

    async def shutdown():
        waiting = asyncio.Event()

        async def promotion():
            try:
                waiting.set()
                await asyncio.Event().wait()
            finally:
                await runner._launch_cleanup_task(handle, attempt["run_id"])
                runner._end_hold(attempt["run_id"])
                runner._retire_hold(attempt["run_id"])
                runner._settle_holds()
                runner._end_slot_claim(attempt["run_id"], "slot-token")

        task = asyncio.create_task(promotion())
        runner._qh_promotions = {attempt["run_id"]: task}
        await waiting.wait()
        await runner.shutdown(detach=False)

    asyncio.run(shutdown())
    runner._stop_for_cap(SimpleNamespace(node_id=attempt["run_id"]), {})
    with pytest.raises(asyncio.CancelledError):
        runner._qh_check_switch(attempt["run_id"])
    handle.stop.assert_not_awaited()
    assert runner.tree.read() == before
    assert read(engine)[1][attempt["attempt_id"]] == before_attempt
