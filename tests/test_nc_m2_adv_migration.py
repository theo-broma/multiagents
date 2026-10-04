"""M2 adversary — NC-R19/NC-R54 migration of queue entries that are not starts.

The legacy provider-concurrency queue holds more than `start` entries: a
steer that found its provider full queues `{"op": "resume", "node_id",
"session_id", "message"}` (Runner.steer), and a consult queues `{"op":
"consult", "task": <the message>}` held by the waiting process (`claim`).
Both carry `agent` and `task`, which is all migration looks at.

What must hold either way: a queued *resume* of an existing session is never
turned into a fresh run whose whole task is the steer message (the session
would never get the message, and a new agent would run on a one-line
instruction), and a consult's waiting turn is not launched as a task run.

Entries are written with today's `Tree.enqueue`, in their real shapes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.world import World, enable_gate, write_tree_entries  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, gate=False)
    world.pc = world.provider("pcfx", max_concurrent=1)
    world.agent("pcworker", "pcfx")
    yield world
    world.close()


def test_a_queued_resume_is_not_migrated_into_a_fresh_run_of_the_steer_message(w):
    message = task("R", "also fix the null check")

    def build(tree):
        tree.enqueue("pcfx", {"op": "resume", "node_id": "ag-1e9ac7", "agent": "pcworker",
                              "session_id": "ses_legacy", "model": "pcfx/m1", "pinned": False,
                              "effort": "", "message": message, "task": message},
                     "provider full")

    write_tree_entries(w, build)
    enable_gate(w)
    w.start_scheduler()
    w.quiet(3)
    fresh = [c for c in w.pc.by_tag("R") if c.get("resume") != "ses_legacy"]
    assert fresh == [], (
        "the queued resume of session ses_legacy was launched as a new run whose task "
        f"is the steer message: {[(c['resume'], c['session']) for c in fresh]}")


def test_a_queued_consult_is_not_migrated_into_a_task_run(w):
    question = task("C", "what does the parser do with tabs?")

    def build(tree):
        tree.enqueue("pcfx", {"op": "consult", "agent": "pcworker", "task": question[:500]},
                     "provider full", claim={"pid": os.getpid(), "at": 0, "start": ""})

    write_tree_entries(w, build)
    enable_gate(w)
    w.start_scheduler()
    w.quiet(3)
    assert w.pc.by_tag("C") == [], "a consult's queued turn was launched as a task run"
    assert all("[C]" not in (n.get("task") or "") for n in w.list()), (
        "a consult entry became a plan node")
