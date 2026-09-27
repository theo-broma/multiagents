"""tests/test_doom_loop_polling.py — bug-8615db.

agy's `manage_task` tool is how it checks on a task it started as a
background job. Checking on one already running (`Action: status`, or
`Action: list` for every task) reports the same arguments — the same
TaskId, in particular — on every poll while that job keeps running: that
repetition is the ordinary wait for a background command to finish, not a
doom loop, and must not trip the watchdog on its own.

The fix is scoped narrowly: `Supervisor.opaque_tool_args` (declared per
provider as `Provider.opaque_tool_args`, e.g. agy's entry in
`providers.yaml`) exempts a tool call only when it matches specific
argument values, unlike the coarser `opaque_tools` (SL-R6, `view_file`),
which exempts a tool name unconditionally. `manage_task {Action: run, ...}`
launching a task is deliberately NOT exempted: repeating that — the same
command, launched again and again — is exactly the loop doom_loop exists to
catch, and nothing here may sweep it in along with the status/list polling.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.providers import Event                            # noqa: E402
from multiagents.supervisor import Supervisor                      # noqa: E402

BIG = 10 ** 9

MANAGE_TASK_POLL_IS_OPAQUE = (
    {"tool": "manage_task", "match": {"Action": ["status", "list"]}},
)


def _sup(**kw) -> Supervisor:
    """A supervisor with every condition but doom_loop disabled, threshold 3."""
    kw.setdefault("silence_timeout", BIG)
    kw.setdefault("wall_timeout", BIG)
    kw.setdefault("max_steps", BIG)
    kw.setdefault("loop_repeats", 3)
    return Supervisor(**kw)


def _tool(name: str = "view_file", **args) -> Event:
    return Event(kind="tool", name=name, args=args or {"path": "/w/a.py"})


def _status_poll(task_id: str = "t-1") -> Event:
    return _tool("manage_task", Action="status", TaskId=task_id)


def _list_poll() -> Event:
    return _tool("manage_task", Action="list")


def _run_call(command: str = "pytest") -> Event:
    return _tool("manage_task", Action="run", Command=command)


def _feed(sup: Supervisor, events) -> list:
    return [t for t in (sup.observe(e) for e in events) if t is not None]


def _reasons(trips) -> list[str]:
    return [t.reason for t in trips]


def test_three_identical_manage_task_status_polls_do_not_trip():
    sup = _sup(opaque_tool_args=MANAGE_TASK_POLL_IS_OPAQUE)
    trips = _feed(sup, [_status_poll()] * 3)
    assert trips == [], trips


def test_three_identical_manage_task_list_polls_do_not_trip():
    sup = _sup(opaque_tool_args=MANAGE_TASK_POLL_IS_OPAQUE)
    trips = _feed(sup, [_list_poll()] * 3)
    assert trips == [], trips


def test_three_identical_view_file_calls_still_trip():
    """Positive control: the fix is scoped to manage_task's poll actions, not
    a general loosening of doom_loop — the watchdog still works."""
    sup = _sup(opaque_tool_args=MANAGE_TASK_POLL_IS_OPAQUE)
    trips = _feed(sup, [_tool()] * 3)
    assert _reasons(trips) == ["doom_loop"], trips


def test_three_identical_manage_task_run_calls_still_trip():
    """`Action: run` is deliberately left off the declared rule: re-launching
    the same command is a real loop and must not be exempted just because
    it shares a tool name with the legitimate status/list polling."""
    sup = _sup(opaque_tool_args=MANAGE_TASK_POLL_IS_OPAQUE)
    trips = _feed(sup, [_run_call()] * 3)
    assert _reasons(trips) == ["doom_loop"], trips


def test_manage_task_status_polls_trip_without_the_declared_rule():
    """Confirms the exemption comes from the declared per-provider rule, not
    from something about manage_task's name or shape."""
    sup = _sup()  # no opaque_tool_args configured
    trips = _feed(sup, [_status_poll()] * 3)
    assert _reasons(trips) == ["doom_loop"], trips
