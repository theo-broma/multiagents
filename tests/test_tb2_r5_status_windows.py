"""TB2-R5: status output does not grow with history.

`scheduler_status` and wait replies list window entries only for nodes that are
not final (open, running, held, pending resume): done and cancelled nodes have
no entry.

Where the contract is silent (kept loose):
- whether a non-final node WITHOUT a window has an entry is not pinned (today
  every node has one); the tests either give every non-final node a window or
  only look at the entries of final nodes and of the nodes with a window.
- an entry is recognised by its `node_id` field, under the `windows` key of the
  status (`scheduler_status` result, and the `scheduler` object of a
  `wait_for_nodes` reply).
- "pending resume" nodes have no cheap fixture here; open, running and held
  stand for the non-final states.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.clock import ALL_DAYS, ClockWorld, local  # noqa: E402
from nc_fixture.m4_world import M4World  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT = 8
DONE_NODES = 50
HISTORY_WAIT = 90     # the one slow test: 50 runs through a ticking scheduler
ASK = "NEED_INFO(q): ASK-QUESTION-903?"
NEUTRAL = local(2026, 10, 5, 12, 0)                 # Monday noon, Paris
CLOSED = {"days": list(ALL_DAYS), "ranges": ["20:00-22:00"]}     # closed at noon
OPEN = {"days": list(ALL_DAYS), "ranges": ["00:00-23:59"]}


@pytest.fixture
def cw(tmp_path, monkeypatch):
    world = ClockWorld(tmp_path, monkeypatch, now=NEUTRAL,
                       scheduler={"starvation_after_seconds": 10**7})
    world.project["limits"]["max_concurrent"] = 8
    world.start_scheduler()
    yield world
    world.close()


@pytest.fixture
def mw(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.start_scheduler()
    yield world
    world.close()


def window_ids(status: dict) -> list[str]:
    return [e["node_id"] for e in status["windows"]]


def wait_status(w) -> dict:
    return call_tool(w, "wait_for_nodes", timeout=0)["scheduler"]


def test_tb2_r5_fifty_done_nodes_and_one_open_node_with_a_window_list_one_entry(cw):
    done = [cw.simple(f"D{i}", fx={"text": "ok"}) for i in range(DONE_NODES)]
    cw.until(lambda: all(cw.get(n)["state"] == "done" for n in done), HISTORY_WAIT,
             step=0.5, what=f"{DONE_NODES} nodes done")
    open_node = cw.simple("O", window=CLOSED)
    cw.until(lambda: cw.get(open_node)["state"] == "open" and cw.get(open_node).get("window"),
             WAIT, what="the open node with its window")
    assert window_ids(cw.status()) == [open_node], cw.status()["windows"]
    assert window_ids(wait_status(cw)) == [open_node]


def test_tb2_r5_done_nodes_have_no_entry_even_one(cw):
    node = cw.simple("D", fx={"text": "ok"}, window=OPEN)
    cw.wait_state(node, "done", timeout=WAIT)
    assert node not in window_ids(cw.status())
    assert node not in window_ids(wait_status(cw))


def test_tb2_r5_cancelling_a_node_removes_its_entry(cw):
    node = cw.simple("O", window=CLOSED)
    cw.until(lambda: node in window_ids(cw.status()), WAIT, what="its entry")
    assert cw.cancel(node).get("ok") is True
    assert cw.get(node)["state"] == "cancelled"
    assert node not in window_ids(cw.status())
    assert node not in window_ids(wait_status(cw))


def test_tb2_r5_the_entry_of_an_open_node_keeps_its_window_fields(cw):
    node = cw.simple("O", window=CLOSED)
    cw.until(lambda: node in window_ids(cw.status()), WAIT, what="its entry")
    entry = [e for e in cw.status()["windows"] if e["node_id"] == node][0]
    assert entry.get("open") is False, entry
    assert entry.get("next_open"), entry


def test_tb2_r5_running_and_held_nodes_keep_their_entries_final_ones_lose_theirs(mw):
    running = mw.simple("R", fx={"gate": "gr"}, window=OPEN)
    held = mw.simple("H", fx={"text": ASK}, window=OPEN)
    mw.wait_running(running, timeout=WAIT)
    mw.wait_held(held, "needs_info", timeout=WAIT)
    done = mw.simple("D", fx={"text": "ok"}, window=OPEN)
    mw.wait_state(done, "done", timeout=WAIT)
    cancelled = mw.simple("X", fx={"gate": "gx"}, window=OPEN)
    mw.wait_running(cancelled, timeout=WAIT)
    assert mw.cancel(cancelled).get("ok") is True
    mw.wait_state(cancelled, "cancelled", timeout=WAIT)
    for ids in (window_ids(mw.status()), window_ids(wait_status(mw))):
        assert running in ids and held in ids, ids
        assert done not in ids and cancelled not in ids, ids
    mw.gate("gr")


def test_tb2_r5_a_node_leaves_the_list_when_it_ends(mw):
    node = mw.simple("R", fx={"gate": "gr"}, window=OPEN)
    mw.wait_running(node, timeout=WAIT)
    assert node in window_ids(mw.status())
    mw.gate("gr")
    mw.wait_state(node, "done", timeout=WAIT)
    assert node not in window_ids(mw.status())


def test_tb2_r5_with_only_final_nodes_the_list_is_empty(mw):
    node = mw.simple("D", fx={"text": "ok"})
    mw.wait_state(node, "done", timeout=WAIT)
    assert mw.status()["windows"] == []
    assert wait_status(mw)["windows"] == []
