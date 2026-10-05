"""NC-R40 publication races and window-validation lock scope."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness
from multiagents import scheduler_config
from multiagents.scheduler import windows
from multiagents.scheduler.engine import save_attempt
from multiagents.scheduler.results import Results


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def launch(h):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    h.engine.runner.tree.update(run.id, status="running", session_id="interrupted")
    return node, attempt, run


def test_nc_r40_stale_reconcile_snapshot_cannot_capture_a_suspension(h, monkeypatch):
    node, attempt, run = launch(h)
    tree = h.engine.runner.tree
    # Reconcile can already hold this snapshot when the supervisor journals
    # its stop and publishes a dead tree entry. Capture must check the store.
    stale = h.journal()[attempt["attempt_id"]]
    with h.service.store.transaction() as db:
        current = dict(stale, window_stop=True, window_stop_started=True)
        save_attempt(db, current)
    tree.update(run.id, status="idle", reason="window suspended")

    def capture(*args):
        pytest.fail("an interrupted turn reached result capture")

    monkeypatch.setattr(Results, "capture", capture)
    h.engine.finished(stale, tree.get(run.id))
    assert h.journal()[attempt["attempt_id"]]["state"] == "launched"
    assert "capture_intent" not in h.journal()[attempt["attempt_id"]]
    assert h.nodes()[node["id"]]["generations"] == []


def test_nc_r40_confirmed_suspension_is_durable_before_tree_idle(h, monkeypatch):
    node, attempt, run = launch(h)
    runner = h.engine.runner
    set_status = runner.tree.set_status

    def publish(id, status, reason=""):
        if status == "idle":
            current = h.journal()[attempt["attempt_id"]]
            assert current["state"] == "suspended"
            assert h.nodes()[node["id"]]["state"] == "suspended"
        return set_status(id, status, reason)

    async def stop(id, *, internal):
        assert internal

    async def dead(predecessor):
        return True

    monkeypatch.setattr(runner.tree, "set_status", publish)
    monkeypatch.setattr(runner, "_steer_predecessor", lambda id: SimpleNamespace(absent=False))
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    monkeypatch.setattr(runner, "stop", stop)
    assert asyncio.run(runner.suspend(run.id))["predecessor_death_confirmed"]
    assert runner.tree.get(run.id).status == "idle"


@pytest.mark.parametrize("template", [False, True])
@pytest.mark.parametrize("zone", ["America/Barbados", "Not/AZone"])
def test_window_zone_lookup_happens_before_rpc_transaction(h, monkeypatch, template, zone):
    store = h.service.store
    if not template:
        # A Service also serves validation requests before it has an Engine.
        h.service.engine = None
    else:
        definition = {"template": "zone-param", "version": 1,
                      "params": {"tz": {"type": "string"}},
                      "root": {"key": "job", "kind": "simple", "agent": "coder", "task": "work",
                               "window": {"timezone": {"param": "tz"}, "days": ["mon"],
                                          "ranges": ["09:00-17:00"]}}}
        with store.transaction() as db:
            db.execute("INSERT INTO templates VALUES (?, ?)", ("zone-param", json.dumps(definition)))
    with store.transaction(write=False) as db:
        token = store.meta(db, "root_token")
    monkeypatch.delitem(windows.ZONES, zone, raising=False)
    transaction = store.transaction
    lookup = windows.ZoneInfo
    depth = 0
    looked_up = []

    @contextmanager
    def tracked(*, write=True):
        nonlocal depth
        with transaction(write=write) as db:
            depth += 1
            try:
                yield db
            finally:
                depth -= 1

    def zoneinfo(name):
        assert depth == 0, "tzdata lookup inside store.transaction()"
        looked_up.append(name)
        return lookup(name)

    monkeypatch.setattr(store, "transaction", tracked)
    monkeypatch.setattr(windows, "ZoneInfo", zoneinfo)
    monkeypatch.setattr(scheduler_config, "ZoneInfo", zoneinfo)
    args = ({"name": "zone-param", "params": {"tz": zone}} if template else
            {"kind": "simple", "agent": "coder", "task": "work", "plan_revision": 0,
             "window": {"timezone": zone, "days": ["mon"], "ranges": ["09:00-17:00"]}})
    reply = h.service.request({"op": "instantiate_template" if template else "create_node",
                               "args": args, "token": token, "request_id": "zone-check"})
    assert zone in looked_up
    assert reply["ok"] is (zone == "America/Barbados"), reply
    if not reply["ok"]:
        assert reply["error"]["error"] == "invalid"
