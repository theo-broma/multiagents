"""Spend-cap launch and accounting failure paths with fake local CLIs."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import sc_harness as sc
from multiagents import spendcap
from multiagents.runner import Run
from multiagents.tree import Node
from multiagents.providers import Event


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = sc.World(tmp_path, monkeypatch)
    w.provider("acme", spend_cap={"usd": 1.0})
    w.agent("worker", "acme", "acme/m1")
    w.up()
    yield w
    w.down()


def test_capped_start_refuses_when_missing_ledger_cannot_be_created(world, monkeypatch):
    ledger = world.runner.ledger
    real_open = os.open

    def cannot_create(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(ledger.path) and flags & os.O_CREAT:
            raise PermissionError("ledger creation denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", cannot_create)

    result = world.runner._cap_refusal("acme", "acme/m1")
    assert result is not None, "admission permits a cap whose ledger cannot be created"
    assert "spend_cap_unreadable" in str(result)
    assert world.p.fakes["acme"].spawns() == 0


def test_failed_charge_never_advances_adoption_checkpoint_past_it(world, monkeypatch):
    runner = world.runner
    failed_at = []
    checkpoints_after_failure = []
    real_note = runner.tree.note_event

    def fail_append(records):
        failed_at.append(True)
        raise spendcap.LedgerError("disk full during charge")

    def observe_checkpoint(*args, **kwargs):
        if failed_at and kwargs.get("follow", {}).get("offset", 0) > 0:
            checkpoints_after_failure.append(kwargs["follow"])
        return real_note(*args, **kwargs)

    monkeypatch.setattr(runner.ledger, "_append", fail_append)
    monkeypatch.setattr(runner.tree, "note_event", observe_checkpoint)

    class Handle:
        offset = 0

        async def lines(self):
            self.offset = 200
            yield json.dumps({"type": "step_finish", "sessionID": "ses_1",
                              "part": {"id": "prt_write_failure", "type": "step-finish",
                                       "cost": 0.25, "reason": "tool-calls"}})

        async def drain_stderr(self):
            return

        async def wait(self):
            return 0

        async def stop(self):
            return

    async def finalized(*args):
        return False

    monkeypatch.setattr(runner, "_finalize", finalized)
    monkeypatch.setattr(runner, "_pc_kick", lambda *args: None)
    node_id = "ag-writefail"
    spec = runner.config.agents.get("worker")
    provider = runner.providers.get("acme")
    runner.tree.add(Node(id=node_id, agent="worker", provider="acme",
                         model="acme/m1", parent=None, depth=1, status="running"))
    runner.paths.run_dir(node_id).mkdir(parents=True, exist_ok=True)
    run = Run(node_id=node_id, provider=provider, spec=spec, handle=Handle(),
              supervisor=runner._supervisor(spec, provider, 0))

    async def go():
        await runner._consume(run)

    asyncio.run(go())
    assert failed_at, "fault must be exercised"
    assert not checkpoints_after_failure, (
        "the charge never landed, but adoption will skip its stream position: "
        f"{checkpoints_after_failure}")


def test_distinct_session_and_step_pairs_do_not_collide_on_separator(world):
    runner = world.runner
    run = Run(node_id="ag-keycheck", provider=runner.providers.get("acme"),
              spec=runner.config.agents.get("worker"))
    runner._charge(run, Event(kind="step", cost=0.25, step_id="b"),
                   "ses|a", 100, False)
    runner._charge(run, Event(kind="step", cost=0.25, step_id="a|b"),
                   "ses", 200, False)
    runner.ledger.refresh()
    assert runner.ledger.spend("acme", "", "day", sc.WED) == 0.5
