"""QH-R15/R16: a veto wins over any floor request that is not yet running."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World, TASK, reserve_tool, wait_until
from multiagents.budget import Budget
from multiagents.runner import ProviderFull, SpendCapRefused


def floor(tmp_path, monkeypatch, gated=False):
    w = World(tmp_path, monkeypatch, reserved="reserve", models={"reserve": "model-a"})
    w.unusable("alpha", "beta", "other")
    w.readings["reserve"] = Budget("reserve", known=True, headroom=.2)
    gate = tmp_path / "release-floor"
    if gated:
        w.set_plan("reserve", gate=str(gate))
    return w, gate


def refuse_once(w, monkeypatch, race, during=None):
    """Make the next floor admission fail late, once; `during` runs first."""
    refused = []
    original_add, original_launch = w.r._admission_add, w.r._launch

    def add(spec, node, queued_id=""):
        if race == "concurrency" and not refused:
            refused.append(node.id)
            raise ProviderFull("reserve", 1, ["competing-run"])
        return original_add(spec, node, queued_id)

    async def launch(**kwargs):
        if not refused and during is not None:
            await during(kwargs["node_id"])
        if race == "spend_cap" and not refused:
            refused.append(kwargs["node_id"])
            raise SpendCapRefused({"reason": "spend_cap: test launch race", "until": w.clock[0] + 60})
        return await original_launch(**kwargs)

    monkeypatch.setattr(w.r, "_admission_add", add)
    monkeypatch.setattr(w.r, "_launch", launch)
    return refused


async def requeued(w, monkeypatch, race):
    await w.r.start("worker", TASK)
    request_id = w.events("reserve_request")[0]["request_id"]
    await reserve_tool("allow_reserve", request_id)
    refused = refuse_once(w, monkeypatch, race)
    await w.r.resume_deferred()
    assert refused and not w.calls()
    [request] = w.r.tree.read()["quota_reserve"]
    assert request["state"] == "allowed"
    return request_id


async def stop_all(w):
    for node in w.r._qh_nodes():
        await w.r.stop(node.id)


@pytest.mark.parametrize("race", ["concurrency", "spend_cap"])
def test_qh_r16_veto_after_requeue_wins_and_defers_until_reset(tmp_path, monkeypatch, race):
    w, _ = floor(tmp_path, monkeypatch)

    async def scenario():
        request_id = await requeued(w, monkeypatch, race)
        result = await reserve_tool("veto_reserve", request_id, "protect orchestrator quota")
        assert "error" not in result, result
        [request] = w.r.tree.read()["quota_reserve"]
        assert request["state"] == "vetoed"
        assert [e["request_id"] for e in w.events("reserve_vetoed")] == [request_id]
        # Quota has not reset: the vetoed request neither runs nor is proposed.
        for _ in range(2):
            await w.r.resume_deferred()
        await asyncio.sleep(.1)
        assert not w.calls()
        assert len(w.events("reserve_request")) == 1
        # A second answer to the same request is refused.
        again = await reserve_tool("allow_reserve", request_id)
        assert "error" in again, again
        await w.r.resume_deferred()
        assert not w.calls()
        w.reset("alpha")
        await w.r.resume_deferred()
        await wait_until(lambda: bool(w.calls()))
        assert [c["instance"] for c in w.calls()] == ["alpha"]
        await stop_all(w)

    asyncio.run(scenario())


def test_qh_r16_veto_while_running_on_floor_is_refused(tmp_path, monkeypatch):
    w, gate = floor(tmp_path, monkeypatch, gated=True)

    async def scenario():
        try:
            await w.r.start("worker", TASK)
            request_id = w.events("reserve_request")[0]["request_id"]
            await reserve_tool("allow_reserve", request_id)
            await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            result = await reserve_tool("veto_reserve", request_id, "too late")
            assert "error" in result and "running" in result["error"], result
            [request] = w.r.tree.read()["quota_reserve"]
            assert request["state"] == "running"
            assert not w.events("reserve_vetoed")
            node = w.r.tree.get(request["dispatched_id"])
            assert node.provider == "reserve" and node.status == "running"
            gate.touch()
            await w.settle(node.id)
            assert w.r.tree.get(node.id).status == "done"
            await w.r.resume_deferred()
            assert len(w.calls()) == 1
        finally:
            gate.touch()
            await stop_all(w)

    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["spend_cap", "none"])
def test_qh_r16_veto_during_dispatch_applies_only_if_dispatch_fails(tmp_path, monkeypatch, race):
    w, gate = floor(tmp_path, monkeypatch, gated=True)
    answers = []

    async def scenario():
        try:
            await w.r.start("worker", TASK)
            request_id = w.events("reserve_request")[0]["request_id"]
            await reserve_tool("allow_reserve", request_id)

            async def veto(_node_id):
                answers.append(await reserve_tool("veto_reserve", request_id, "changed my mind"))

            refuse_once(w, monkeypatch, race, during=veto)
            await w.r.resume_deferred()
            [answer] = answers
            assert "error" in answer and "dispatch" in answer["error"], answer
            [request] = w.r.tree.read()["quota_reserve"]
            if race == "none":
                # The dispatch started: the run proceeds, as the error said.
                await wait_until(lambda: bool(w.calls()))
                assert request["state"] == "running" and request.get("started")
                assert not w.events("reserve_vetoed")
                gate.touch()
                await w.settle(request["dispatched_id"])
            else:
                assert request["state"] == "vetoed"
                assert [e["request_id"] for e in w.events("reserve_vetoed")] == [request_id]
                await w.r.resume_deferred()
                await asyncio.sleep(.1)
                assert not w.calls()
        finally:
            gate.touch()
            await stop_all(w)

    asyncio.run(scenario())


def test_qh_r16_preserved_approval_dispatches_once_and_is_not_reusable(tmp_path, monkeypatch):
    w, gate = floor(tmp_path, monkeypatch, gated=True)

    async def scenario():
        try:
            request_id = await requeued(w, monkeypatch, "concurrency")
            assert "error" in await reserve_tool("allow_reserve", request_id)
            for _ in range(3):
                await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            await asyncio.sleep(.1)
            assert len(w.calls()) == 1
            assert "error" in await reserve_tool("allow_reserve", request_id)
            [request] = w.r.tree.read()["quota_reserve"]
            # Both grants (refused attempt, dispatched run) are consumed.
            assert not w.r.__dict__.get("_qh_floor_grants")
            with pytest.raises(RuntimeError):
                await w.r.start("worker", TASK, _qh_floor_grant="forged")
            gate.touch()
            await w.settle(request["dispatched_id"])
            await w.r.resume_deferred()
            assert len(w.calls()) == 1
            assert w.r.tree.read()["quota_reserve"][0]["state"] == "finished"
        finally:
            gate.touch()
            await stop_all(w)

    asyncio.run(scenario())


def test_qh_r15_requeued_approval_holds_the_floor_queue_until_vetoed(tmp_path, monkeypatch):
    w, gate = floor(tmp_path, monkeypatch, gated=True)

    async def scenario():
        try:
            first = await requeued(w, monkeypatch, "spend_cap")
            await w.r.start("worker", "second floor task")
            # A re-queued approval still owns the floor queue: one at a time.
            assert len(w.events("reserve_request")) == 1
            await reserve_tool("veto_reserve", first, "not this one")
            await w.r.resume_deferred()
            requests = w.events("reserve_request")
            assert len(requests) == 2 and "second floor task" in requests[1]["task"]
            await reserve_tool("allow_reserve", requests[1]["request_id"])
            await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            await asyncio.sleep(.1)
            assert len(w.calls()) == 1 and "second floor task" in w.calls()[0]["prompt"]
        finally:
            gate.touch()
            await stop_all(w)

    asyncio.run(scenario())
