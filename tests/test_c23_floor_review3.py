"""QH-R14/R16: floor approval survives late admission refusals."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World, TASK, reserve_tool, wait_until
from multiagents.budget import Budget
from multiagents.runner import ProviderFull, SpendCapRefused


@pytest.mark.parametrize("race", ["none", "concurrency", "spend_cap"])
def test_qh_r16_approved_floor_request_runs_without_losing_or_duplicating_approval(tmp_path, monkeypatch, race):
    w = World(tmp_path, monkeypatch, reserved="reserve", models={"reserve": "model-a"})
    w.unusable("alpha", "beta", "other")
    w.readings["reserve"] = Budget("reserve", known=True, headroom=.2)
    gate = tmp_path / "release-floor"
    w.set_plan("reserve", gate=str(gate))
    refused = []

    async def scenario():
        await w.r.start("worker", TASK)
        request_id = w.events("reserve_request")[0]["request_id"]
        assert not w.calls()
        await reserve_tool("allow_reserve", request_id)
        original_add = w.r._admission_add

        def add(spec, node, queued_id=""):
            if race == "concurrency" and not refused:
                refused.append(node.id)
                raise ProviderFull("reserve", 1, ["competing-run"])
            return original_add(spec, node, queued_id)

        monkeypatch.setattr(w.r, "_admission_add", add)
        original_launch = w.r._launch

        async def launch(**kwargs):
            if race == "spend_cap" and not refused:
                refused.append(kwargs["node_id"])
                raise SpendCapRefused({"reason": "spend_cap: test launch race", "until": w.clock[0] + 60})
            return await original_launch(**kwargs)

        monkeypatch.setattr(w.r, "_launch", launch)
        try:
            await w.r.resume_deferred()
            if race != "none":
                assert refused and not w.calls()
                requests = w.r.tree.read()["quota_reserve"]
                assert len(requests) == 1, "late refusal created a second request without its approval"
                assert requests[0]["id"] == request_id and requests[0]["state"] == "allowed"
                await w.r.resume_deferred()
            await wait_until(lambda: bool(w.calls()))
            assert [call["instance"] for call in w.calls()] == ["reserve"]
            request = w.r.tree.read()["quota_reserve"][0]
            node = w.r.tree.get(request["dispatched_id"])
            assert node.provider == "reserve" and node.on_reserve_floor
            assert node.reserve_request == request_id
            assert len(w.events("reserve_request")) == 1
            assert len(w.events("reserve_allowed")) == 1
            gate.touch()
            await w.settle(node.id)
            assert w.r.tree.get(node.id).status == "done"
            await w.r.resume_deferred()
            assert len(w.r.tree.read()["quota_reserve"]) == 1
            assert len(w.calls()) == 1
        finally:
            gate.touch()
            for node in w.r._qh_nodes():
                await w.r.stop(node.id)

    asyncio.run(scenario())
