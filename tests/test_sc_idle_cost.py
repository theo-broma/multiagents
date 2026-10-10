"""SC — the idle scheduler costs almost nothing (context/specs/scheduler-idle-cost.md).

SC-R1  the between-tick completion check decodes no attempt record when
       nothing is claimed or launched, stays bounded by the active count
       otherwise, and still wakes when an active attempt turns terminal.
SC-R2  an idle tick on 5,000 terminal nodes/attempts costs at most 2x the same
       tick on 50, and yields the same transitions, holds, attention file and
       notifications.
SC-R3  is the unchanged observable contract: it is held by the existing
       scheduler suite (per-id outcomes), not by a test here.

Cost proxy, deterministic and implementation-neutral: the number of JSON
records decoded (`json.loads` calls) and the number of SQLite virtual-machine
steps (a progress handler on every store connection, which counts rows
scanned however the query is shaped). Never wall clock.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness
from multiagents.scheduler.engine import save_attempt
from multiagents.tree import now

HISTORY = 2000        # SC-R1: terminal attempts and nodes
SMALL, LARGE = 50, 5000   # SC-R2
SLACK = 2.0


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


class Meter:
    """Counts decoded records and SQLite VM steps while active."""

    def __init__(self, monkeypatch):
        self.decoded = 0
        self.steps = 0
        real_loads = json.loads
        real_connect = sqlite3.connect

        def loads(*args, **kwargs):
            self.decoded += 1
            return real_loads(*args, **kwargs)

        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_progress_handler(self._step, 1)
            return db

        monkeypatch.setattr(json, "loads", loads)
        monkeypatch.setattr(sqlite3, "connect", connect)

    def _step(self):
        self.steps += 1
        return 0

    def reset(self):
        self.decoded = self.steps = 0


def populate(h, n):
    """`n` terminal nodes, each with one terminal attempt, written directly."""
    nodes = [h.record(task=f"old-{i}") for i in range(n)]
    for i, node in enumerate(nodes):
        node.update(state="done" if i % 2 else "cancelled", outcome="completed")
    with h.service.store.transaction() as db:
        for i, node in enumerate(nodes):
            h.service.store.save_node(db, node)
            save_attempt(db, {"attempt_id": f"old-attempt-{i}", "activation_id": f"old-act-{i}",
                              "run_id": f"old-run-{i}", "node_id": node["id"],
                              "state": "recorded" if i % 2 else "abandoned",
                              "locks": [], "at": now(), "ended_at": now()})
    return nodes


def check_cost(h, meter):
    meter.reset()
    h.engine.completion_changed()
    return meter.decoded


# ---------------------------------------------------------------- SC-R1
def test_sc_r1_completion_check_decodes_no_attempt_when_nothing_is_active(h, monkeypatch):
    populate(h, HISTORY)
    meter = Meter(monkeypatch)
    assert h.engine.completion_changed() is False
    assert check_cost(h, meter) == 0


def test_sc_r1_completion_check_is_bounded_by_the_active_attempts(h, monkeypatch):
    populate(h, HISTORY)
    live = [h.record(task=f"live-{i}") for i in range(3)]
    h.save(*live)
    for node in live:
        h.launch(node["id"])
    meter = Meter(monkeypatch)
    h.engine.completion_changed()
    cost = check_cost(h, meter)
    # a few decodes per active attempt (plus the tree read) is fine; one per terminal is not
    assert cost <= 3 * 3 + 5, f"{cost} records decoded with 3 active and {HISTORY} terminal attempts"


def test_sc_r1_the_check_does_not_grow_with_history(h, monkeypatch):
    live = h.record(task="live")
    h.save(live)
    h.launch(live["id"])
    meter = Meter(monkeypatch)
    small = check_cost(h, meter)
    populate(h, HISTORY)
    large = check_cost(h, meter)
    assert large <= small, f"decodes grew from {small} to {large} with history"


def test_sc_r1_steps_do_not_grow_with_history_when_idle(h, monkeypatch):
    meter = Meter(monkeypatch)
    populate(h, 10)
    meter.reset()
    h.engine.completion_changed()
    small = meter.steps
    populate(h, HISTORY)
    meter.reset()
    h.engine.completion_changed()
    assert meter.steps <= max(small * SLACK, small + 50), \
        f"rows read grew from {small} to {meter.steps} steps"


def test_sc_r1_still_wakes_when_a_launched_attempt_turns_terminal(h, monkeypatch):
    # The harness launches with a tree status of "done" (terminal) and an
    # attempt still "launched": the wake condition. It must hold with a large
    # history in the store, and clear once the attempt is recorded.
    populate(h, HISTORY)
    node = h.record(task="live")
    h.save(node)
    attempt, run = h.launch(node["id"])
    assert h.engine.completion_changed() is True
    assert h.engine.completion_changed() is True   # repeated: not consumed
    h.engine.finished(attempt, run)
    assert h.engine.completion_changed() is False


def test_sc_r1_still_wakes_for_a_claimed_attempt(h):
    populate(h, HISTORY)
    node = h.record(task="claimed")
    h.save(node)
    attempt, run = h.launch(node["id"])
    with h.service.store.transaction() as db:
        save_attempt(db, {**h.journal()[attempt["attempt_id"]], "state": "claimed"})
    assert h.engine.completion_changed() is True


def test_sc_r1_a_terminal_attempt_never_wakes(h):
    populate(h, 20)
    node = h.record(task="finished")
    h.save(node)
    attempt, run = h.launch(node["id"])
    with h.service.store.transaction() as db:
        save_attempt(db, {**h.journal()[attempt["attempt_id"]], "state": "recorded"})
    assert h.engine.completion_changed() is False


# ---------------------------------------------------------------- SC-R2
def idle_tick(h):
    asyncio.run(h.engine.tick())


def observable(h):
    paths = h.world.paths
    attention = (paths.data / "attention.json")
    with h.service.store.transaction(write=False) as db:
        transitions = [(r[0], json.loads(r[1])) for r in db.execute("SELECT seq, record FROM notifications ORDER BY seq")]
        outbox = [tuple(r) for r in db.execute("SELECT kind, ref, data FROM notify_outbox ORDER BY seq")]
    return {
        "nodes": {id: (n["state"], n.get("hold"), n.get("outcome")) for id, n in h.nodes().items()},
        "transitions": [(t["kind"], t["node_id"], t["detail"]) for _, t in transitions],
        "outbox": outbox,
        "attention": attention.read_text() if attention.exists() else None,
    }


def measured_tick(h, meter):
    idle_tick(h)          # warm-up: first tick may read the history once
    meter.reset()
    idle_tick(h)
    return meter.decoded, meter.steps


@pytest.mark.parametrize("which", ["decoded", "steps"])
def test_sc_r2_idle_tick_cost_does_not_grow_with_history(tmp_path_factory, monkeypatch, which):
    costs = {}
    for size in (SMALL, LARGE):
        root = tmp_path_factory.mktemp(f"sc-{size}")
        with monkeypatch.context() as m:
            harness = Harness(root, m)
            try:
                populate(harness, size)
                meter = Meter(m)
                decoded, steps = measured_tick(harness, meter)
                costs[size] = decoded if which == "decoded" else steps
            finally:
                harness.close()
    assert costs[LARGE] <= SLACK * max(costs[SMALL], 1), \
        f"idle tick {which}: {costs[SMALL]} at {SMALL} terminal nodes, {costs[LARGE]} at {LARGE}"


def seed_mixed(h, terminal):
    """Terminal history plus held and open nodes that must keep their handling."""
    populate(h, terminal)
    held = h.record(task="held")
    h.save(held)
    with h.service.store.transaction() as db:
        h.engine.hold(db, h.service.store.nodes(db)[held["id"]], "dirty_worktree", "kept.diff")
    return held


def test_sc_r2_idle_ticks_on_a_mixed_store_are_stable_and_report_the_holds(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    try:
        held = seed_mixed(harness, 300)
        idle_tick(harness)
        first = observable(harness)
        idle_tick(harness)
        second = observable(harness)
        assert second == first                     # idle: nothing new on a repeat
        assert held["id"] in first["attention"]    # attention lists the hold
        assert first["nodes"][held["id"]][0] == "held"
        assert [t for t in first["transitions"] if t[0] == "held" and t[1] == held["id"]], \
            "the hold was recorded exactly as a transition"
        assert len([t for t in first["transitions"] if t[0] == "held"]) == 1
        # terminal history is untouched and produced no transition of its own
        assert all(t[1] == held["id"] for t in first["transitions"] if t[0] == "held")
    finally:
        harness.close()


def test_sc_r2_mixed_store_observables_do_not_depend_on_history_size(tmp_path_factory, monkeypatch):
    seen = {}
    for size in (SMALL, 600):
        root = tmp_path_factory.mktemp(f"sc-mixed-{size}")
        with monkeypatch.context() as m:
            harness = Harness(root, m)
            try:
                held = seed_mixed(harness, size)
                idle_tick(harness)
                obs = observable(harness)
                # compare only what concerns the held node and the totals of non-history output
                seen[size] = {
                    "hold_state": obs["nodes"][held["id"]],
                    "held_transitions": [t[0] for t in obs["transitions"] if t[1] == held["id"]],
                    "attention_has_hold": held["id"] in (obs["attention"] or ""),
                    "other_transitions": sorted(t[0] for t in obs["transitions"] if t[1] != held["id"]),
                    "terminal_states": sorted(v[0] for k, v in obs["nodes"].items() if k != held["id"])[:1],
                }
            finally:
                harness.close()
    assert seen[SMALL] == seen[600]
    assert seen[SMALL]["attention_has_hold"] is True
