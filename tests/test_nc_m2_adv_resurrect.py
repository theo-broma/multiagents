"""M2 adversary — node states that a later operation brings back to life.

- NC-R59: only scheduler transitions write `state`; a `cancelled` node is
  terminal (NC-R80), and a `done` node reopens only through `relaunch_node`.
- NC-R56: steering a managed run is an activation of the same run, admitted
  by the scheduler; it is not a way to reopen a node.
- NC-R17: a node never has two live runs from one eligibility, and nothing
  launches for a node that is no longer `open`.

Everything goes through public surfaces: the RPC, the MCP tool functions of
`multiagents.server` (as the orchestrator calls them), and the fixture's
calls log. The store is read (never written) to catch the claim window.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.world import World, call_tool, run_id_of  # noqa: E402
from multiagents.paths import state_root  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.close()


def test_steering_the_run_of_a_cancelled_node_does_not_bring_the_node_back(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "ga"})
    run = run_id_of(w.wait_running(a)["active_run"])
    reply = w.cancel(a)
    assert reply.get("ok"), reply
    w.wait_state(a, "cancelled", timeout=20)
    spawns = len(w.fx.by_tag("A"))

    call_tool(w, "steer_agent", run, "carry on anyway")
    w.quiet(2)

    node = w.get(a)
    assert node["state"] == "cancelled", (
        f"a steer turned a cancelled node into {node['state']!r} (outcome {node['outcome']!r})")
    assert len(w.fx.by_tag("A")) == spawns, "a cancelled node's session was resumed"


def _claimed(w: World, node_id: str) -> bool:
    db_file = state_root() / "scheduler" / w.paths.slug / "plan.sqlite3"
    db = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        rows = [json.loads(r) for (r,) in db.execute("SELECT record FROM attempts")]
    finally:
        db.close()
    return any(r.get("node_id") == node_id and r.get("state") == "claimed" for r in rows)


def test_cancelling_a_node_between_claim_and_launch_launches_nothing(w):
    w.start_scheduler()
    a = w.simple("A", fx={"gate": "ga"})
    end = time.monotonic() + 15
    while time.monotonic() < end and not _claimed(w, a):
        time.sleep(0.01)
    assert _claimed(w, a), "never observed the claimed attempt"
    assert w.fx.by_tag("A") == [], "launched before the claim window could be used"
    reply = w.cancel(a)
    w.quiet(4)
    node = w.get(a)
    assert w.fx.by_tag("A") == [], (
        f"cancel_node during the claim returned {reply} and the run launched anyway; "
        f"node is now {node['state']!r}")
    assert node["state"] in ("cancelled", "held"), node["state"]
