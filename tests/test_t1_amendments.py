"""Amendments of 2026-09-30 (after the advisor) to T1: DQ-R8, DQ-R4a, DQ-R2a, DQ-R3c.

Contract: `context/specs/t1-deferred-queue.md`. Seams are those of
`tests/test_t1_deferred_queue.py` (real Runner over a throwaway project with
fake provider CLIs, MCP tools called as functions, observation through tool
results, `events.jsonl`, the tree file and the git worktree list); its
docstring's accepted assumptions apply here too.

Assumptions of this file (the contract is silent; each is deliberately loose):
- DQ-R8 names no field for the claim. To set up "a `restarting` entry whose
  claimer is dead" the tests write `status: "restarting"` plus a claim record
  spelling the pid under several plausible keys (`claim.pid`, `claimed_pid`,
  ...). NEED_INFO(claim-shape): if the implementation reads another key, this
  file needs that key added; nothing else about the claim is asserted.
- The node's `deferred_id` is read from the raw node record in the tree file.
- Concurrency is exercised by two drains at once, in one event loop and in two
  threads, each with its own loop; both share the tree file, as two processes
  would.
- `refused_total` is absent or zero when nothing is refused ("whenever it is
  non-zero").
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_t1_deferred_queue import Proj, blob  # noqa: E402


@pytest.fixture
def p(tmp_path, monkeypatch):
    return Proj(tmp_path, monkeypatch)


def worker_nodes(p):
    return [n for n in p.tree.read()["nodes"].values() if n.get("agent") == "worker"]


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def mark_restarting(p, df, pid):
    """Leave entry `df` as a drain that died mid-restart would."""
    when = time.time() - 60
    with p.tree.transaction() as data:
        for e in data["deferred"]:
            if e["id"] == df:
                e["status"] = "restarting"
                e["claim"] = {"pid": pid, "time": when, "at": when, "ts": when,
                              "claimed_at": when}
                e["claimed_pid"] = pid
                e["claimed_by"] = pid
                e["claimer_pid"] = pid


def entry(p, df):
    return next((e for e in p.tree.read()["deferred"] if e["id"] == df), None)


def add_node_for(p, df, agent_id="ag-orphan"):
    p.tree.add(Node(id=agent_id, agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    with p.tree.transaction() as data:
        data["nodes"][agent_id]["deferred_id"] = df
    return agent_id


def refuse_one(p, task="pinned"):
    df = p.queue(model="z1", task=task)
    p.remove_fallback()
    return df


# ---------------------------------------------------------------------------
# DQ-R8 — claim before restart
# ---------------------------------------------------------------------------

def test_dq_r8_a_started_node_records_the_deferred_id_it_came_from(p):
    df = p.queue()
    p.wait()
    (node,) = worker_nodes(p)
    assert node.get("deferred_id") == df, node


def test_dq_r8_two_concurrent_drains_in_one_loop_start_one_run(p):
    df = p.queue()

    async def both():
        return await asyncio.gather(server.wait_for_agents(timeout=1),
                                    server.wait_for_agents(timeout=1))
    asyncio.run(both())
    assert len(worker_nodes(p)) == 1, worker_nodes(p)
    exits = p.exits()
    assert [(e["deferred_id"], e["outcome"]) for e in exits] == [(df, "restarted")], exits
    assert p.queue_ids() == []


def test_dq_r8_two_drains_in_two_threads_start_one_run(p):
    df = p.queue()
    barrier = threading.Barrier(2)
    errors = []

    def drain():
        try:
            barrier.wait()
            asyncio.run(server.wait_for_agents(timeout=1))
        except Exception as exc:                 # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert len(worker_nodes(p)) == 1, worker_nodes(p)
    assert [(e["deferred_id"], e["outcome"]) for e in p.exits()] == [(df, "restarted")]


def test_dq_r8_concurrent_drains_over_a_batch_start_each_entry_once(p):
    ids = [p.queue(task=f"t{n}") for n in range(3)]

    async def both():
        return await asyncio.gather(server.wait_for_agents(timeout=1),
                                    server.wait_for_agents(timeout=1))
    asyncio.run(both())
    nodes = worker_nodes(p)
    assert sorted(n["deferred_id"] for n in nodes) == sorted(ids), nodes
    assert sorted(e["deferred_id"] for e in p.exits()) == sorted(ids)


def test_dq_r8_an_entry_claimed_by_a_live_drain_is_left_alone(p):
    df = p.queue()
    mark_restarting(p, df, os.getpid())          # this process is alive
    p.wait(timeout=1)
    assert worker_nodes(p) == []
    assert p.exits() == []
    assert entry(p, df)["status"] == "restarting"


def test_dq_r8_a_dead_claimers_entry_without_a_node_returns_to_waiting(p):
    df = p.queue(ago=-3600)                      # not due, so nothing restarts it
    mark_restarting(p, df, dead_pid())
    p.wait(timeout=1)
    e = entry(p, df)
    assert e is not None and e["status"] == "waiting", e
    assert worker_nodes(p) == []
    assert p.exits() == []
    assert [x["status"] for x in p.listed()] == ["waiting"]


def test_dq_r8_a_recovered_waiting_entry_that_is_due_is_then_restarted_once(p):
    df = p.queue()
    mark_restarting(p, df, dead_pid())
    result = p.wait()
    (node,) = worker_nodes(p)
    assert node["deferred_id"] == df
    assert [(e["deferred_id"], e["outcome"]) for e in p.exits()] == [(df, "restarted")]
    assert p.queue_ids() == []
    assert len(result["deferred"]["restarted"]) == 1, result


def test_dq_r8_a_dead_claimers_entry_with_a_node_counts_as_restarted(p):
    df = p.queue()
    node_id = add_node_for(p, df)
    mark_restarting(p, df, dead_pid())
    result = p.wait(timeout=1)
    assert p.queue_ids() == []
    assert len(worker_nodes(p)) == 1, "no second run was started"
    exits = p.exits()
    assert len(exits) == 1, exits
    e = exits[0]
    assert (e["deferred_id"], e["agent"], e["outcome"]) == (df, "worker", "restarted")
    assert e.get("agent_id") == node_id, e
    assert node_id in blob(result.get("deferred", {}).get("restarted", [])) or \
        node_id in blob(result), result


def test_dq_r8_a_recovered_entry_writes_its_event_only_once(p):
    df = p.queue()
    add_node_for(p, df)
    mark_restarting(p, df, dead_pid())
    p.wait(timeout=1)
    p.wait(timeout=1)
    assert len(p.exits()) == 1


def test_dq_r8_recovery_does_not_touch_an_entry_that_is_merely_waiting(p):
    df = p.queue(ago=-3600)
    p.wait(timeout=1)
    assert entry(p, df)["status"] == "waiting"
    assert p.exits() == []


# ---------------------------------------------------------------------------
# DQ-R4a — deferred_by comes from the trusted caller
# ---------------------------------------------------------------------------

def _child_defers(p, agent_id="ag-a"):
    p.tree.add(Node(id=agent_id, agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child(agent_id)
    p.headroom["acme"] = 0.0
    result = p.call(server.start_agent, "worker", "child work")
    assert result.get("deferred") is True, result
    return p.queue_ids()[-1]


def test_dq_r4a_the_creator_is_recorded_at_creation_and_listed(p):
    df = _child_defers(p)
    by_id = {e["id"]: e for e in p.listed()}
    assert by_id[df]["deferred_by"] == "ag-a"


def test_dq_r4a_the_creator_is_not_the_agent_the_task_is_for(p):
    """spec.agent is `worker`; the deferrer is `ag-a`. Nothing infers one from the other."""
    df = _child_defers(p)
    (e,) = [x for x in p.listed() if x["id"] == df]
    assert e["agent"] == "worker" and e["deferred_by"] != "worker"


def test_dq_r4a_the_creator_survives_a_drain_that_leaves_the_entry_waiting(p):
    df = _child_defers(p)
    h.as_root(p.monkeypatch)
    p.wait(timeout=1)                            # acme has no headroom: nothing restarts
    (e,) = [x for x in p.listed() if x["id"] == df]
    assert e["status"] == "waiting" and e["deferred_by"] == "ag-a"


def test_dq_r4a_an_entry_without_deferred_by_cannot_be_cancelled_by_a_subagent(p):
    df = p.queue(ago=-3600)                      # made without a caller: legacy
    assert "deferred_by" not in (entry(p, df) or {}) or not entry(p, df)["deferred_by"]
    # The caller is a node whose *agent name* equals the entry's spec.agent, the
    # tempting thing to infer an owner from.
    p.tree.add(Node(id="ag-w", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child("ag-w")
    result = p.call(server.cancel_deferred, df)
    assert result.get("error"), result
    assert p.queue_ids() == [df]
    assert p.exits() == []


def test_dq_r4a_an_entry_without_deferred_by_cannot_be_cancelled_by_any_other_subagent(p):
    df = p.queue(ago=-3600)
    p.tree.add(Node(id="ag-other", agent="plain", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child("ag-other")
    assert p.call(server.cancel_deferred, df).get("error")
    assert p.queue_ids() == [df]


def test_dq_r4a_the_orchestrator_may_cancel_an_entry_without_deferred_by(p):
    df = p.queue(ago=-3600)
    result = p.call(server.cancel_deferred, df)
    assert "error" not in result, result
    assert p.queue_ids() == []
    assert [e["outcome"] for e in p.exits()] == ["cancelled"]


# ---------------------------------------------------------------------------
# DQ-R2a — refused entries stay visible
# ---------------------------------------------------------------------------

def refused_total(result):
    return (result.get("deferred") or {}).get("refused_total", 0)


def test_dq_r2a_the_drain_that_refuses_reports_the_total(p):
    refuse_one(p)
    result = p.wait(timeout=1)
    assert refused_total(result) == 1, result


def test_dq_r2a_a_later_wait_that_drains_nothing_still_reports_the_refused_entry(p):
    refuse_one(p)
    p.wait(timeout=1)
    later = p.wait(timeout=1)
    assert refused_total(later) == 1, later


def test_dq_r2a_the_total_is_on_a_timeout_result_too(p):
    refuse_one(p)
    p.wait(timeout=1)
    result = p.wait_beside_a_long_run()
    assert result.get("timed_out") is True, result
    assert refused_total(result) == 1, result


def test_dq_r2a_the_total_counts_every_refused_entry(p):
    refuse_one(p, "a")
    p.queue(model="z1", task="b")
    p.wait(timeout=1)
    assert refused_total(p.wait(timeout=1)) == 2


def test_dq_r2a_the_total_is_reported_beside_a_restart_from_the_same_drain(p):
    refuse_one(p)
    p.wait(timeout=1)
    p.queue(model=None, task="fine")             # `worker` on acme/m1: startable
    result = p.wait()
    assert len(result["deferred"]["restarted"]) == 1, result
    assert refused_total(result) == 1, result


def test_dq_r2a_refused_entries_do_not_expire_by_themselves(p):
    df = refuse_one(p)
    for _ in range(3):
        p.wait(timeout=1)
    time.sleep(1.5)
    assert refused_total(p.wait(timeout=1)) == 1
    assert df in p.queue_ids()


def test_dq_r2a_cancelling_the_refused_entry_takes_it_out_of_the_total(p):
    df = refuse_one(p)
    p.wait(timeout=1)
    p.call(server.cancel_deferred, df)
    assert refused_total(p.wait(timeout=1)) == 0


def test_dq_r2a_waiting_entries_are_not_counted_as_refused(p):
    p.queue(ago=-3600)
    assert refused_total(p.wait(timeout=1)) == 0


def test_dq_r2a_no_refused_entries_means_no_nonzero_total(p):
    assert refused_total(p.wait(timeout=1)) == 0


# ---------------------------------------------------------------------------
# DQ-R3c — an error returned by start() leaves nothing behind
# ---------------------------------------------------------------------------

def worktree_dirs(p):
    out = subprocess.run(["git", "-C", str(p.r.paths.root), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l.startswith("worktree ")]


def leftovers(p):
    root = Path(p.r.paths.worktrees)
    return [x for x in root.rglob("*") if x.is_dir()] if root.exists() else []


def test_dq_r3c_a_refused_drain_leaves_no_node_and_no_worktree(p):
    before = worktree_dirs(p)
    df = refuse_one(p)
    p.wait(timeout=1)
    assert entry(p, df)["status"] == "refused"
    assert p.tree.read()["nodes"] == {}
    assert worktree_dirs(p) == before
    assert leftovers(p) == []


def test_dq_r3c_repeated_drains_of_a_refused_entry_leave_nothing_either(p):
    before = worktree_dirs(p)
    refuse_one(p)
    for _ in range(3):
        p.wait(timeout=1)
    assert p.tree.read()["nodes"] == {}
    assert worktree_dirs(p) == before


def test_dq_r3c_a_start_that_returns_an_error_creates_no_node_or_worktree(p):
    """Directly, not through the drain: the pin names a model `worker` no longer has."""
    p.remove_fallback()
    before = worktree_dirs(p)
    try:
        result = p.call(server.start_agent, "worker", "pinned task", model="z1")
    except (ValueError, PermissionError) as exc:
        result = {"error": str(exc)}
    assert result.get("error"), result
    assert not result.get("agent_id")
    assert p.tree.read()["nodes"] == {}
    assert worktree_dirs(p) == before
    assert leftovers(p) == []


def test_dq_r3c_if_a_refusal_did_leave_a_node_the_entry_names_it(p):
    """The fallback clause: whatever exists must be findable from the entry."""
    df = refuse_one(p)
    p.wait(timeout=1)
    nodes = p.tree.read()["nodes"]
    if nodes:
        e = entry(p, df)
        assert e.get("node_id") in nodes, (e, list(nodes))
