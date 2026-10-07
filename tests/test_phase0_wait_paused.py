"""Phase 0, group D — P0-R6: `wait_for_agents` under a pause (`bug-b864b8`).

Contract: `context/specs/phase0-runtime-repairs.md` § P0-R6. `still_running`
is settled as a list of agent id STRINGS on every path (changed, timeout,
pause, nothing to wait on) — the orchestrator's ruling on the open question,
recorded in the contract at 4694b9d.

The agents are tree nodes with no process behind them: `wait_for_any` watches
the shared tree (a nested server's agents are only visible that way), so a
node whose status another coroutine flips is exactly what it sees in
production. The pause is set through the tree's own public `pause()`, which is
how a quota pause reaches every server in the tree.

Timing bounds are deliberately loose. The wait polls on a period of about a
second; the assertions only distinguish "returned at once", "waited for the
agent", and "waited for the full timeout".
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

PAUSE_REASON = "every provider is out of quota (test)"
PAUSE_FOR = 120.0


# ---------------------------------------------------------------- helpers --

@pytest.fixture
def runner(tmp_path, monkeypatch):
    return h.make_runner(tmp_path, monkeypatch, git=False)


def _running(r, agent_id: str) -> None:
    r.tree.add(h.Node(id=agent_id, agent="worker", provider="p", model="m",
                      parent=None, depth=1))
    r.tree.set_status(agent_id, "running")


def _finished(r, agent_id: str) -> None:
    _running(r, agent_id)
    r.tree.set_status(agent_id, "done", "finished before the wait")


def _pause(r) -> None:
    r.tree.pause(until=time.time() + PAUSE_FOR, reason=PAUSE_REASON)


async def _finish_later(r, agent_id: str, after: float) -> None:
    await asyncio.sleep(after)
    r.tree.set_status(agent_id, "done", "finished during the wait")


def _wait(r, ids, timeout, finish=None):
    """Run one wait; `finish` = (agent_id, after_seconds) flips it mid-wait.

    Returns (result, elapsed seconds).
    """
    async def go():
        side = None
        if finish:
            side = asyncio.create_task(_finish_later(r, *finish))
        started = time.monotonic()
        result = await r.wait_for_any(ids, float(timeout))
        elapsed = time.monotonic() - started
        if side:
            side.cancel()
        return result, elapsed
    return asyncio.run(go())


def _changed_ids(result) -> list[str]:
    return [c["agent_id"] for c in result.get("changed", [])]


def _assert_still_running(result, expected: list[str]) -> None:
    assert "still_running" in result, (
        f"P0-R6.2: still_running must be present in every result: {result}")
    value = result["still_running"]
    assert isinstance(value, list), value
    assert all(isinstance(x, str) for x in value), (
        f"P0-R6.2: still_running is a list of agent id strings, got {value!r}")
    assert sorted(value) == sorted(expected), value


def _assert_pause_fields(result) -> None:
    assert result.get("paused") is True, result
    assert result.get("reason") == PAUSE_REASON, result
    retry = result.get("retry_after_seconds")
    assert isinstance(retry, (int, float)) and not isinstance(retry, bool), result
    assert 0 < retry <= PAUSE_FOR, retry


# ---------------------------------------------------------------- P0-R6.1 --

def test_p0_r6_1_pause_does_not_end_the_wait_on_a_named_running_agent(runner):
    _running(runner, "ag-a")
    _pause(runner)
    result, elapsed = _wait(runner, ["ag-a"], timeout=15, finish=("ag-a", 1.5))
    assert _changed_ids(result) == ["ag-a"], result
    assert result["changed"][0]["status"] == "done", result
    assert elapsed >= 1.0, (
        f"returned after {elapsed:.2f}s, before the agent changed state: {result}")
    assert elapsed < 10, "returned on the change, not on the timeout"


def test_p0_r6_1_pause_does_not_end_the_wait_on_every_active_agent(runner):
    """No ids: waits on every active agent, pause or not."""
    _running(runner, "ag-a")
    _pause(runner)
    result, elapsed = _wait(runner, None, timeout=15, finish=("ag-a", 1.5))
    assert _changed_ids(result) == ["ag-a"], result
    assert 1.0 <= elapsed < 10, elapsed


def test_p0_r6_1_paused_wait_on_a_running_agent_runs_to_its_timeout(runner):
    _running(runner, "ag-a")
    _pause(runner)
    result, elapsed = _wait(runner, ["ag-a"], timeout=3)
    assert result.get("changed") == [], result
    assert elapsed >= 2.5, (
        f"a paused wait on a running agent returned after {elapsed:.2f}s of a 3s "
        f"timeout: {result}")
    assert elapsed < 8, elapsed


def test_p0_r6_1_paused_wait_reports_the_agent_that_changed_not_the_other(runner):
    _running(runner, "ag-a")
    _running(runner, "ag-b")
    _pause(runner)
    result, _ = _wait(runner, ["ag-a", "ag-b"], timeout=15, finish=("ag-b", 1.5))
    assert _changed_ids(result) == ["ag-b"], result


# ---------------------------------------------------------------- P0-R6.2 --
# paused/unpaused x (agent changes, agent times out, nothing to wait on,
# everything already finished). Unpaused rows prove the field is not a
# pause-only addition.

@pytest.mark.parametrize("paused", [False, True], ids=["unpaused", "paused"])
def test_p0_r6_2_still_running_lists_the_other_agent_when_one_changes(runner, paused):
    _running(runner, "ag-a")
    _running(runner, "ag-b")
    if paused:
        _pause(runner)
    result, _ = _wait(runner, ["ag-a", "ag-b"], timeout=15, finish=("ag-a", 1.5))
    assert _changed_ids(result) == ["ag-a"], result
    _assert_still_running(result, ["ag-b"])


@pytest.mark.parametrize("paused", [False, True], ids=["unpaused", "paused"])
def test_p0_r6_2_still_running_is_id_strings_on_timeout(runner, paused):
    _running(runner, "ag-a")
    if paused:
        _pause(runner)
    result, _ = _wait(runner, ["ag-a"], timeout=2)
    assert result.get("changed") == [], result
    _assert_still_running(result, ["ag-a"])


@pytest.mark.parametrize("paused", [False, True], ids=["unpaused", "paused"])
def test_p0_r6_2_still_running_is_empty_when_nothing_is_active(runner, paused):
    if paused:
        _pause(runner)
    result, _ = _wait(runner, None, timeout=5)
    _assert_still_running(result, [])


@pytest.mark.parametrize("paused", [False, True], ids=["unpaused", "paused"])
def test_p0_r6_2_still_running_is_empty_when_every_named_agent_had_finished(runner, paused):
    _finished(runner, "ag-a")
    if paused:
        _pause(runner)
    result, _ = _wait(runner, ["ag-a"], timeout=5)
    _assert_still_running(result, [])
    if not paused:
        assert _changed_ids(result) == ["ag-a"], result


def test_p0_r6_2_still_running_excludes_watched_agents_that_are_not_running(runner):
    """Lists the WATCHED agents still running — not every active agent."""
    _running(runner, "ag-a")
    _running(runner, "ag-unwatched")
    _pause(runner)
    result, _ = _wait(runner, ["ag-a"], timeout=2)
    _assert_still_running(result, ["ag-a"])


# ---------------------------------------------------------------- P0-R6.3 --

def test_p0_r6_3_pause_fields_when_an_agent_changes(runner):
    _running(runner, "ag-a")
    _pause(runner)
    result, _ = _wait(runner, ["ag-a"], timeout=15, finish=("ag-a", 1.5))
    assert _changed_ids(result) == ["ag-a"], result
    _assert_pause_fields(result)


def test_p0_r6_3_pause_fields_on_timeout(runner):
    _running(runner, "ag-a")
    _pause(runner)
    result, _ = _wait(runner, ["ag-a"], timeout=2)
    _assert_pause_fields(result)


def test_p0_r6_3_pause_fields_when_nothing_is_running(runner):
    _pause(runner)
    result, _ = _wait(runner, None, timeout=5)
    _assert_pause_fields(result)


def test_p0_r6_3_no_pause_claimed_when_none_is_in_force(runner):
    _running(runner, "ag-a")
    result, _ = _wait(runner, ["ag-a"], timeout=15, finish=("ag-a", 1.5))
    assert result.get("paused") in (None, False), result


def test_p0_r6_3_an_expired_pause_is_not_reported(runner):
    _running(runner, "ag-a")
    runner.tree.pause(until=time.time() - 1, reason=PAUSE_REASON)
    result, _ = _wait(runner, ["ag-a"], timeout=15, finish=("ag-a", 1.5))
    assert result.get("paused") in (None, False), result
    assert _changed_ids(result) == ["ag-a"], result


# ---------------------------------------------------------------- P0-R6.4 --

def test_p0_r6_4_paused_with_nothing_running_returns_promptly(runner):
    _pause(runner)
    result, elapsed = _wait(runner, None, timeout=30)
    assert elapsed < 3, f"blocked {elapsed:.2f}s of a 30s timeout with nothing to wait on"
    _assert_still_running(result, [])
    assert result.get("paused") is True, result


def test_p0_r6_4_paused_with_only_finished_agents_named_returns_promptly(runner):
    _finished(runner, "ag-a")
    _pause(runner)
    result, elapsed = _wait(runner, ["ag-a"], timeout=30)
    assert elapsed < 3, elapsed
    _assert_still_running(result, [])
    assert result.get("paused") is True, result


def test_p0_r6_4_paused_with_an_unknown_id_named_returns_promptly(runner):
    """An id the tree has never heard of is not something to wait on."""
    _pause(runner)
    result, elapsed = _wait(runner, ["ag-nosuch"], timeout=30)
    assert elapsed < 3, elapsed
    _assert_still_running(result, [])
