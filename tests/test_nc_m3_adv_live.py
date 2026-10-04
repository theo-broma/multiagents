"""Adversary, M3, against a live scheduler: merge_node conflict and retry
(NC-R39, NC-R86), and result capture of a run that packed its refs (NC-R33).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import err_code  # noqa: E402

WAIT_BOUND = 8.0


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    until = world.until

    def capped(pred, timeout: float = 30, *args, **kw):
        return until(pred, min(timeout, WAIT_BOUND), *args, **kw)

    world.until = capped
    yield world
    world.close()


def settled(w, node):
    return w.until(lambda: (n := w.get(node))["state"] in {"done", "held"} and n,
                   what="the node to settle")


def test_adv_merge_conflict_keeps_main_and_node_and_a_retry_after_resolution_merges(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "from the node\n"})
    w.done(node)
    w.commit_on_main("a.txt", "from main\n", "main took a.txt")
    main_before = w.main_tip()
    node_before = w.get(node)
    tip_before = w.tip(node)

    reply = w.rpc("merge_node", {"id": node}, request_id="m-1")
    assert err_code(reply) == "merge_conflict", reply
    assert w.main_tip() == main_before
    assert w.git("status", "--porcelain") == ""
    after = w.get(node)
    assert w.tip(node) == tip_before
    assert after["revision"] == node_before["revision"] and not after.get("published")

    # The orchestrator resolves: main drops its conflicting change.
    w.git("rm", "-q", "a.txt")
    w.git("commit", "-q", "-m", "resolve: drop main's a.txt")
    reply = w.rpc("merge_node", {"id": node}, request_id="m-2")
    assert reply.get("ok") is True, reply
    assert w.blob(w.main_tip(), "a.txt") == "from the node"
    assert w.get(node)["published"] == w.main_tip()


def test_adv_a_run_that_packs_its_refs_still_has_its_result_integrated(w):
    """`git gc` / `git pack-refs` in the run's own checkout is ordinary agent
    behaviour; the committed result must still be captured."""
    w.start_scheduler()
    node = w.coder("A", commit=False, fx={"shell": [
        "echo packed > packed.txt && git add -A && git commit -q -m packed "
        "&& git pack-refs --all"]})
    got = settled(w, node)
    assert got["state"] == "done" and got["outcome"] == "completed", (
        f"{got['state']} {got.get('hold')}")
    assert "packed.txt" in w.files(w.tip(node)), "the committed result was not integrated"


def test_adv_a_deleted_readonly_path_is_restored_before_integration(w):
    """Mutation guard: capture's readonly revert must cover deletions, not only
    modifications (dropping D from the diff filter survived the M3 suite)."""
    w.commit_on_main("protected.txt", "original\n", "protected base")
    w.start_scheduler()
    node = w.coder("A", commit=False, agent="guard", fx={"shell": [
        "git rm -q protected.txt && echo ok > ok.txt && git add -A && git commit -q -m del"]})
    got = settled(w, node)
    assert got["state"] == "done", f"{got['state']} {got.get('hold')}"
    assert w.blob(w.tip(node), "protected.txt") == "original"
    assert w.blob(w.tip(node), "ok.txt") == "ok"
