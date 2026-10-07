"""TB2-R3: `wait_for_nodes` carries the same `capacity` object as
`wait_for_agents` (`running`, `max_concurrent`, `free_slots`, and the idle note).

Driven through the MCP tools `server.wait_for_nodes` / `server.wait_for_agents`
called in-process as the orchestrator does, against a real scheduler with
fixture runs.

Where the contract is silent (kept loose):
- "the same object" is checked as: the same keys and values as the one
  `wait_for_agents` reports at the same moment, plus the figures that follow from
  the world (one gated node running).
- the idle note's wording is not pinned; only its presence (a slot is free and
  something runs) or absence (nothing running, or no slot free).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT = 8
NODE_WAIT = 1     # `timeout` of a wait_for_nodes that has nothing to wait for


def make_world(tmp_path, monkeypatch, max_concurrent=None):
    world = M4World(tmp_path, monkeypatch)
    if max_concurrent is not None:
        world.project["limits"]["max_concurrent"] = max_concurrent
    world.start_scheduler()
    return world


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


def capacity_of(reply: dict) -> dict:
    assert isinstance(reply, dict) and "capacity" in reply, reply
    return reply["capacity"]


def test_tb2_r3_a_wait_with_one_node_running_reports_the_capacity(w):
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node, timeout=WAIT)
    cap = capacity_of(call_tool(w, "wait_for_nodes", timeout=NODE_WAIT))
    assert cap.get("running") == 1, cap
    assert isinstance(cap.get("max_concurrent"), int) and cap["max_concurrent"] >= 2, cap
    assert cap.get("free_slots") == cap["max_concurrent"] - 1, cap
    assert cap.get("note"), f"a slot is free and one node runs: the idle note is missing: {cap}"
    w.gate("ga")


def test_tb2_r3_it_is_the_object_wait_for_agents_reports(w):
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node, timeout=WAIT)
    nodes_cap = capacity_of(call_tool(w, "wait_for_nodes", timeout=NODE_WAIT))
    agents_cap = capacity_of(call_tool(w, "wait_for_agents", None, timeout=1))
    assert nodes_cap == agents_cap
    w.gate("ga")


def test_tb2_r3_a_wait_that_times_out_without_transitions_still_reports_it(w):
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node, timeout=WAIT)
    first = call_tool(w, "wait_for_nodes", timeout=NODE_WAIT)
    cursor = first["next_cursor"]
    again = call_tool(w, "wait_for_nodes", cursor=cursor, timeout=NODE_WAIT)
    assert again["transitions"] == [], again
    cap = capacity_of(again)
    assert cap.get("running") == 1 and cap.get("free_slots") == cap["max_concurrent"] - 1, cap
    w.gate("ga")


def test_tb2_r3_with_nothing_running_all_slots_are_free_and_there_is_no_note(w):
    cap = capacity_of(call_tool(w, "wait_for_nodes", timeout=NODE_WAIT))
    assert cap.get("running") == 0, cap
    assert isinstance(cap.get("max_concurrent"), int) and cap["max_concurrent"] >= 1, cap
    assert cap.get("free_slots") == cap["max_concurrent"], cap
    assert not cap.get("note"), f"nothing runs: there is nothing to say about idle slots: {cap}"


def test_tb2_r3_a_filtered_wait_reports_it_too(w):
    node = w.simple("A", fx={"gate": "ga"})
    w.wait_running(node, timeout=WAIT)
    reply = call_tool(w, "wait_for_nodes", node_ids=[node], timeout=NODE_WAIT)
    assert capacity_of(reply).get("running") == 1, reply
    w.gate("ga")


def test_tb2_r3_a_full_house_reports_no_free_slot_and_no_note(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, max_concurrent=1)
    try:
        node = world.simple("A", fx={"gate": "ga"})
        world.wait_running(node, timeout=WAIT)
        cap = capacity_of(call_tool(world, "wait_for_nodes", timeout=NODE_WAIT))
        assert cap.get("max_concurrent") == 1 and cap.get("running") == 1, cap
        assert cap.get("free_slots") == 0, cap
        assert not cap.get("note"), cap
        world.gate("ga")
    finally:
        world.close()


def test_tb2_r3_the_figure_follows_the_runs_when_one_ends(w):
    a = w.simple("A", fx={"gate": "ga"})
    b = w.simple("B", fx={"gate": "gb"})
    w.wait_running(a, timeout=WAIT)
    w.wait_running(b, timeout=WAIT)
    assert capacity_of(call_tool(w, "wait_for_nodes", timeout=NODE_WAIT))["running"] == 2
    w.gate("ga")
    w.wait_state(a, "done", timeout=WAIT)
    cap = capacity_of(call_tool(w, "wait_for_nodes", timeout=NODE_WAIT))
    assert cap["running"] == 1 and cap["free_slots"] == cap["max_concurrent"] - 1, cap
    w.gate("gb")
