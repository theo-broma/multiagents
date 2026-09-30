"""Adversarial review of T1, the deferred queue (DQ-R1..R8a).

Contract: `context/specs/t1-deferred-queue.md`, amendments included. Every test
here fails against the implementation it was written for, and states the
contract clause it holds the code to. Seams are those of
`tests/test_t1_deferred_queue.py` (real Runner, fake provider CLIs); a few
tests wrap `Runner.start` to place an event at an exact point of the drain —
a crash, a cancellation, a concurrent tool call — which is the only way to
make an interleaving deterministic.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents import procs, server  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_t1_deferred_queue import Proj  # noqa: E402


@pytest.fixture
def p(tmp_path, monkeypatch):
    return Proj(tmp_path, monkeypatch)


class DrainDied(BaseException):
    """Stands in for the drain process dying: nothing after it runs."""


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def worker_nodes(p):
    return [n for n in p.tree.read()["nodes"].values() if n.get("agent") == "worker"]


def entry(p, df):
    return next((e for e in p.tree.read()["deferred"] if e.get("id") == df), None)


def wrap_start(p, before=None, after=None):
    real = p.r.start

    async def start(*args, **kwargs):
        if before:
            before()
        result = await real(*args, **kwargs)
        if after:
            after(result)
        return result
    p.monkeypatch.setattr(p.r, "start", start)
    return real


# ---------------------------------------------------------------------------
# Lead 1 — the crash window between start() returning and deferred_id written
# ---------------------------------------------------------------------------

def test_a_drain_dying_after_start_returned_does_not_restart_the_task_twice(p):
    """DQ-R8: a drain that dies mid-restart must not cause a second restart.

    The run is started by start(), but the node only learns its `deferred_id`
    in a later transaction. A drain that dies in between leaves a node with no
    `deferred_id`, recovery finds no carrier, returns the entry to `waiting`,
    and the next drain starts the same task again.
    """
    df = p.queue()
    real = p.r.start

    async def start_then_die(*args, **kwargs):
        result = await real(*args, **kwargs)
        with p.tree.transaction() as data:        # this process is now "dead"
            for e in data["deferred"]:
                if e["id"] == df:
                    e["claim"]["pid"] = dead_pid()
        raise DrainDied()
    p.monkeypatch.setattr(p.r, "start", start_then_die)
    with pytest.raises(DrainDied):
        p.wait(timeout=1)
    assert len(worker_nodes(p)) == 1

    p.monkeypatch.setattr(p.r, "start", real)     # a fresh server drains next
    p.wait(timeout=10)
    assert len(worker_nodes(p)) == 1, (
        f"the task was started twice: {[n['id'] for n in worker_nodes(p)]}")


# ---------------------------------------------------------------------------
# A cancelled drain leaves the entry `restarting` behind a live pid, for ever
# ---------------------------------------------------------------------------

def test_a_drain_cancelled_mid_start_does_not_strand_the_entry(p):
    """DQ-R8 / DQ-R7: a due entry still restarts at the next wait_for_agents.

    asyncio.CancelledError (an MCP client giving up on a wait_for_agents call)
    is a BaseException, so `except Exception` does not release the claim. The
    entry stays `restarting` with this server's pid, which is alive, so
    recovery never touches it and no drain in this server's lifetime restarts
    it.
    """
    df = p.queue()
    real = p.r.start

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    p.monkeypatch.setattr(p.r, "start", cancelled)
    with pytest.raises(asyncio.CancelledError):
        p.wait(timeout=1)

    p.monkeypatch.setattr(p.r, "start", real)
    p.wait(timeout=10)
    p.wait(timeout=10)
    assert len(worker_nodes(p)) == 1, (
        f"entry stranded: {entry(p, df)}")


# ---------------------------------------------------------------------------
# Interference — cancel_deferred while the drain is restarting the entry
# ---------------------------------------------------------------------------

def test_a_cancel_that_succeeds_mid_restart_does_not_let_the_run_start_anyway(p):
    """DQ-R1 / DQ-R5: exactly one exit event, and it must be true.

    cancel_deferred accepts a `restarting` entry, removes it and writes
    `cancelled`; the drain's start() then runs the task, its `restarted` exit
    finds no entry and writes nothing. The record says cancelled, the task ran.
    """
    df = p.queue()
    replies = []
    wrap_start(p, before=lambda: replies.append(server.cancel_deferred(df)))
    p.wait(timeout=10)
    cancelled = "error" not in replies[0]
    ran = bool(worker_nodes(p))
    outcomes = [e["outcome"] for e in p.exits() if e["deferred_id"] == df]
    assert not (cancelled and ran), (
        f"cancel reported success {replies[0]} but the run started anyway; "
        f"events: {outcomes}")
    assert len(outcomes) == 1, outcomes


# ---------------------------------------------------------------------------
# DQ-R8a — a re-deferred entry keeps its original deferred_by
# ---------------------------------------------------------------------------

def test_a_re_deferral_drained_by_a_subagent_keeps_the_orchestrators_ownership(p):
    """DQ-R8a / DQ-R4a: the orchestrator's entry, drained by a child's
    wait_for_agents and re-deferred, becomes the child's (deferred_by is the
    drainer), and the child may then cancel it."""
    df = p.queue()                                # deferred_by: None (orchestrator)
    p.tree.add(Node(id="ag-kid", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    p.as_child("ag-kid")
    p.headroom["acme"] = 0.0
    p.headroom["zeta"] = 0.0
    p.wait(timeout=1)
    (fresh,) = [e for e in p.tree.read()["deferred"] if e["id"] != df]
    assert fresh.get("deferred_by") in (None, "orchestrator"), (
        f"re-deferred entry now owned by {fresh.get('deferred_by')!r}")
    result = p.call(server.cancel_deferred, fresh["id"])
    assert result.get("error"), f"ag-kid cancelled the orchestrator's task: {result}"


def test_a_re_deferral_does_not_rewrite_another_agents_concurrent_entry(p):
    """DQ-R8a / DQ-R4a: the drain finds "its" re-deferred entry as the first id
    that was not in the queue before start(); an entry deferred by someone else
    in that window is taken instead, handed the drained entry's owner, and
    named as the new id in the `re_deferred` event."""
    spec = {"agent": "worker", "task": "owned by ag-a", "timeout": None,
            "model": None, "workdir": None}
    df = p.tree.defer(spec, time.time() - 1, "quota", deferred_by="ag-a")["id"]
    other = {}

    def someone_else_defers():
        spec2 = dict(spec, task="ag-x's own work")
        other["id"] = p.tree.defer(spec2, time.time() + 600, "quota",
                                   deferred_by="ag-x")["id"]
        p.headroom["acme"] = 0.0
        p.headroom["zeta"] = 0.0
    wrap_start(p, before=someone_else_defers)
    p.wait(timeout=1)
    assert entry(p, other["id"])["deferred_by"] == "ag-x", entry(p, other["id"])
    (ev,) = [e for e in p.exits() if e["deferred_id"] == df]
    assert ev.get("new_deferred_id") != other["id"], ev


# ---------------------------------------------------------------------------
# Lead 4 — recovery can revive a refused entry
# ---------------------------------------------------------------------------

def test_two_recovering_drains_do_not_retry_a_refused_entry(p):
    """DQ-R3: a refused entry is never retried automatically.

    Recovery reads a `restarting` entry, checks its pid outside any
    transaction, and then `requeue_deferred` sets `waiting` without checking
    the status is still `restarting`. If a second drain resolved the entry
    meanwhile (here: to `refused`), the first puts it back to `waiting` and
    retries it.
    """
    df = p.queue(model="z1")
    p.remove_fallback()                           # the pin can no longer run
    with p.tree.transaction() as data:
        data["deferred"][0]["status"] = "restarting"
        data["deferred"][0]["claim"] = {"pid": dead_pid(), "at": time.time() - 60}

    real_alive = procs.alive
    state = {"inner": False}

    def other_drain():
        state["inner"] = True
        asyncio.run(server.wait_for_agents(timeout=1))

    def alive(pid, start=""):
        if not state["inner"]:
            t = threading.Thread(target=other_drain)
            t.start()
            t.join()
        return real_alive(pid, start)
    p.monkeypatch.setattr(procs, "alive", alive)
    p.wait(timeout=1)
    refused = [e for e in p.exits() if e["deferred_id"] == df]
    assert [e["outcome"] for e in refused] == ["refused"], refused
    assert entry(p, df)["status"] == "refused"


# ---------------------------------------------------------------------------
# Lead 5 — malformed queue entries must not crash wait_for_agents
# ---------------------------------------------------------------------------

def _plant(p, bad):
    with p.tree.transaction() as data:
        data["deferred"].append(bad)


@pytest.mark.parametrize("bad", [
    pytest.param({"id": "df-strpid", "spec": {"agent": "worker", "task": "t"},
                  "retry_after": 0, "status": "restarting",
                  "claim": {"pid": "12345", "at": 0}}, id="claim-pid-is-a-string"),
    pytest.param("df-not-a-dict", id="entry-is-a-string"),
    pytest.param(None, id="entry-is-null"),
    pytest.param({"id": "df-noretry", "spec": {"agent": "worker", "task": "t"},
                  "status": "waiting"}, id="missing-retry_after"),
    pytest.param({"id": "df-strretry", "spec": {"agent": "worker", "task": "t"},
                  "retry_after": "soon"}, id="retry_after-is-a-string"),
    pytest.param({"spec": {"agent": "worker", "task": "t"}, "retry_after": 0},
                 id="missing-id"),
    pytest.param({"id": "df-nullclaim", "spec": {"agent": "worker", "task": "t"},
                  "retry_after": 0, "status": "restarting", "claim": "x"},
                 id="claim-is-a-string"),
])
def test_a_malformed_entry_does_not_crash_wait_for_agents(p, bad):
    """Lead 5: nothing in the queue may crash wait_for_agents; the good entry
    beside the bad one still restarts."""
    good = p.queue(task="good")
    _plant(p, bad)
    result = p.wait(timeout=10)
    assert isinstance(result, dict) and "error" not in result, result
    assert entry(p, good) is None, "the well-formed entry was not drained"


@pytest.mark.parametrize("bad", [
    pytest.param("df-not-a-dict", id="entry-is-a-string"),
    pytest.param({"id": "df-strretry", "spec": {"agent": "worker"},
                  "retry_after": "soon"}, id="retry_after-is-a-string"),
])
def test_a_malformed_entry_does_not_crash_list_deferred(p, bad):
    p.queue(task="good")
    _plant(p, bad)
    result = p.call(server.list_deferred)
    assert "error" not in result, result


# ---------------------------------------------------------------------------
# DQ-R2 — a restart settled by recovery while paused is not reported
# ---------------------------------------------------------------------------

def test_a_restart_settled_by_recovery_is_reported_even_while_paused(p):
    """DQ-R2: when the drain did anything, the result carries `deferred`.

    Recovery resolves a dead drain's entry as `restarted` (event written, entry
    removed) before the pause check; on the paused return the result has no
    `still_deferred`, so wait_for_any drops the `deferred` field and the
    restart is reported nowhere in the result.
    """
    df = p.queue(task="settled")
    p.queue(task="holds the pause", ago=-600)
    p.tree.pause(time.time() + 600, "quota", deferral=True)
    with p.tree.transaction() as data:
        for e in data["deferred"]:
            if e["id"] == df:
                e["status"] = "restarting"
                e["claim"] = {"pid": dead_pid(), "at": time.time() - 60}
    p.tree.add(Node(id="ag-carrier", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="done"))
    with p.tree.transaction() as data:
        data["nodes"]["ag-carrier"]["deferred_id"] = df
    result = p.wait(timeout=1)
    assert [e["outcome"] for e in p.exits()] == ["restarted"]   # the drain acted
    assert "ag-carrier" in str((result.get("deferred") or {}).get("restarted")), result
