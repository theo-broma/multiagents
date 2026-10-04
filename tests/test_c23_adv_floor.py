"""Adversary (ag-89bcfd): QH-R14..R16 reserved floor admission leaks."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World, TASK, wait_until, reserve_tool  # noqa: E402
from multiagents import quota_handover as qh  # noqa: E402
from multiagents.budget import Budget  # noqa: E402


def test_qh_r15_floor_admission_does_not_leak_into_the_floor_runs_own_tasks(tmp_path, monkeypatch):
    """The floor ContextVar is set around start(); every task start() creates
    (the run's _supervise, and the _drain_all a slot release spawns from it)
    copies that context. A later launch from those tasks is admitted below the
    floor with no reserve_request, no veto window and on_reserve_floor=True.
    """
    w = World(tmp_path, monkeypatch, reserved='reserve', models={'reserve': 'model-a'})
    w.unusable('alpha', 'beta', 'other')
    w.readings['reserve'] = Budget('reserve', known=True, headroom=.2)
    gate = tmp_path / 'release'
    w.set_plan('reserve', gate=str(gate))

    async def scenario():
        await w.r.start('worker', TASK)
        requests = w.events('reserve_request')
        assert requests
        try:
            await reserve_tool('allow_reserve', requests[0]['request_id'])
            await w.r.resume_deferred()
            await wait_until(lambda: bool(w.r.runs))
            run = next(iter(w.r.runs.values()))
            leaked = run.task.get_context().get(qh._floor_dispatch, "")
            assert leaked == "", ("floor admission token %r is inherited by the floor run's "
                                  "supervisor task and every task it spawns" % leaked)
        finally:
            gate.touch()
            await asyncio.sleep(.3)
    asyncio.run(scenario())
