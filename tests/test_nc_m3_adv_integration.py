"""Adversary, M3: result capture and integration under crash/replay (NC-R33,
NC-R61).

The replay of a journalled integration is checked against the branch tip it
was computed from. A sibling that integrates in between makes the replay look
like a conflict even though the two results touch different files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness  # noqa: E402
from multiagents.scheduler.results import Results  # noqa: E402


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def test_adv_replayed_integration_after_a_sibling_integrated_is_not_a_conflict(h, monkeypatch):
    """Crash after the merge commit of child 1 is journalled, before the
    branch moves; child 2 (another file) integrates; child 1 is replayed.

    Sequence: finished(c1) -> crash before update-ref of nodes/<group>;
    finished(c2) -> integrated; integrate(c1) replay.
    """
    group, (c1, c2) = h.tree("group", 2)
    a1, r1 = h.launch(c1["id"])
    a2, r2 = h.launch(c2["id"])
    h.commit(r1, {"one.txt": "one\n"})
    h.commit(r2, {"two.txt": "two\n"})

    original = Results.move
    crashed = []

    def move(self, ref, sha, expected):
        if ref.startswith("refs/heads/nodes/") and not crashed:
            crashed.append(ref)
            raise RuntimeError("crash before branch move")
        return original(self, ref, sha, expected)

    monkeypatch.setattr(Results, "move", move)
    with pytest.raises(RuntimeError, match="crash before branch move"):
        h.engine.finished(a1, r1)
    monkeypatch.setattr(Results, "move", original)

    h.engine.finished(a2, r2)
    assert h.nodes()[c2["id"]]["generations"], "child 2 was not integrated"

    h.engine.integrate(h.journal()[a1["attempt_id"]])
    nodes = h.nodes()
    one = nodes[c1["id"]]
    assert one["state"] == "done", f"child 1 replay ended {one['state']} {one.get('hold')}"
    assert len(one["generations"]) == 1
    tip = h.world.tip(group["id"])
    assert {"one.txt", "two.txt"} <= h.world.files(tip), "child 1's result was lost"

