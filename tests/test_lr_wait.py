"""LR-R4 — `wait_for_agents` reports a hold in `changed` only once.

Contract: `context/specs/loop-run-failed-resume.md` § LR-R4.

Driven the way the orchestrator drives it: the MCP tool `server.wait_for_agents`
called in-process (`call_tool`) against a real scheduler on fixture providers.
Two unrelated nodes are held (`needs_info`), a third is running behind a gate.

Where the contract is silent (kept loose): whether the first wait after a hold
appeared reports it is not asserted (an existing TB2-R7 test requires it);
only that a *later* wait with nothing new does not. Standing holds may appear
under `holds`; that key is not asserted.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT = 8
SHORT = 2        # `timeout` of a wait that must block for all of it
WATCH = 6        # `timeout` of a wait that something must cut short
SLACK = 0.4


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.agent("wk", "fxw", writes=True)
    world.start_scheduler()
    yield world
    world.gate("ga", world.fxw)          # never leave a gated run behind
    world.close()


def held_node(w, tag: str) -> str:
    n = w.simple(tag, "wk", fx={"text": f"NEED_INFO(q): QUESTION-{tag}?"})
    w.wait_held(n, "needs_info", timeout=WAIT)
    return n


def world_with_holds(w):
    holds = [held_node(w, "H1"), held_node(w, "H2")]
    running = w.simple("G", "wk", fx={"gate": "ga"})
    w.wait_running(running, timeout=WAIT)
    return holds, running


def changed_text(reply) -> str:
    return json.dumps(reply.get("changed", []))


def timed(fn):
    start = time.monotonic()
    reply = fn()
    return reply, time.monotonic() - start


def test_lr_r4_a_second_wait_on_a_running_node_does_not_relist_standing_holds(w):
    holds, running = world_with_holds(w)
    call_tool(w, "wait_for_agents", [running], timeout=1)     # may report them: they are new
    reply, took = timed(lambda: call_tool(w, "wait_for_agents", [running], timeout=SHORT))
    text = changed_text(reply)
    assert not any(h in text for h in holds), f"old holds came back in changed: {reply}"
    assert took >= SHORT - SLACK, f"the wait returned after {took:.1f}s instead of blocking"


def test_lr_r4_repeated_waits_stay_quiet(w):
    holds, running = world_with_holds(w)
    call_tool(w, "wait_for_agents", [running], timeout=1)
    for _ in range(2):
        reply, took = timed(lambda: call_tool(w, "wait_for_agents", [running], timeout=1))
        assert not any(h in changed_text(reply) for h in holds), reply
        assert took >= 1 - SLACK, f"returned after {took:.1f}s with nothing new"


def test_lr_r4_a_wait_without_ids_does_not_relist_standing_holds_either(w):
    holds, running = world_with_holds(w)
    call_tool(w, "wait_for_agents", None, timeout=1)
    reply, took = timed(lambda: call_tool(w, "wait_for_agents", None, timeout=SHORT))
    assert not any(h in changed_text(reply) for h in holds), reply
    assert took >= SHORT - SLACK, f"returned after {took:.1f}s with nothing new"


def test_lr_r4_a_wait_on_the_running_node_blocks_until_it_finishes(w):
    holds, running = world_with_holds(w)
    call_tool(w, "wait_for_agents", [running], timeout=1)
    box: dict = {}

    def waiter():
        box["reply"] = call_tool(w, "wait_for_agents", [running], timeout=WATCH)
        box["end"] = time.monotonic()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(1.0)
    released = time.monotonic()
    w.gate("ga", w.fxw)
    thread.join(WATCH + 2)
    assert not thread.is_alive(), "the wait never returned"
    assert box["end"] >= released, "the wait returned before the node finished"
    assert box["end"] - released < WATCH - 1, "the wait ran out its timeout instead of waking"
    assert box["reply"].get("timed_out") is not True, box["reply"]
    assert not any(h in changed_text(box["reply"]) for h in holds), box["reply"]
    w.wait_state(running, "done", timeout=WAIT)


def test_lr_r4_a_hold_that_appears_during_the_wait_is_reported_once(w):
    holds, running = world_with_holds(w)
    call_tool(w, "wait_for_agents", [running], timeout=1)
    box: dict = {}

    def waiter():
        box["reply"] = call_tool(w, "wait_for_agents", [running], timeout=WATCH)
        box["end"] = time.monotonic()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.5)
    fresh = held_node(w, "H3")
    thread.join(WATCH + 2)
    assert not thread.is_alive()
    assert fresh in changed_text(box["reply"]), f"a new hold was not reported: {box['reply']}"
    assert not any(h in changed_text(box["reply"]) for h in holds), box["reply"]
    again, took = timed(lambda: call_tool(w, "wait_for_agents", [running], timeout=1))
    assert fresh not in changed_text(again), f"the same hold was reported twice: {again}"
    assert took >= 1 - SLACK
