"""M2 — migration of the legacy queue and runs that exist when the gate turns
on: NC-R19 and NC-R54.

The legacy state is built the way today's code builds it: queue entries with
`Tree.enqueue` / `Tree.defer` (their real shapes), runs launched with the gate
OFF from a helper process (`legacy_start`). The gate is then turned on and the
scheduler started.

Assumptions where the contract is silent (kept loose):
- a migrated node's `pins` mention the provider of the entry (NC-R19: "pins
  ... recorded provider from its spec").
- `created_at` is epoch seconds or ISO-8601.
- a migrated node is under root: its plan `parent` is null.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import alive, task  # noqa: E402
from nc_fixture.world import (World, blocked_codes, enable_gate, legacy_start,  # noqa: E402
                              write_tree_entries)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, gate=False)
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    yield world
    world.close()


def epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()


def pc_spec(agent: str, text: str, *, parent=None, depth=1, model="pcfx/m1") -> dict:
    return {"op": "start", "agent": agent, "task": text, "timeout": None, "model": model,
            "pinned": False, "workdir": None, "verifies": "", "budget_tag": "",
            "parent": parent, "depth": depth}


class Queue:
    ids: dict[str, str]
    queued_at: dict[str, float]


def make_queue(w: World) -> Queue:
    q = Queue()
    q.ids, q.queued_at = {}, {}

    def build(tree):
        for tag in ("M1", "M2"):
            e = tree.enqueue("pcfx", pc_spec("pcworker", task(tag, gate=f"g{tag}")),
                             "provider full")
            q.ids[tag], q.queued_at[tag] = e["id"], e["queued_at"]
            time.sleep(0.05)
        e = tree.defer({"agent": "worker", "task": task("M3", gate="gM3"), "timeout": None,
                        "model": None, "workdir": None, "provider": "fx"},
                       time.time() + 3600, "quota window")
        q.ids["M3"], q.queued_at["M3"] = e["id"], e["queued_at"]
        e = tree.defer({"agent": "worker", "task": task("M4", gate="gM4"), "timeout": None,
                        "model": None, "workdir": None, "provider": "fx"},
                       time.time() + 3600, "quota window")
        q.ids["M4"], q.queued_at["M4"] = e["id"], e["queued_at"]
        with tree.transaction() as data:
            for d in data["deferred"]:
                if d["id"] == e["id"]:
                    d["status"] = "refused"

    write_tree_entries(w, build)
    return q


def by_tag(w: World, tag: str) -> dict | None:
    for n in w.list():
        if f"[{tag}]" in (n.get("task") or ""):
            return n
    return None


def test_nc_r19_each_queue_entry_becomes_one_simple_node_and_the_queue_empties(w):
    make_queue(w)
    enable_gate(w)
    w.start_scheduler()
    assert w.deferred() == []
    nodes = {t: by_tag(w, t) for t in ("M1", "M2", "M3", "M4")}
    assert all(nodes.values()), nodes
    assert len({n["id"] for n in nodes.values()}) == 4
    assert len(w.list()) == 4
    for tag, n in nodes.items():
        assert n["kind"] == "simple" and n["parent"] in (None, "")
    assert nodes["M1"]["agent"] == "pcworker" and nodes["M3"]["agent"] == "worker"
    assert "pcfx" in json.dumps(nodes["M1"]["pins"]) and "pcfx" in json.dumps(nodes["M2"]["pins"])


def test_nc_r19_created_at_is_the_time_the_entry_was_queued(w):
    q = make_queue(w)
    enable_gate(w)
    w.start_scheduler()
    for tag in ("M1", "M2", "M3"):
        assert epoch(by_tag(w, tag)["created_at"]) == pytest.approx(q.queued_at[tag], abs=1.0)


def test_nc_r19_a_refused_entry_becomes_a_held_node_and_never_launches(w):
    make_queue(w)
    enable_gate(w)
    w.start_scheduler()
    n = by_tag(w, "M4")
    assert n["state"] == "held"
    assert (n["hold"] or {}).get("reason") == "admission:refused"
    assert "held" in blocked_codes(w.get(n["id"]))
    w.quiet(3)
    assert w.fx.by_tag("M4") == []
    assert "admission:refused" in json.dumps(w.status())


def test_nc_r19_a_second_start_creates_nothing_and_launches_nothing_twice(w):
    make_queue(w)
    enable_gate(w)
    w.start_scheduler()
    first = sorted(n["id"] for n in w.list())
    w.wait_spawn("M1", w.pc)
    w.restart_scheduler()
    w.quiet(3)
    assert sorted(n["id"] for n in w.list()) == first
    assert w.deferred() == []
    assert len(w.pc.by_tag("M1")) == 1


def test_nc_r19_migrated_pc_entries_keep_their_order(w):
    make_queue(w)
    enable_gate(w)
    w.start_scheduler()
    w.wait_spawn("M1", w.pc)
    w.quiet(2)
    assert w.pc.by_tag("M2") == [], "M2 was queued after M1 behind a one-slot provider"
    w.gate("gM1", w.pc)
    w.wait_spawn("M2", w.pc)


def test_nc_r19_a_migrated_node_launches_under_the_recorded_run_parent_and_depth(w):
    parent_run, _ = legacy_start(w, "worker", task("PARENT"))
    w.until(lambda: w.fx.done_tags() == ["PARENT"], what="the legacy parent to finish")

    def build(tree):
        tree.enqueue("pcfx", pc_spec("pcworker", task("CHILD"), parent=parent_run, depth=2),
                     "provider full")
    write_tree_entries(w, build)
    enable_gate(w)
    w.start_scheduler()
    node = by_tag(w, "CHILD")
    assert node["parent"] in (None, "")                  # plan parent: root
    done = w.wait_state(node["id"], "done", timeout=60)
    tree_node = w.tree_nodes()[done["runs"][0]["run_id"]]
    assert tree_node["parent"] == parent_run
    assert tree_node["depth"] == 2


# ----------------------------------------------------------------- NC-R54

def test_nc_r54_a_legacy_run_is_not_adopted_and_still_counts_against_the_slots(w):
    run_id, helper = legacy_start(w, "pcworker", task("L", gate="gL"))
    w.wait_spawn("L", w.pc)
    enable_gate(w)
    w.start_scheduler()
    assert all(run_id not in json.dumps(n) for n in w.list()), "legacy run adopted into a node"
    b = w.simple("B", "pcworker")
    w.until(lambda: "admission:provider_concurrency" in blocked_codes(w.get(b)),
            what="the legacy run to hold the slot")
    assert w.tree_nodes()[run_id]["status"] == "running"
    w.quiet(2)
    assert w.pc.by_tag("B") == []
    w.gate("gL", w.pc)
    w.wait_state(b, "done", timeout=60)
    assert w.tree_nodes()[run_id]["status"] == "done"
    assert all(run_id not in json.dumps(n) for n in w.list())


def test_nc_r54_a_finished_legacy_run_keeps_its_legacy_tools(w):
    run_id, _ = legacy_start(w, "worker", task("L"))
    w.until(lambda: w.fx.done_tags() == ["L"], what="legacy run to finish")
    enable_gate(w)
    w.start_scheduler()
    from nc_fixture.world import call_tool
    checked = call_tool(w, "check_agent", run_id)
    assert checked.get("status") == "done" and not checked.get("error")
    assert w.list() == []


def test_nc_r54_a_legacy_resume_takes_the_scheduler_admission(w):
    run_id, _ = legacy_start(w, "pcworker", task("L"))
    w.until(lambda: w.pc.done_tags() == ["L"], what="legacy run to finish")
    enable_gate(w)
    w.start_scheduler()
    h = w.simple("H", "pcworker", fx={"gate": "gH"})
    w.wait_running(h)
    from nc_fixture.world import call_tool
    spawns = len(w.pc.calls())
    r = call_tool(w, "steer_agent", run_id, "again")
    assert any("provider_concurrency" in str(b) for b in (r.get("blocked") or [])), r
    assert w.deferred() == []
    assert len(w.pc.calls()) == spawns
