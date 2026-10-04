"""NC-R18: configuration changes invalidate an in-flight admission probe."""
import asyncio
from unittest.mock import AsyncMock

from test_nc_m2_guard_recovery import local, deposit, read


def test_nc_r18_a_claim_uses_the_configuration_that_was_actually_admitted(local, monkeypatch):
    world, engine = local
    node = deposit(engine)
    probes = []

    async def admit(*args, **kwargs):
        probes.append(kwargs["launch_context"])
        world.agents.pop("worker")
        world.write_config()
        return {"admitted": True}

    monkeypatch.setattr(engine.runner, "start", admit)
    monkeypatch.setattr(engine.runner, "adopt", AsyncMock())
    monkeypatch.setattr(engine, "reconcile", AsyncMock())
    spawned = []
    monkeypatch.setattr(engine, "spawn", spawned.append)
    asyncio.run(engine.tick())
    nodes, journal = read(engine)
    assert len(probes) == 1 and probes[0].admission_only
    assert spawned == [] and journal == {}
    assert nodes[node["id"]]["state"] == "open"
