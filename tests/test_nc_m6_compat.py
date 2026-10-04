"""M6 — compatibility tools: NC-R21 in full (beyond the minimal NC-R51 part that
`tests/test_nc_m2_admission.py` already covers) and NC-R48 (gate off: no
regression, no scheduler state).

Already covered elsewhere and NOT repeated here: `start_agent` returning
`{node_id, agent_id, status, blocked}`, the saturated case and the
`scheduler_unavailable` case (test_nc_m2_admission.py, NC-R21/NC-R51);
`steer_agent` / `stop_agent` on a suspended node's run (test_nc_m5_suspend.py).

Assumptions where the contract is silent (kept loose):
- the error for a run tool given a node id is exactly
  `{"error": "node_id", "hint": "use get_node"}` (NC-R21) -- the hint is
  asserted to *mention* `get_node`, not to be byte-equal.
- `wait_for_agents([node_id])` for a node with a run reports that run (its id
  appears in the result) once it finishes; for a node with none the result
  contains the word `no_run_yet`, anywhere in the reply.
- `start_agent(..., request_id=...)` (NC-R83): the same id twice returns the
  same `node_id` (and the same `agent_id`), and creates one node, one run.
- with the gate off a node tool answers `scheduler_disabled` (NC-R1), and the
  monitor's `/api/state` / the tree carry no scheduler section.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import World, call_tool, err_code  # noqa: E402
from multiagents.paths import state_root  # noqa: E402

RUN_TOOLS = [
    ("check_agent", ()),
    ("collect_agent", ()),
    ("steer_agent", ("hello",)),
    ("stop_agent", ()),
    ("merge_agent", ()),
    ("discard_agent", ()),
]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    yield world
    world.close()


@pytest.fixture
def goff(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch, gate=False)
    world.write_config()
    yield world
    world.close()


def started(w, tag="S", **kw):
    r = call_tool(w, "start_agent", "worker", task(tag, **kw))
    assert not r.get("error"), r
    return r


# ------------------------------------------------- run tools given a node id

@pytest.mark.parametrize("tool,extra", RUN_TOOLS)
def test_nc_r21_a_run_tool_given_a_node_id_answers_node_id_with_a_hint(w, tool, extra):
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    out = call_tool(w, tool, r["node_id"], *extra)
    assert out.get("error") == "node_id", out
    assert "get_node" in str(out.get("hint")), out


@pytest.mark.parametrize("tool,extra", RUN_TOOLS)
def test_nc_r21_a_refused_node_id_has_no_side_effect(w, tool, extra):
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    before = (w.get(r["node_id"]), len(w.fx.calls()), w.tree_json())
    call_tool(w, tool, r["node_id"], *extra)
    w.quiet(1)
    after = w.get(r["node_id"])
    assert after["state"] == "running" and after["revision"] == before[0]["revision"]
    assert len(w.fx.calls()) == before[1]
    assert {k: v["status"] for k, v in w.tree_nodes().items()} == \
        {k: v["status"] for k, v in before[2]["nodes"].items()}


def test_nc_r21_the_same_tools_still_accept_the_run_id(w):
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    out = call_tool(w, "check_agent", r["agent_id"])
    assert not out.get("error"), out
    w.gate("g1")
    w.wait_state(r["node_id"], "done")
    out = call_tool(w, "collect_agent", r["agent_id"])
    assert not out.get("error"), out


def test_nc_r21_an_unknown_node_shaped_id_is_not_mistaken_for_a_known_node(w):
    w.start_scheduler()
    out = call_tool(w, "check_agent", "nd-deadbeef")
    assert out.get("error") and out.get("error") != "scheduler_unavailable"
    assert w.list() == []


# ------------------------------------------------------- wait_for_agents

def test_nc_r21_waiting_on_a_node_id_waits_for_its_current_run(w):
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    w.gate("g1")
    out = call_tool(w, "wait_for_agents", [r["node_id"]], timeout=30)
    assert not out.get("error"), out
    assert r["agent_id"] in json.dumps(out)
    assert w.get(r["node_id"])["state"] in ("running", "done")
    w.wait_state(r["node_id"], "done")


def test_nc_r21_waiting_on_a_node_id_returns_when_that_run_ends_not_before(w):
    import time
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    t0 = time.monotonic()
    out = call_tool(w, "wait_for_agents", [r["node_id"]], timeout=2)
    assert time.monotonic() - t0 >= 1.5, "returned although the run was still going"
    assert w.get(r["node_id"])["state"] == "running"
    assert not out.get("error"), out


def test_nc_r21_a_node_without_a_run_answers_no_run_yet_at_once(w):
    import time
    w.start_scheduler()
    hold = w.simple("H", "pcworker", fx={"gate": "gH"})
    w.wait_running(hold)
    blocked = call_tool(w, "start_agent", "pcworker", task("B"))
    assert blocked["node_id"] and not blocked.get("agent_id")
    t0 = time.monotonic()
    out = call_tool(w, "wait_for_agents", [blocked["node_id"]], timeout=20)
    assert "no_run_yet" in json.dumps(out), out
    assert time.monotonic() - t0 < 10, "a node with no run must not be waited for"
    assert w.pc.by_tag("B") == []


def test_nc_r21_a_mixed_list_of_run_and_node_ids_is_accepted(w):
    w.start_scheduler()
    a = started(w, "A", gate="gA")
    b = started(w, "B", gate="gB")
    w.wait_running(a["node_id"])
    w.wait_running(b["node_id"])
    w.gate("gA")
    out = call_tool(w, "wait_for_agents", [a["agent_id"], b["node_id"]], timeout=30)
    assert not out.get("error"), out
    assert a["agent_id"] in json.dumps(out)
    w.gate("gB")


def test_nc_r21_waiting_never_follows_the_next_run_of_a_node(w):
    """A node's *current* run only: a node relaunched after `done` is a new run
    and the earlier wait has already returned."""
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    w.gate("g1")
    out = call_tool(w, "wait_for_agents", [r["node_id"]], timeout=30)
    assert r["agent_id"] in json.dumps(out)
    done = w.wait_state(r["node_id"], "done")
    again = call_tool(w, "wait_for_agents", [r["node_id"]], timeout=2)
    assert not again.get("error"), again
    assert len(w.get(r["node_id"])["runs"]) == len(done["runs"]) == 1


# -------------------------------------------------- agent_tree / node_id

def test_nc_r21_agent_tree_counts_and_lists_runs_not_plan_nodes(w):
    w.start_scheduler()
    hold = w.simple("H", "pcworker", fx={"gate": "gH"})
    w.wait_running(hold)
    w.simple("B", "pcworker")                    # open, no run
    w.simple("C", "pcworker")
    w.quiet(2)
    tree = call_tool(w, "agent_tree")
    assert tree["nodes"] == 1, tree
    assert [a["agent_id"] for a in tree["active"]] == [
        w.get(hold)["runs"][0]["run_id"]]
    assert all(a["agent_id"].startswith("ag-") for a in tree["active"])
    assert "nd-" not in json.dumps(tree["active"])


def test_nc_r21_agent_tree_ids_are_run_ids_after_start_agent(w):
    w.start_scheduler()
    r = started(w, gate="g1")
    w.wait_running(r["node_id"])
    tree = call_tool(w, "agent_tree")
    assert [a["agent_id"] for a in tree["active"]] == [r["agent_id"]]


# ----------------------------------------------------------- request_id

def test_nc_r83_a_retry_with_the_same_request_id_creates_no_second_node(w):
    w.start_scheduler()
    first = call_tool(w, "start_agent", "worker", task("R", gate="gR"), request_id="req-1")
    again = call_tool(w, "start_agent", "worker", task("R", gate="gR"), request_id="req-1")
    assert not first.get("error") and not again.get("error"), (first, again)
    assert again["node_id"] == first["node_id"]
    assert again.get("agent_id") == first.get("agent_id")
    w.wait_running(first["node_id"])
    assert len(w.list()) == 1
    assert len(w.fx.by_tag("R")) == 1


def test_nc_r83_a_retry_after_the_run_finished_still_returns_the_original_reply(w):
    w.start_scheduler()
    first = call_tool(w, "start_agent", "worker", task("R"), request_id="req-2")
    w.wait_state(first["node_id"], "done")
    again = call_tool(w, "start_agent", "worker", task("R"), request_id="req-2")
    assert again["node_id"] == first["node_id"]
    assert len(w.list()) == 1 and len(w.fx.by_tag("R")) == 1


def test_nc_r83_a_retry_survives_a_scheduler_restart(w):
    w.start_scheduler()
    first = call_tool(w, "start_agent", "worker", task("R", gate="gR"), request_id="req-3")
    w.wait_running(first["node_id"])
    w.restart_scheduler()
    again = call_tool(w, "start_agent", "worker", task("R", gate="gR"), request_id="req-3")
    assert again["node_id"] == first["node_id"]
    assert len(w.list()) == 1 and len(w.fx.by_tag("R")) == 1


def test_nc_r83_two_different_request_ids_are_two_nodes(w):
    w.start_scheduler()
    a = call_tool(w, "start_agent", "worker", task("A", gate="gA"), request_id="x-1")
    b = call_tool(w, "start_agent", "worker", task("A", gate="gA"), request_id="x-2")
    assert a["node_id"] != b["node_id"]
    assert len(w.list()) == 2


def test_nc_r83_the_same_request_id_with_another_task_is_refused_not_merged(w):
    w.start_scheduler()
    first = call_tool(w, "start_agent", "worker", task("A", gate="gA"), request_id="x-3")
    other = call_tool(w, "start_agent", "worker", task("OTHER"), request_id="x-3")
    assert other.get("error") == "request_id_reused", other
    assert len(w.list()) == 1 and w.fx.by_tag("OTHER") == []
    assert first["node_id"]


def test_nc_r83_without_a_request_id_each_call_is_its_own_node(w):
    w.start_scheduler()
    a = call_tool(w, "start_agent", "worker", task("A", gate="gA"))
    b = call_tool(w, "start_agent", "worker", task("A", gate="gA"))
    assert a["node_id"] != b["node_id"]


# ---------------------------------------------------------------- NC-R48

def scheduler_traces(w: World) -> list[str]:
    out = []
    for sub in ("scheduler", "scheduler-rpc"):
        base = state_root() / sub
        if base.exists():
            out += [str(p) for p in base.rglob("*")]
    return out


def drive(world: World, scenario):
    """Run `scenario(call)` in ONE event loop, the way a real MCP server lives:
    today's runs are supervised by the loop that started them, so a loop per
    call (what `call_tool` does) would end every run between two calls.
    `call(name, *a, **kw)` awaits a tool of `multiagents.server` as root."""
    import asyncio
    import inspect
    from multiagents import server
    server._reset()

    async def call(name, *args, **kwargs):
        out = getattr(server, name)(*args, **kwargs)
        return await out if inspect.isawaitable(out) else out

    async def main():
        return await scenario(call)

    try:
        return asyncio.run(main())
    finally:
        server._reset()


def commit_config(world: GitWorld) -> None:
    """Config written, and the runtime files kept out of `git status` so that
    today's merge (which wants a clean target worktree) can run."""
    world.write_config()
    with open(world.root / ".git" / "info" / "exclude", "a") as f:
        f.write(".multiagents/\n")


def test_nc_r48_gate_off_runs_work_the_legacy_way_and_create_no_scheduler_state(goff):
    commit_config(goff)

    async def scenario(call):
        r = await call("start_agent", "coder", task("L1", write={"l.txt": "x"}, commit="l1"))
        assert r.get("agent_id") and "node_id" not in r and "blocked" not in r, r
        out = await call("wait_for_agents", [r["agent_id"]], timeout=60)
        assert not out.get("error"), out
        got = await call("collect_agent", r["agent_id"])
        assert not got.get("error"), got
        tree = await call("agent_tree")
        assert tree["nodes"] == 1
        merged = await call("merge_agent", r["agent_id"])
        assert merged.get("result") != "failed", merged
        return r

    drive(goff, scenario)
    assert "l.txt" in goff.files(goff.base)
    assert scheduler_traces(goff) == []
    assert not goff.sock.exists()
    assert not [e for e in goff.events() if goff.kind_of(e).startswith("node.")]


def test_nc_r48_gate_off_steer_stop_and_discard_are_unchanged(goff):
    import asyncio
    commit_config(goff)

    async def scenario(call):
        r = await call("start_agent", "worker", task("L2", session="ses_l2", gate="gL2"))
        assert r.get("agent_id") and "node_id" not in r, r
        for _ in range(100):                    # until its session id is captured
            out = await call("steer_agent", r["agent_id"], "new direction")
            if not out.get("error"):
                break
            await asyncio.sleep(0.2)
        assert not out.get("error"), out
        stopped = await call("stop_agent", r["agent_id"])
        assert not stopped.get("error"), stopped
        discarded = await call("discard_agent", r["agent_id"], force=True)
        assert "managed_run" not in str(discarded) and "node_id" not in str(discarded), discarded

    drive(goff, scenario)
    assert [c for c in goff.fx.calls() if c["resume"] == "ses_l2"]
    assert scheduler_traces(goff) == []


def test_nc_r48_gate_off_saturation_still_defers_into_the_legacy_queue(goff):
    goff.pc = goff.provider("pcfx", max_concurrent=1)
    goff.agent("pcworker", "pcfx")
    commit_config(goff)

    async def scenario(call):
        a = await call("start_agent", "pcworker", task("P1", gate="gP1"))
        b = await call("start_agent", "pcworker", task("P2"))
        assert a.get("agent_id")
        assert "node_id" not in b and "node_id" not in a, (a, b)
        return b

    drive(goff, scenario)
    assert goff.deferred(), "today's deferral queue must still be used with the gate off"
    assert scheduler_traces(goff) == []


@pytest.mark.parametrize("op,args", [
    ("create_node", {"kind": "simple", "agent": "worker", "task": "t", "plan_revision": 0}),
    ("list_nodes", {}),
    ("get_node", {"id": "nd-00000000"}),
    ("wait_for_nodes", {"timeout": 0.1}),
    ("scheduler_status", {}),
])
def test_nc_r48_gate_off_node_tools_answer_scheduler_disabled_and_leave_no_file(goff, op, args):
    out = call_tool(goff, op, **args)
    assert out == {"error": "scheduler_disabled"} or out.get("error") == "scheduler_disabled", out
    assert scheduler_traces(goff) == []
    assert not goff.sock.exists()


def test_nc_r48_gate_off_a_run_tool_given_a_node_shaped_id_is_not_a_node_error(goff):
    out = call_tool(goff, "check_agent", "nd-deadbeef")
    assert out.get("error") != "node_id"
    assert scheduler_traces(goff) == []


def test_nc_r48_gate_off_the_monitor_state_has_no_scheduler_section(goff):
    from multiagents.config import load as load_config
    from multiagents.monitor import snapshot
    state = snapshot.snapshot(goff.paths, load_config(goff.paths), with_scripts=False)
    sched = state.get("scheduler")
    assert not sched or sched.get("enabled") is False, sched
    assert scheduler_traces(goff) == []


def test_nc_r48_gate_off_scheduler_start_creates_nothing_and_run_does_not_start_one(goff):
    out = goff.cli("scheduler", "start", timeout=30)
    assert "invalid choice" not in out.stderr, out.stderr
    assert scheduler_traces(goff) == []
    assert not goff.sock.exists()


def test_nc_r48_gate_off_the_default_ships_off(tmp_path, monkeypatch):
    """The shipped default is off: a project that says nothing about the
    scheduler behaves as gate off."""
    world = GitWorld(tmp_path, monkeypatch, gate=False)
    world.project.pop("scheduler")
    world.write_config()
    r = call_tool(world, "start_agent", "worker", task("D"))
    assert r.get("agent_id") and "node_id" not in r, r
    out = call_tool(world, "list_nodes")
    assert out.get("error") == "scheduler_disabled", out
    assert scheduler_traces(world) == []
    world.close()
