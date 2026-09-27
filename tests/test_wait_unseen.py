"""tests/test_wait_unseen.py — bug-d6310f.

`wait_for_any(None)` seeded `watched` only from `tree.active()` and
`open_questions()`. An agent that already failed or finished before the call
(e.g. a provider crash within the first few seconds of `start()`) is in
neither set, so it was reported nowhere at all: not in `changed`, not in
`already_finished`, not in `still_running`.

The fix folds `tree.unseen()` (parentless nodes with a result nobody has been
told about yet — the same set `driver.py`'s `_busy()` checks) into the seed
when `agent_ids` is `None`, so the agent surfaces through the existing
already-finished path. It must then disappear again once something marks it
seen — the same call `server.py`'s `_seen()` makes, for every entry in
`changed`/`already_finished`, on the node's next read.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402


@pytest.fixture
def runner(tmp_path, monkeypatch):
    return h.make_runner(tmp_path, monkeypatch, git=False)


def _crashed(r, agent_id: str) -> None:
    """A root agent that ran briefly and then failed, before anyone waited on it."""
    r.tree.add(h.Node(id=agent_id, agent="worker", provider="p", model="m",
                      parent=None, depth=1))
    r.tree.set_status(agent_id, "running")
    r.tree.set_status(agent_id, "failed", "startup crash")


def _running(r, agent_id: str) -> None:
    r.tree.add(h.Node(id=agent_id, agent="worker", provider="p", model="m",
                      parent=None, depth=1))
    r.tree.set_status(agent_id, "running")


def _wait(r, ids, timeout=5):
    return asyncio.run(r.wait_for_any(ids, float(timeout)))


def _agent_ids(result: dict) -> list[str]:
    return ([c["agent_id"] for c in result.get("changed", [])] +
            [c["agent_id"] for c in result.get("already_finished", [])])


def test_agent_that_failed_before_the_wait_is_reported(runner):
    """bug-d6310f: previously invisible — absent from changed, already_finished
    AND still_running, because it was never in `watched` to begin with."""
    _crashed(runner, "ag-crashed")
    result = _wait(runner, None)
    assert "ag-crashed" in _agent_ids(result), result
    assert "ag-crashed" not in result.get("still_running", []), result


def test_agent_that_failed_before_the_wait_reports_its_real_status(runner):
    _crashed(runner, "ag-crashed")
    result = _wait(runner, None)
    entries = result.get("changed", []) + result.get("already_finished", [])
    entry = next(e for e in entries if e["agent_id"] == "ag-crashed")
    assert entry["status"] == "failed", entry


def test_agent_that_failed_before_the_wait_is_not_reported_twice(runner):
    """Once whatever read the result marks it seen (server.py's `_seen()`, in
    production), the next `wait_for_any(None)` must not surface it again —
    the polling-loop guarantee `already_finished` already gives named waits."""
    _crashed(runner, "ag-crashed")
    first = _wait(runner, None)
    assert "ag-crashed" in _agent_ids(first), first

    # Mimics server.py's `_seen()`, which calls this for every entry in
    # `changed`/`already_finished` whose node is still unseen and addressed
    # to the caller.
    runner.tree.mark_seen("ag-crashed", "failed")

    second = _wait(runner, None)
    assert "ag-crashed" not in _agent_ids(second), second
    assert "ag-crashed" not in second.get("still_running", []), second


def test_a_named_wait_is_unaffected(runner):
    """agent_ids given explicitly: watched is exactly what was asked for, same
    as before this fix — tree.unseen() is only folded in for the None
    (wait-on-everything) case, not when the caller named its own list."""
    _running(runner, "ag-b")
    _crashed(runner, "ag-crashed")
    result = _wait(runner, ["ag-b"], timeout=1)
    assert _agent_ids(result) == [], result
    assert result.get("still_running") == ["ag-b"], result
