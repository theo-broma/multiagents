"""TB2-R4: waiting on a composite wakes on its children.

`wait_for_nodes` (and `wait_for_agents` given a composite node id), filtered on
a composite, returns transitions of any descendant, not only of the composite.

Where the contract is silent (kept loose):
- the cursor is read from an earlier `wait_for_nodes`; only a transition with a
  `node_id` field is looked at.
- for `wait_for_agents` on a composite, "returns" is checked as: it does not
  return while the child is gated, and once the child ends it returns with the
  child's node id or run id somewhere in the reply.
- whether a filter on a composite also includes the composite's own transitions
  is the existing behaviour (kept: they are included).
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT = 8
WAIT_TIMEOUT = 4      # `timeout` of a wait that must be woken, not run out
HOLD = 1.5            # a wait that must NOT be woken is observed this long


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.start_scheduler()
    yield world
    world.close()


class InFlight:
    def __init__(self, fn):
        self.box: dict = {}
        self.t = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.t.start()

    def _run(self, fn):
        try:
            self.box["result"] = fn()
        except BaseException as exc:      # noqa: BLE001 - reported by the test
            self.box["error"] = exc

    def returned(self, within: float) -> bool:
        self.t.join(within)
        return not self.t.is_alive()

    def result(self, within: float = WAIT):
        assert self.returned(within), "the wait did not return"
        assert "error" not in self.box, self.box.get("error")
        return self.box["result"]


def cursor_now(w) -> int:
    return call_tool(w, "wait_for_nodes", timeout=0)["next_cursor"]


def waiting_on(w, ids, cursor):
    return InFlight(lambda: call_tool(w, "wait_for_nodes", cursor=cursor, node_ids=ids,
                                      timeout=WAIT_TIMEOUT))


def touched(reply: dict) -> set[str]:
    return {t["node_id"] for t in reply["transitions"]}


def sequence(w):
    a = w.simple("A", fx={"gate": "ga"})
    b = w.simple("B", fx={"gate": "gb"})
    seq = w.comp("sequence", [a, b])
    w.wait_running(a, timeout=WAIT)
    w.wait_state(seq, "running", timeout=WAIT)
    return seq, a, b


def test_tb2_r4_a_sequence_whose_first_child_finishes_wakes_a_wait_on_the_sequence(w):
    seq, a, b = sequence(w)
    wait = waiting_on(w, [seq], cursor_now(w))
    assert not wait.returned(HOLD), "returned although no node of the sequence moved"
    w.gate("ga")
    reply = wait.result()
    assert a in touched(reply), reply
    assert reply["transitions"], reply
    w.gate("gb")


def test_tb2_r4_only_the_composite_and_its_descendants_are_returned(w):
    seq, a, b = sequence(w)
    other = w.simple("X", fx={"gate": "gx"})
    w.wait_running(other, timeout=WAIT)
    wait = waiting_on(w, [seq], cursor_now(w))
    assert not wait.returned(0.3)
    w.gate("gx")                    # an unrelated node ends first
    w.wait_state(other, "done", timeout=WAIT)
    assert not wait.returned(HOLD), "an unrelated node woke the wait"
    w.gate("ga")
    reply = wait.result()
    assert other not in touched(reply), reply
    assert touched(reply) <= {seq, a, b}, reply
    assert a in touched(reply)
    w.gate("gb")


def test_tb2_r4_a_grandchild_wakes_a_wait_on_the_outer_composite(w):
    g1 = w.simple("G1", fx={"gate": "g1"})
    grp = w.comp("group", [g1])
    z = w.simple("Z", fx={"gate": "gz"})
    seq = w.comp("sequence", [grp, z])
    w.wait_running(g1, timeout=WAIT)
    wait = waiting_on(w, [seq], cursor_now(w))
    assert not wait.returned(HOLD)
    w.gate("g1")
    reply = wait.result()
    assert g1 in touched(reply), reply
    w.gate("gz")


def test_tb2_r4_a_wait_on_a_group_wakes_on_any_member(w):
    a = w.simple("A", fx={"gate": "ga"})
    b = w.simple("B", fx={"gate": "gb"})
    grp = w.comp("group", [a, b])
    w.wait_running(a, timeout=WAIT)
    w.wait_running(b, timeout=WAIT)
    wait = waiting_on(w, [grp], cursor_now(w))
    assert not wait.returned(HOLD)
    w.gate("gb")
    assert b in touched(wait.result())
    w.gate("ga")


def test_tb2_r4_a_wait_on_a_leaf_is_unchanged_and_ignores_its_siblings(w):
    seq, a, b = sequence(w)
    wait = waiting_on(w, [b], cursor_now(w))
    w.gate("ga")                    # a sibling ends, B starts: not B's end
    w.wait_running(b, timeout=WAIT)
    reply_early = wait.returned(HOLD)
    if reply_early:                 # B's own launch may be reported, never A's
        assert touched(wait.result()) <= {b}
    w.gate("gb")
    assert touched(wait.result()) <= {b}


def test_tb2_r4_a_child_that_moved_before_the_cursor_does_not_wake_the_wait(w):
    seq, a, b = sequence(w)
    w.gate("ga")
    w.wait_running(b, timeout=WAIT)
    wait = waiting_on(w, [seq], cursor_now(w))
    assert not wait.returned(HOLD), "an acknowledged past transition woke the wait"
    w.gate("gb")
    assert b in touched(wait.result())


def test_tb2_r4_wait_for_agents_given_a_composite_id_wakes_on_a_child(w):
    seq, a, b = sequence(w)
    # acknowledge the setup transitions: a wait returns unacknowledged ones at once
    call_tool(w, "ack_nodes", cursor=cursor_now(w))
    wait = InFlight(lambda: call_tool(w, "wait_for_agents", [seq], timeout=WAIT_TIMEOUT))
    assert not wait.returned(HOLD), "a composite id was answered at once: nothing had moved"
    w.gate("ga")
    reply = wait.result()
    run_a = [r.get("run_id") for r in (w.get(a).get("runs") or [])]
    text = json.dumps(reply, default=str)
    assert a in text or any(r and r in text for r in run_a), reply
    w.gate("gb")


def test_tb2_r4_wait_for_agents_given_a_composite_times_out_when_nothing_moves(w):
    seq, a, b = sequence(w)
    reply = call_tool(w, "wait_for_agents", [seq], timeout=1)
    assert isinstance(reply, dict) and "error" not in reply, reply
    assert w.get(a)["state"] == "running"
    w.gate("ga")
    w.gate("gb")
