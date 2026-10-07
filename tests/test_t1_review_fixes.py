"""Regression tests for the review findings on T1: DQ-R12 and DQ-R2a, plus the
phase-0 provider-name invariant on monitor/snapshot.py.

Contract: `context/specs/t1-deferred-queue.md` (DQ-R12, DQ-R2a). Seams are
those of `tests/test_t1_deferred_queue.py` (real Runner over a throwaway
project with fake provider CLIs, MCP tools called as functions, observation
through tool results, `events.jsonl` and the tree file); its docstring's
accepted assumptions apply here too.

What each test pins:
- DQ-R12: the status check and the removal of a cancel happen in one
  transaction, and a cancel of a `restarting` entry is refused even when the
  caller's snapshot read said `waiting` — the record can then never say
  `cancelled` while the claimed run starts.
- DQ-R2a: the paused early return of the drain carries `refused_total` when
  it is non-zero (and nothing when it is zero), and the wait_for_agents
  result keeps carrying it while the tree is paused.
- Phase 0: the comment introducing `ISO_STAMP` names no provider.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents import server  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_t1_deferred_queue import Proj  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def p(tmp_path, monkeypatch):
    return Proj(tmp_path, monkeypatch)


def refused_total(result):
    return (result.get("deferred") or {}).get("refused_total", 0)


def refuse_one(p, task="pinned"):
    """Queue a due entry pinned to a model the roster no longer serves."""
    df = p.queue(model="z1", task=task)
    p.remove_fallback()
    return df


def claim_as_drain(p, df, pid=None):
    """Put entry `df` in the state an in-flight drain's claim leaves (DQ-R8a)."""
    with p.tree.transaction() as data:
        entry = next(e for e in data["deferred"] if e["id"] == df)
        entry["status"] = "restarting"
        entry["claim"] = {"pid": pid if pid is not None else os.getpid(),
                          "at": time.time()}


def entry(p, df):
    return next((e for e in p.tree.read()["deferred"] if e["id"] == df), None)


# ---------------------------------------------------------------------------
# DQ-R12 — cancel versus restart
# ---------------------------------------------------------------------------

def test_dq_r12_the_exit_refuses_to_cancel_an_entry_that_is_restarting(p):
    """The removal and the status check are one transaction (DQ-R12)."""
    df = p.queue(ago=-3600)                     # not due: only a claim can move it
    claim_as_drain(p, df)
    assert p.tree.exit_deferred(df, "cancelled") == "restarting"
    now = entry(p, df)
    assert now is not None and now["status"] == "restarting", now
    assert p.exits() == [], "a refused cancel writes no event"


def test_dq_r12_a_cancel_that_races_a_claim_is_refused_in_the_transaction(p):
    """The snapshot said `waiting`; the claim lands before the removal.

    This is the race the review found: the old code decided from the snapshot
    and removed without re-checking, so the record said `cancelled` while the
    claimed run started anyway.
    """
    df = p.queue(ago=-3600)
    real_read = p.tree.read
    claimed = []

    def read_then_claim():
        data = real_read()                      # the snapshot the cancel sees
        with p.tree.transaction() as tdata:     # the drain's claim lands after it
            e = next(e for e in tdata["deferred"] if e["id"] == df)
            if e.get("status") == "waiting":
                e["status"] = "restarting"
                e["claim"] = {"pid": os.getpid(), "at": time.time()}
                claimed.append(True)
        return data

    p.monkeypatch.setattr(p.tree, "read", read_then_claim)
    result = p.call(server.cancel_deferred, df)

    assert claimed, "the claim never landed, so the race was not exercised"
    assert result.get("error"), result
    assert "restarting" in result["error"] and "DQ-R12" in result["error"], result
    now = entry(p, df)
    assert now is not None and now["status"] == "restarting", now
    assert p.exits() == [], "no deferred_exit may be written for a refused cancel"


def test_dq_r12_a_cancel_refused_then_requeued_is_cancelled_not_refused(p):
    """The reviewer's interleaving: the guard refuses, a transient start
    failure requeues the entry to `waiting`, and only then does the server
    look again. The entry is waiting and no run exists — the caller's cancel
    still stands, so the answer is `cancelled`, never "restarting".
    """
    df = p.queue(ago=-3600)
    real_exit = p.tree.exit_deferred
    seen = []

    def exit_with_claim_then_transient_settle(deferred_id, outcome, **fields):
        if seen or outcome != "cancelled":
            return real_exit(deferred_id, outcome, **fields)
        seen.append("entry")
        claim_as_drain(p, df)                    # the drain wins the race first
        refused = real_exit(deferred_id, outcome, **fields)   # the guard refuses
        p.tree.requeue_deferred(df)              # the start failed; the claim dissolves
        return refused

    p.monkeypatch.setattr(p.tree, "exit_deferred", exit_with_claim_then_transient_settle)
    result = p.call(server.cancel_deferred, df)

    assert seen == ["entry"], "the interleaving was not exercised"
    assert result.get("cancelled") == df, result
    assert "error" not in result, result
    assert p.queue_ids() == []
    assert [(e["deferred_id"], e["outcome"]) for e in p.exits()] == \
        [(df, "cancelled")], "exactly one event, and it is true"


def test_dq_r12_the_drains_own_exits_of_a_restarting_entry_still_work(p):
    """Control: the refusal is the cancel's alone, not the exit's."""
    df = p.queue(ago=-3600)
    claim_as_drain(p, df)
    p.tree.add(Node(id="ag-claimed", agent="worker", provider="acme", model="m1",
                    parent=None, depth=1, status="running"))
    assert p.tree.exit_deferred(df, "restarted", agent_id="ag-claimed") is True
    assert p.queue_ids() == []
    assert [e["outcome"] for e in p.exits()] == ["restarted"]


def test_dq_r12_a_malformed_restarting_entry_is_still_cancellable(p):
    """DQ-R9: a malformed entry is never claimed, so its cancel cannot race,
    and the orchestrator's cancel is its only way out."""
    with p.tree.transaction() as data:
        data["deferred"].append({"id": "df-bad", "spec": {"agent": "worker", "task": "t"},
                                 "retry_after": time.time() + 600,
                                 "status": "restarting",
                                 "claim": {"pid": "not-an-int", "at": 0}})
    result = p.call(server.cancel_deferred, "df-bad")
    assert "error" not in result, result
    assert "df-bad" not in p.queue_ids()
    assert [e["outcome"] for e in p.exits()] == ["cancelled"]


# ---------------------------------------------------------------------------
# DQ-R2a — refused entries stay visible on the paused path too
# ---------------------------------------------------------------------------

def test_dq_r2a_the_paused_drain_itself_carries_the_refused_total(p):
    df = refuse_one(p)
    p.wait(timeout=1)                           # drained once: the entry is refused
    assert entry(p, df)["status"] == "refused", entry(p, df)
    p.defer_for_real()                          # a real deferral pauses the tree

    drain = asyncio.run(p.r.resume_deferred())

    assert drain.get("paused") is True, drain
    assert drain.get("refused_total") == 1, drain


def test_dq_r2a_the_wait_result_keeps_carrying_it_while_the_tree_is_paused(p):
    """The composed path the contract states: pause or not, the count rides."""
    refuse_one(p)
    p.wait(timeout=1)
    p.defer_for_real()
    result = p.wait(timeout=1)
    assert refused_total(result) == 1, result


def test_dq_r2a_the_paused_drain_without_refusals_carries_no_total(p):
    p.defer_for_real()
    drain = asyncio.run(p.r.resume_deferred())
    assert drain.get("paused") is True, drain
    assert "refused_total" not in drain, drain


# ---------------------------------------------------------------------------
# Phase 0 — no provider name in the comment that introduces ISO_STAMP
# ---------------------------------------------------------------------------

def test_phase0_the_iso_stamp_comment_names_no_provider():
    """Leftover of the invariant budget.py's comment was held to (9ceede2)."""
    text = (ROOT / "src" / "multiagents" / "monitor" / "snapshot.py").read_text()
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("ISO_STAMP"))
    block = []
    for line in reversed(lines[:start]):
        if not line.startswith("#"):
            break
        block.append(line)
    pattern = re.compile(r"claude|agy|opencode|z\.ai|zai", re.IGNORECASE)
    named = [line for line in block if pattern.search(line)]
    assert not named, f"provider names in the ISO_STAMP comment: {named}"
