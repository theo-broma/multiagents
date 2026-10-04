"""M2 adversary — a managed run that parks on a question is not a finished run.

A run that prints `NEED_DECISION(topic): question` is stopped by Runner and
recorded `awaiting_user`: its session is kept so `answer_question` resumes
the SAME run (tree.py: "the process has exited but the session is
resumable"). It has not failed and its work is not over.

- NC-R84: only a run that *fails* ends its node `done`/`failed`.
- NC-R26: a lock is released when the holder's run is over; a parked run
  resumes under the same run id, so releasing its lock lets a second holder
  in while the first can still be resumed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.world import World, run_id_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    yield world
    world.close()


QUESTION = "NEED_DECISION(schema): keep the v1 column or drop it?\nDEFAULT: keep it"


def parked(w: World, node: str) -> str:
    run = run_id_of(w.wait_running(node)["active_run"])
    w.until(lambda: w.tree_nodes()[run]["status"] == "awaiting_user", timeout=20,
            what="the run to park on its question")
    return run


def test_a_run_parked_on_a_question_does_not_end_its_node_failed(w):
    w.start_scheduler()
    a = w.simple("A", fx={"text": QUESTION})
    parked(w, a)
    w.quiet(3)
    node = w.get(a)
    assert not (node["state"] == "done" and node["outcome"] == "failed"), (
        "a run waiting for an answer was recorded as a failed node")


def test_a_parked_run_keeps_its_lock(w):
    w.start_scheduler()
    a = w.simple("A", locks=["schema"], fx={"text": QUESTION})
    parked(w, a)
    b = w.simple("B", locks=["schema"], fx={"gate": "gb"})
    w.quiet(3)
    assert w.fx.by_tag("B") == [], (
        f"lock 'schema' went to B while A's run is parked and resumable "
        f"(A is {w.get(a)['state']}/{w.get(a)['outcome']})")
