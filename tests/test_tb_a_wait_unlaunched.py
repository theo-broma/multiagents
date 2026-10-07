"""TB-R3 (tooling batch 2026-10, package A): `wait_for_agents()` without ids
also covers the caller-created nodes that are open and have no run yet.

Driven through the MCP tool `server.wait_for_agents` called in-process as the
orchestrator does (`nc_fixture.world.call_tool`), against a real scheduler with
fixture runs. A wait that is expected to be in flight while the test acts runs
in a thread, joined with a bound.

Assumptions where the contract is silent (kept loose):
- the result's shape is not pinned beyond what exists today (`changed`,
  `still_running`, `reason`); "reported" means the node id or its run id appears
  in the result JSON, "pending, with their blocked reason" means the node id
  and the blocked code (`lock` / `dependency`) appear in the JSON of the timeout
  result.
- a node created with another principal's capability (a subagent's token) is
  not "caller-created" for the root's wait.
- the in-flight tests need the wait to have taken its snapshot before the test
  acts; there is no observable for that, so they give the call QUIET seconds
  of head start. A correct implementation is not hurt by acting early (the
  node then simply has a run already); a missing one fails at its assertion.
- the tests of "launches and finishes during the wait" and "parks" use a
  dependent of a node held by TB-R1 (`needs_info`) as the run-less open node,
  because it is the only run-less blocked node that no running run keeps
  covered; they are therefore red until R1 is also implemented.
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
HEAD_START = 1.0     # the in-flight wait's time to take its snapshot
WAIT_TIMEOUT = 4     # `timeout` of a wait that must not run out
ASK = "NEED_INFO(q): ASK-QUESTION-903?"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.agent("wk", "fxw", writes=True)
    world.start_scheduler()
    yield world
    world.close()


class InFlight:
    """`wait_for_agents()` running in a thread."""

    def __init__(self, w, timeout: int = WAIT_TIMEOUT, ids=None):
        self.box: dict = {}
        self.t = threading.Thread(target=self._run, args=(w, timeout, ids), daemon=True)
        self.t.start()

    def _run(self, w, timeout, ids):
        try:
            self.box["result"] = call_tool(w, "wait_for_agents", ids, timeout=timeout)
        except BaseException as exc:      # noqa: BLE001 - reported by the test
            self.box["error"] = exc

    def settle(self, w) -> None:
        w.quiet(HEAD_START)

    def returned(self, within: float) -> bool:
        self.t.join(within)
        return not self.t.is_alive()

    def result(self, within: float = WAIT) -> dict:
        assert self.returned(within), "the wait did not return"
        assert "error" not in self.box, self.box.get("error")
        return self.box["result"]


def blob(x) -> str:
    return json.dumps(x, default=str)


def run_ids(w, node: str) -> list[str]:
    return [r.get("run_id") or r.get("id") for r in (w.get(node).get("runs") or [])]


def mentions(w, result: dict, node: str) -> bool:
    text = blob(result)
    return node in text or any(r in text for r in run_ids(w, node))


def lock_blocked_node(w, **fx):
    """H runs gated holding lock L; B (no run) is open, blocked by the lock."""
    holder = w.hold_lock("L")
    b = w.simple("B", "wk", locks=["L"], fx=fx)
    w.until(lambda: "lock" in [x["code"] for x in w.get(b).get("blocked") or []],
            WAIT, what="B blocked by the lock")
    assert w.get(b)["state"] == "open" and not w.get(b).get("runs")
    return holder, b


def dep(node: str) -> dict:
    return {"node": node, "require": "success"}


def held_with_dependent(w, **fx):
    """A is held (TB-R1) and has a run; B depends on A, is open and has no run."""
    a = w.simple("A", "wk", fx={"text": ASK})
    b = w.simple("B", "wk", depends_on=[dep(a)], fx=fx)
    w.wait_held(a, "needs_info", timeout=WAIT)
    first = call_tool(w, "wait_for_agents", None, timeout=1)   # reports A, once
    assert first is not None
    return a, b


# ------------------------------------------------------ today's behaviour

def test_tb_r3_with_nothing_running_and_nothing_pending_it_answers_at_once(w):
    res = call_tool(w, "wait_for_agents", None, timeout=WAIT_TIMEOUT)
    assert res.get("changed") == [] and "no active agents" in blob(res)


def test_tb_r3_a_wait_on_a_node_id_with_no_run_is_still_answered_at_once(w):
    holder, b = lock_blocked_node(w)
    res = call_tool(w, "wait_for_agents", [b], timeout=WAIT_TIMEOUT)
    assert b in blob(res.get("no_run_yet") or []), res
    w.gate("holder")


def test_tb_r3_the_caller_runs_are_still_covered(w):
    h = w.simple("H", "wk", fx={"gate": "gh", "text": "done"})
    w.wait_running(h, timeout=WAIT)
    wait = InFlight(w)
    wait.settle(w)
    assert not wait.returned(0), "returned while the only run was still gated"
    w.gate("gh", w.fxw)
    assert mentions(w, wait.result(), h)


# ----------------------------------------------- launches and finishes

def test_tb_r3_an_open_node_that_launches_and_finishes_during_the_wait_returns_it(w):
    a, b = held_with_dependent(w, gate="gb", text="B finished")
    wait = InFlight(w)
    wait.settle(w)
    assert not wait.returned(0), "returned although nothing finished"
    assert w.root_op("close_node", a, outcome="approved").get("ok") is True
    w.wait_running(b, timeout=WAIT)
    assert not wait.returned(1), "a launch alone returned the wait"
    w.gate("gb", w.fxw)
    res = wait.result()
    assert mentions(w, res, b), res
    assert w.get(b)["outcome"] == "completed"


def test_tb_r3_a_launch_alone_does_not_return_the_wait(w):
    a, b = held_with_dependent(w, gate="gb", text="B finished")
    wait = InFlight(w, timeout=4)
    wait.settle(w)
    assert w.root_op("close_node", a, outcome="approved").get("ok") is True
    w.wait_running(b, timeout=WAIT)
    # B is running and gated: nothing covered has finished, parked or been cancelled
    assert not wait.returned(1.5), "the launch of a snapshot node returned the wait"
    res = wait.result(within=8)           # runs out its own timeout
    assert b in blob(res) or any(r in blob(res) for r in run_ids(w, b))
    w.gate("gb", w.fxw)


def test_tb_r3_a_snapshot_node_that_launches_and_ends_held_parks_and_returns(w):
    a, b = held_with_dependent(w, gate="gb", text=ASK)
    wait = InFlight(w)
    wait.settle(w)
    assert w.root_op("close_node", a, outcome="approved").get("ok") is True
    w.wait_running(b, timeout=WAIT)
    assert not wait.returned(0.5)
    w.gate("gb", w.fxw)
    res = wait.result()
    assert mentions(w, res, b), res
    assert w.get(b)["state"] == "held"


# ------------------------------------------------------------ cancelled

def test_tb_r3_a_snapshot_node_cancelled_during_the_wait_returns_the_wait(w):
    holder, b = lock_blocked_node(w)
    wait = InFlight(w)
    wait.settle(w)
    assert not wait.returned(0), "returned while the holder's run is gated and B is pending"
    assert w.cancel(b).get("ok") is True
    res = wait.result(within=WAIT)
    assert b in blob(res), res
    assert w.get(b)["state"] == "cancelled"
    assert w.get(holder)["state"] == "running", "the holder finished: the return was not caused by B"
    w.gate("holder")


def test_tb_r3_a_node_cancelled_before_the_call_is_not_in_the_snapshot(w):
    holder, b = lock_blocked_node(w)
    assert w.cancel(b).get("ok") is True
    res = call_tool(w, "wait_for_agents", None, timeout=2)
    assert b not in blob(res), res
    w.gate("holder")


# -------------------------------------------------------------- timeout

def test_tb_r3_on_timeout_it_returns_as_today_and_lists_the_pending_node_with_its_blocked_reason(w):
    holder, b = lock_blocked_node(w)
    res = call_tool(w, "wait_for_agents", None, timeout=1)
    assert res.get("changed") == [], "nothing covered finished, parked or was cancelled"
    text = blob(res)
    assert b in text, f"the pending node is not listed: {res}"
    assert "lock" in text[text.index(b):] or "lock" in text, "its blocked reason is missing"
    # as today: the running holder is still listed as running
    assert mentions(w, {"x": res.get("still_running")}, holder)
    assert w.get(b)["state"] == "open" and not w.get(b).get("runs")
    w.gate("holder")


def test_tb_r3_a_timeout_with_the_node_blocked_by_a_dependency_names_that_reason(w):
    a, b = held_with_dependent(w)
    res = call_tool(w, "wait_for_agents", None, timeout=1)
    text = blob(res)
    assert b in text and "dependency" in text, res


def test_tb_r3_a_pending_node_that_has_since_launched_is_not_listed_as_pending(w):
    holder, b = lock_blocked_node(w, gate="gb")
    wait = InFlight(w, timeout=3)
    wait.settle(w)
    w.gate("holder")                      # H finishes: the wait returns on it
    first = wait.result()
    assert mentions(w, first, holder)
    w.wait_running(b, timeout=WAIT)
    again = call_tool(w, "wait_for_agents", None, timeout=1)
    assert b not in blob(again.get("changed")) and b not in blob(again.get("still_running")), again
    assert not [k for k, v in again.items() if k not in ("capacity", "node_runs") and b in blob(v)], again
    w.gate("gb", w.fxw)


# ----------------------------------------------------- the snapshot's edges

def test_tb_r3_nodes_created_during_the_wait_do_not_join_it(w):
    holder, b = lock_blocked_node(w)
    wait = InFlight(w, timeout=3)
    wait.settle(w)
    late = w.simple("LATE", "wk", locks=["L"])
    assert w.cancel(late).get("ok") is True
    assert not wait.returned(1), "a node created after the call began returned the wait"
    res = wait.result(within=8)           # its own timeout
    assert late not in blob(res), "a late node was listed"
    assert b in blob(res)
    w.gate("holder")


def test_tb_r3_a_node_created_by_another_caller_is_not_covered(w):
    from multiagents.scheduler import issue_run_capability
    holder, b = lock_blocked_node(w)
    other = issue_run_capability(w.root, "some-run", holder, {"read", "delegate"})
    foreign = w.simple("F", "wk", token=other, locks=["L"])
    res = call_tool(w, "wait_for_agents", None, timeout=1)
    assert foreign not in blob(res), "a node another caller created was covered"
    assert b in blob(res)
    w.gate("holder")


def test_tb_r3_the_wait_launches_nothing_by_itself(w):
    holder, b = lock_blocked_node(w)
    call_tool(w, "wait_for_agents", None, timeout=1)
    assert w.get(b)["state"] == "open" and w.fxw.by_tag("B") == []
    w.gate("holder")
