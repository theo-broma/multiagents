"""TB-R2 (tooling batch 2026-10, package A): `relaunch_node` works on a simple
node, through the scheduler RPC and through the MCP tool wrapper
(`server.relaunch_node`, called in-process as the orchestrator does).

Assumptions where the contract is silent (kept loose):
- a refusal is `ok: false` over the RPC and `{"error": ...}` from the tool; the
  error "names the parameter" = the parameter's name appears in the error JSON.
  Only `invalid` is asserted as the code for round controls on a simple node.
- "fresh session": the relaunched run is not started with `-s <session>`
  (the fixture's `resume`) and gets a session id different from the first run.
- "same branch": the relaunched run's commit lands on `refs/heads/nodes/<id>`.
- "a new generation": the node lists one more run than before and the new run
  has a run id of its own; the previous run directory still exists.
- not tested (contract silent): relaunching a simple node that is done with
  outcome `completed` (is that "non-approved"?), `pins`/`new_session` on a
  simple node, and what happens to a dependent that was waiting on the node.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, err_code, verdict_entry  # noqa: E402
from nc_fixture.world import call_tool, run_id_of  # noqa: E402

WAIT = 8
WAIT_LOOP = 15
QUIET = 2

ASK = "NEED_INFO(q): ASK-QUESTION-901?"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def held_simple(w) -> str:
    """A simple node held by TB-R1 (its first run ended asking for information)."""
    w.fxw.queue({"text": ASK, "write": {"first.txt": "1\n"}, "commit": "first run"})
    w.start_scheduler()
    n = w.simple("N", "wk", prose="TASK-PROSE-XYZ")
    w.wait_held(n, "needs_info", timeout=WAIT)
    return n


def failed_simple(w) -> str:
    w.fxw.queue({"exit": 1})
    w.start_scheduler()
    n = w.simple("N", "wk", prose="TASK-PROSE-XYZ")
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "failed"
    assert w.fxw.spawns() == 1
    return n


def runs_of(w, n: str) -> list:
    return list(w.get(n).get("runs") or [])


def run_ids(w, n: str) -> list[str]:
    return [r.get("run_id") or r.get("id") for r in runs_of(w, n)]


def tool_relaunch(w, n: str, **kw) -> dict:
    return call_tool(w, "relaunch_node", n, w.get(n)["revision"], **kw)


def refused(reply) -> bool:
    if isinstance(reply, dict) and "ok" in reply:
        return reply["ok"] is False
    return isinstance(reply, dict) and bool(reply.get("error"))


# ------------------------------------------------------ the RPC, held node

@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_a_held_simple_node_is_relaunched_as_a_fresh_run(w, make):
    n = make(w)
    first_run = run_ids(w, n)[-1]
    w.fxw.queue({"text": "answered, finished", "write": {"second.txt": "2\n"}, "commit": "second run"})
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok") is True, reply
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert w.fxw.spawns() == 2
    first, second = w.fxw.calls()
    assert second["resume"] is None, "the relaunch resumed the old session"
    assert second["session"] != first["session"]
    assert "-s" not in second["argv"]
    assert run_ids(w, n)[:1] == [first_run] and len(run_ids(w, n)) == 2
    assert len(set(run_ids(w, n))) == 2


@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_the_relaunched_run_has_the_same_task(w, make):
    n = make(w)
    w.fxw.queue({"text": "finished"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    w.wait_state(n, "done", timeout=WAIT)
    first, second = w.fxw.calls()
    assert "TASK-PROSE-XYZ" in first["prompt"] and "TASK-PROSE-XYZ" in second["prompt"]
    assert w.get(n)["task"].count("TASK-PROSE-XYZ") == 1


@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_the_relaunched_run_works_on_the_same_branch(w, make):
    n = make(w)
    w.fxw.queue({"text": "finished", "write": {"second.txt": "2\n"}, "commit": "second run"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    w.wait_state(n, "done", timeout=WAIT)
    assert w.show(f"refs/heads/nodes/{n}:second.txt") == "2\n"
    branches = w.git("for-each-ref", "--format=%(refname)", "refs/heads/nodes/").stdout.split()
    assert branches == [f"refs/heads/nodes/{n}"], "the relaunch used another branch"


@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_the_previous_run_directory_is_kept(w, make):
    n = make(w)
    first_run = run_ids(w, n)[-1]
    before = sorted(p.name for p in w.paths.run_dir(first_run).iterdir())
    assert before, "the first run left nothing to keep"
    w.fxw.queue({"text": "finished"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    w.wait_state(n, "done", timeout=WAIT)
    assert w.paths.run_dir(first_run).is_dir()
    assert sorted(p.name for p in w.paths.run_dir(first_run).iterdir()) == before
    second_run = run_ids(w, n)[-1]
    assert second_run != first_run and w.paths.run_dir(second_run).is_dir()


def test_tb_r2_a_relaunched_node_that_asks_again_is_held_again(w):
    n = held_simple(w)
    w.fxw.queue({"text": "NEED_INFO(q2): SECOND-ASK-902?"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    # held -> running -> held: wait for the second run to have ended
    w.until(lambda: w.fxw.spawns() == 2 and w.get(n)["state"] == "held"
            and "SECOND-ASK-902" in json.dumps(w.get(n)), WAIT, what="the second hold")
    got = w.get(n)
    assert got["hold"]["reason"] == "needs_info" and got["outcome"] is None
    assert "ASK-QUESTION-901" not in json.dumps(got["hold"]), "the old markers were not replaced"


# ----------------------------------------------------- the RPC, failed node

def test_tb_r2_a_failed_simple_node_is_relaunched(w):
    n = failed_simple(w)
    w.fxw.queue({"text": "recovered", "write": {"second.txt": "2\n"}, "commit": "retry"})
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok") is True, reply
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed"
    assert w.fxw.spawns() == 2
    assert w.fxw.calls()[1]["resume"] is None
    assert "TASK-PROSE-XYZ" in w.fxw.calls()[1]["prompt"]


def test_tb_r2_a_relaunched_failed_node_that_fails_again_can_be_relaunched_again(w):
    n = failed_simple(w)
    w.fxw.queue({"exit": 1})
    assert w.root_op("relaunch_node", n).get("ok") is True
    w.until(lambda: w.fxw.spawns() == 2 and w.get(n)["state"] == "done", WAIT, what="second failure")
    w.fxw.queue({"text": "third time lucky"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "completed"
    assert w.fxw.spawns() == 3


# --------------------------------------------------------------- refusals

def test_tb_r2_relaunching_a_running_simple_node_is_refused(w):
    w.fxw.queue({"gate": "g1"})
    w.start_scheduler()
    n = w.simple("N", "wk")
    w.wait_running(n, timeout=WAIT)
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok") is False, reply
    w.quiet(1)
    assert w.get(n)["state"] == "running" and w.fxw.spawns() == 1
    w.gate("g1", w.fxw)
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "completed"
    assert w.fxw.spawns() == 1


def test_tb_r2_relaunching_an_open_node_that_has_not_run_is_refused(w):
    w.start_scheduler()
    holder = w.hold_lock("L")
    n = w.simple("N", "wk", locks=["L"])
    w.until(lambda: w.get(n)["state"] == "open", WAIT, what="N open")
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok") is False, reply
    assert w.get(n)["state"] == "open"
    w.gate("holder")
    w.wait_state(holder, "done", timeout=WAIT)


@pytest.mark.parametrize("controls", [{"max_rounds": 3}, {"max_rounds": 1}, {"retry": "round"},
                                      {"retry": "verdict_child"}])
@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_round_controls_on_a_simple_node_are_refused_naming_the_parameter(w, controls, make):
    n = make(w)
    state = w.get(n)["state"]
    before = w.get(n)
    reply = w.root_op("relaunch_node", n, **controls)
    assert reply.get("ok") is False, reply
    assert err_code(reply) == "invalid", reply
    name = next(iter(controls))
    assert name in json.dumps(reply), f"the error does not name {name!r}: {reply}"
    w.quiet(1)
    after = w.get(n)
    assert after["state"] == state and after["revision"] == before["revision"]
    assert w.fxw.spawns() == 1, "a refused relaunch launched a run"


def test_tb_r2_a_stale_revision_is_a_conflict_and_launches_nothing(w):
    n = failed_simple(w)
    rev = w.get(n)["revision"]
    reply = w.rpc("relaunch_node", {"id": n, "revision": rev - 1})
    assert err_code(reply) == "conflict", reply
    w.quiet(1)
    assert w.fxw.spawns() == 1 and w.get(n)["state"] == "done"


def test_tb_r2_relaunch_is_root_only(w):
    n = failed_simple(w)
    from multiagents.scheduler import issue_run_capability
    other = issue_run_capability(w.root, "some-run", n, {"read", "delegate"})
    reply = w.rpc("relaunch_node", {"id": n, "revision": w.get(n)["revision"]}, token=other)
    assert err_code(reply) == "forbidden", reply
    w.quiet(1)
    assert w.get(n)["state"] == "done" and w.fxw.spawns() == 1


def test_tb_r2_the_same_request_twice_launches_one_run(w):
    n = failed_simple(w)
    w.fxw.queue({"text": "finished"})
    rev = w.get(n)["revision"]
    first = w.rpc("relaunch_node", {"id": n, "revision": rev}, request_id="tb-r2-retry-1")
    second = w.rpc("relaunch_node", {"id": n, "revision": rev}, request_id="tb-r2-retry-1")
    assert first.get("ok") is True and second == first
    w.wait_state(n, "done", timeout=WAIT)
    w.quiet(1)
    assert w.fxw.spawns() == 2


def test_tb_r2_a_second_relaunch_while_the_first_run_is_active_is_refused(w):
    n = failed_simple(w)
    w.fxw.queue({"gate": "g2", "text": "finished"})
    assert w.root_op("relaunch_node", n).get("ok") is True
    w.wait_running(n, timeout=WAIT)
    again = w.root_op("relaunch_node", n)
    assert again.get("ok") is False, again
    w.gate("g2", w.fxw)
    w.wait_state(n, "done", timeout=WAIT)
    assert w.fxw.spawns() == 2


# -------------------------------------------------- the MCP tool wrapper

def test_tb_r2_the_tool_relaunches_a_held_simple_node_without_round_controls(w):
    n = held_simple(w)
    w.fxw.queue({"text": "finished", "write": {"second.txt": "2\n"}, "commit": "second run"})
    res = tool_relaunch(w, n)
    assert not refused(res), res
    done = w.wait_state(n, "done", timeout=WAIT)
    assert done["outcome"] == "completed" and w.fxw.spawns() == 2
    assert w.fxw.calls()[1]["resume"] is None


def test_tb_r2_the_tool_relaunches_a_failed_simple_node(w):
    n = failed_simple(w)
    w.fxw.queue({"text": "recovered"})
    res = tool_relaunch(w, n)
    assert not refused(res), res
    assert w.wait_state(n, "done", timeout=WAIT)["outcome"] == "completed"
    assert w.fxw.spawns() == 2


@pytest.mark.parametrize("controls,name", [({"max_rounds": 3}, "max_rounds"),
                                           ({"retry": "round"}, "retry"),
                                           ({"retry": "verdict_child"}, "retry")])
@pytest.mark.parametrize("make", [held_simple, failed_simple], ids=["held", "failed"])
def test_tb_r2_the_tool_still_refuses_round_controls_on_a_simple_node_naming_the_parameter(w, controls, name, make):
    n = make(w)
    state = w.get(n)["state"]
    res = tool_relaunch(w, n, **controls)
    assert refused(res), res
    assert name in json.dumps(res), f"the error does not name {name!r}: {res}"
    w.quiet(1)
    assert w.get(n)["state"] == state and w.fxw.spawns() == 1


def test_tb_r2_the_tool_does_not_send_a_loop_retry_default_a_loop_at_its_maximum_reruns_the_round(w):
    """With no `retry` the scheduler's own default applies: a loop held at
    `loop_max` runs the whole round again (worker included). The wrapper used
    to force `retry=verdict_child`, which reran only the reviewer."""
    w.fxw.queue(commit_entry("f1.txt", "1\n", "r1"), commit_entry("f2.txt", "2\n", "r2"),
                commit_entry("f3.txt", "3\n", "r3"))
    w.fxr.queue(verdict_entry("rejected", [{"summary": "no", "severity": "major"}]),
                verdict_entry("rejected", [{"summary": "no again", "severity": "major"}]),
                verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(2)
    w.wait_held(loop, "loop_max", timeout=WAIT_LOOP)
    assert w.fxw.spawns() == 2 and w.fxr.spawns() == 2
    res = call_tool(w, "relaunch_node", loop, w.get(loop)["revision"], max_rounds=4)
    assert not refused(res), res
    done = w.wait_state(loop, "done", timeout=WAIT_LOOP)
    assert done["outcome"] == "approved"
    assert w.fxw.spawns() == 3, "the worker was not rerun: the wrapper still forces a retry mode"
    assert w.fxr.spawns() == 3


def test_tb_r2_the_tool_still_passes_an_explicit_retry_to_a_loop(w):
    w.fxw.queue(commit_entry("f1.txt", "1\n", "r1"))
    w.fxr.queue({"text": "no verdict"}, verdict_entry("approved"))
    w.start_scheduler()
    loop, wk, rv = w.mkloop(3)
    w.wait_held(loop, "unresolved_round", timeout=WAIT_LOOP)
    res = call_tool(w, "relaunch_node", loop, w.get(loop)["revision"], retry="verdict_child")
    assert not refused(res), res
    assert w.wait_state(loop, "done", timeout=WAIT_LOOP)["outcome"] == "approved"
    assert w.fxw.spawns() == 1 and w.fxr.spawns() == 2
