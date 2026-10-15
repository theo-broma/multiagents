"""tests/test_wait_noid_live.py — bug-a3d76f.

Documented contract (server.py, wait_for_agents): with no ids the wait covers
every active agent and the caller's open nodes without a run. Stale terminal
history (unseen results from bug-d6310f) is context to report, never a reason
to stop waiting while a run is live, and never something from another session.

Races are made deterministic by wrapping `tree.active()` — no sleeps.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

WAIT_S = 1.0


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.delenv("MULTIAGENTS_SESSION_ID", raising=False)
    return h.make_runner(tmp_path, monkeypatch, git=False)


def _node(r, agent_id: str, status: str, session: str = "") -> None:
    r.tree.add(h.Node(id=agent_id, agent="worker", provider="p", model="m",
                      parent=None, depth=1, session=session))
    if status == "running":
        r.tree.set_status(agent_id, "running")
    elif status == "failed":
        r.tree.set_status(agent_id, "running")
        r.tree.set_status(agent_id, "failed", "startup crash")


def _wait(r):
    return asyncio.run(r.wait_for_any(None, WAIT_S))


def _reported(result: dict) -> list[str]:
    return [c["agent_id"] for c in result.get("changed", []) + result.get("already_finished", [])]


def test_stale_terminal_history_does_not_hide_a_live_run(runner):
    _node(runner, "ag-old", "failed")
    _node(runner, "ag-live", "running")
    result = _wait(runner)
    assert not result.get("all_finished"), result
    assert "ag-live" in result.get("still_running", []), result
    assert "ag-live" not in _reported(result), result


def test_stale_terminal_history_is_reported_only_as_finished_context(runner):
    _node(runner, "ag-old", "failed")
    _node(runner, "ag-live", "running")
    result = _wait(runner)
    assert "ag-old" in _reported(result), result
    assert "ag-old" not in result.get("still_running", []), result
    entry = next(e for e in result.get("changed", []) + result.get("already_finished", [])
                 if e["agent_id"] == "ag-old")
    assert entry["status"] == "failed", entry


def _live_after_snapshot(runner, monkeypatch, agent_id="ag-late"):
    """`tree.active()` answers from before the run became live; the run starts
    immediately after, before the wait decides what to return."""
    real = runner.tree.active
    state = {"fired": False}

    def active():
        snapshot = real()
        if not state["fired"]:
            state["fired"] = True
            _node(runner, agent_id, "running")
        return snapshot

    monkeypatch.setattr(runner.tree, "active", active)


def test_race_run_live_after_active_snapshot_is_still_waited_on(runner, monkeypatch):
    _node(runner, "ag-old", "failed")
    _live_after_snapshot(runner, monkeypatch)
    result = _wait(runner)
    assert not result.get("all_finished"), result
    assert "ag-late" in result.get("still_running", []), result


def test_race_never_all_finished_while_capacity_running(runner, monkeypatch):
    _node(runner, "ag-old", "failed")
    _live_after_snapshot(runner, monkeypatch)
    result = _wait(runner)
    if result.get("all_finished"):
        assert result["capacity"]["running"] == 0, result


def test_race_with_nothing_unseen_does_not_claim_no_active_agents(runner, monkeypatch):
    _live_after_snapshot(runner, monkeypatch)
    result = _wait(runner)
    assert not result.get("all_finished"), result
    assert result.get("reason") != "no active agents", result
    assert "ag-late" in result.get("still_running", []), result


def test_noid_result_never_says_every_agent_you_named(runner, monkeypatch):
    _node(runner, "ag-old", "failed")
    result = _wait(runner)  # only history: finishing is fine, the wording is not
    assert "every agent you named" not in str(result.get("note", "")), result
    _live_after_snapshot(runner, monkeypatch)
    result = _wait(runner)
    assert "every agent you named" not in str(result.get("note", "")), result


@pytest.mark.parametrize("other", ["s-other", ""])
def test_unseen_history_from_another_or_empty_session_is_not_imported(
        runner, monkeypatch, other):
    monkeypatch.setenv("MULTIAGENTS_SESSION_ID", "s-mine")
    _node(runner, "ag-theirs", "failed", session=other)
    _node(runner, "ag-mine", "failed", session="s-mine")
    result = _wait(runner)
    assert "ag-theirs" not in _reported(result), result
    assert "ag-theirs" not in result.get("still_running", []), result
    assert "ag-mine" in _reported(result), result
