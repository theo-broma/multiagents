"""A durable Git effect that fails resolves its intent instead of killing the
scheduler loop: it holds the affected node with a reason, reports the failure
to the waiting client, or retries a bounded number of times (NC-R5, NC-R33,
NC-R39, NC-R61, NC-R64, NC-R86)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness
from multiagents import gitops
from multiagents.scheduler import effects
from multiagents.scheduler.results import Results


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def request(h, op, id, request_id=None, **args):
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    return h.service.request({"op": op, "request_id": request_id or op, "token": token,
                              "args": {"id": id, **args}})


def completed(h, files=None):
    root, (child,) = h.tree("group", 1)
    attempt, run = h.launch(child["id"])
    h.commit(run, files or {"output.txt": "work\n"})
    h.engine.finished(attempt, run)
    h.engine.composites()
    assert h.nodes()[root["id"]]["state"] == "done"
    return root, child


def journalled_publication(h, monkeypatch, root, after_move):
    """Leave a publish intent with its publication journalled, as a crash would."""
    main_ref = h.results.git("symbolic-ref", "-q", "HEAD", check=True).out
    original = Results.move
    crashed = False

    def move(self, ref, target, expected):
        nonlocal crashed
        if ref == main_ref and not crashed:
            crashed = True
            if after_move:
                original(self, ref, target, expected)
            raise RuntimeError("crash publishing")
        return original(self, ref, target, expected)

    monkeypatch.setattr(Results, "move", move)
    assert request(h, "merge_node", root["id"], request_id="publish")["error"]["error"] == "internal"
    monkeypatch.setattr(Results, "move", original)
    return h.nodes()[root["id"]]["git_operation"]["publication"]


def saved_reply(h, root, request_id):
    return request(h, "merge_node", root["id"], request_id=request_id)


def test_nc_r86_publication_conflict_after_the_intent_is_journalled_is_reported_not_raised(h, monkeypatch):
    root, _ = completed(h)
    journalled_publication(h, monkeypatch, root, after_move=False)
    revision = h.nodes()[root["id"]]["revision"]
    h.world.commit_on_main("manual.txt", "pushed by hand\n", "manual push")
    manual = h.results.tip("HEAD")
    asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert "git_operation" not in node and not node.get("published")
    assert node["state"] == "done" and node["revision"] == revision
    assert h.results.tip("HEAD") == manual
    reply = saved_reply(h, root, "publish")
    assert reply["error"]["error"] == "merge_conflict", reply
    # The intent is resolved: evaluation goes on, and a new request publishes.
    asyncio.run(h.engine.reconcile())
    retry = saved_reply(h, root, "publish-again")
    assert retry["ok"], retry
    assert retry["result"]["published"] == h.results.tip("HEAD") != manual
    assert h.results.git("merge-base", "--is-ancestor", manual, "HEAD").ok


def test_nc_r39_publication_replay_after_later_commits_on_main_is_already_published(h, monkeypatch):
    root, _ = completed(h)
    publication = journalled_publication(h, monkeypatch, root, after_move=True)
    h.results.git("read-tree", "-u", "-m", publication["before"], publication["target"], check=True)
    h.world.commit_on_main("later.txt", "after the publication\n", "later work")
    later = h.results.tip("HEAD")
    asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert node["published"] == publication["target"] and "git_operation" not in node
    assert h.results.tip("HEAD") == later


def test_nc_r86_publication_whose_checkout_update_fails_restores_main(h, monkeypatch):
    root, _ = completed(h, {"output.txt": "from the node\n"})
    publication = journalled_publication(h, monkeypatch, root, after_move=False)
    # Work the orchestrator started after the publication was prepared.
    untracked = h.world.root / "output.txt"
    untracked.write_text("mine, not committed\n")
    asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert "git_operation" not in node and not node.get("published")
    assert h.results.tip("HEAD") == publication["before"]
    assert untracked.read_text() == "mine, not committed\n"
    assert saved_reply(h, root, "publish")["error"]["error"] == "merge_conflict"


def test_nc_r86_publication_git_failure_is_a_merge_conflict_with_main_unchanged(h, monkeypatch):
    root, _ = completed(h)
    before = h.results.tip("HEAD")

    def broken(self, intent):
        raise gitops.GitError("update-ref: cannot lock ref")

    monkeypatch.setattr(Results, "apply_publication", broken)
    reply = request(h, "merge_node", root["id"], request_id="publish")
    assert reply["error"]["error"] == "merge_conflict", reply
    assert h.results.tip("HEAD") == before
    assert "git_operation" not in h.nodes()[root["id"]]


def test_nc_r5_completion_ref_in_an_unexpected_state_holds_the_node(h):
    root, (child,) = h.tree("sequence", 1)
    attempt, run = h.launch(child["id"])
    h.commit(run, {"output.txt": "work\n"})
    h.engine.finished(attempt, run)
    ref = f"refs/heads/node-generations/{root['id']}/1"
    foreign = h.results.tip("HEAD")
    h.results.git("update-ref", ref, foreign, check=True)
    h.engine.composites()
    asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert node["state"] == "held" and node["hold"]["reason"] == "integration_conflict"
    assert "completion_pending" not in node and not node["generations"]
    assert h.results.tip(ref) == foreign
    # Held composites are not completed again on every evaluation.
    h.engine.composites()
    assert h.nodes()[root["id"]]["revision"] == node["revision"]


def test_nc_r5_completion_failure_for_a_close_request_replies_with_the_held_node(h, monkeypatch):
    root, (child,) = h.tree("group", 1)
    attempt, run = h.launch(child["id"])
    h.commit(run, {"output.txt": "work\n"})
    h.engine.finished(attempt, run)
    node = h.nodes()[root["id"]]
    node.update(state="held", hold={"reason": "decision"})
    h.save(node)

    def move(self, ref, target, expected):
        raise gitops.GitError("cannot lock ref " + ref)

    monkeypatch.setattr(Results, "move", move)
    reply = request(h, "close_node", root["id"], revision=node["revision"], outcome="approved")
    assert reply["ok"], reply
    assert reply["result"]["state"] == "held"
    assert reply["result"]["hold"]["reason"] == "integration_conflict"
    assert "completion_pending" not in h.nodes()[root["id"]]


def test_nc_r64_disposal_failure_is_retried_a_bounded_number_of_times_then_holds(h, monkeypatch):
    root, child = completed(h)
    original = Results.git
    failing = True

    def git(self, *args, **kwargs):
        if failing and "update-ref" in args and "-d" in args:
            raise gitops.GitError("cannot lock ref")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Results, "git", git)
    revision = h.nodes()[root["id"]]["revision"]
    first = request(h, "dispose_node", root["id"], request_id="dispose", revision=revision)
    assert first["ok"], first
    assert h.nodes()[root["id"]]["git_operation"]["kind"] == "dispose"
    for _ in range(effects.DISPOSAL_TRIES):
        asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert "git_operation" not in node and not node.get("disposed")
    assert node["state"] == "held" and node["hold"]["reason"] == "disposal_failed"
    assert not any(n.get("disposal_pending") for n in h.nodes().values())
    reply = request(h, "dispose_node", root["id"], request_id="dispose", revision=revision)
    assert reply["error"]["error"] == "disposal_failed", reply
    failing = False
    retry = request(h, "dispose_node", root["id"], request_id="dispose-again", revision=node["revision"])
    assert retry["ok"] and retry["result"]["disposed"], retry


def test_nc_r33_integration_ref_failure_holds_the_node_and_records_the_attempt(h, monkeypatch):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    h.commit(run, {"output.txt": "work\n"})
    branch = f"refs/heads/nodes/{node['id']}"
    before = h.results.tip(branch)
    original = Results.move

    def move(self, ref, target, expected):
        if ref.startswith("refs/heads/node-generations/"):
            raise gitops.GitError("cannot lock ref " + ref)
        return original(self, ref, target, expected)

    monkeypatch.setattr(Results, "move", move)
    h.engine.finished(attempt, run)
    held = h.nodes()[node["id"]]
    assert held["state"] == "held" and held["hold"]["reason"] == "integration_conflict"
    assert h.journal()[attempt["attempt_id"]]["state"] == "recorded"
    assert h.results.tip(branch) == before == held["branch_tip"]
    asyncio.run(h.engine.reconcile())
    assert h.nodes()[node["id"]]["revision"] == held["revision"]


def test_nc_r33_launch_ref_failure_abandons_the_claim_and_holds_the_node(h, monkeypatch):
    node = h.record()
    h.save(node)
    monkeypatch.setattr(h.engine.runner, "adopt", AsyncMock())
    monkeypatch.setattr(h.engine, "reconcile", AsyncMock())
    monkeypatch.setattr(h.engine.runner, "start", AsyncMock(return_value={"admitted": True, "provider": "fx", "model": "fx/m1"}))
    spawned = []
    monkeypatch.setattr(h.engine, "spawn", spawned.append)

    def ensure_branch(self, node):
        raise gitops.GitError("node branch differs from its host-recorded tip")

    monkeypatch.setattr(Results, "ensure_branch", ensure_branch)
    asyncio.run(h.engine.tick())
    assert not spawned
    held = h.nodes()[node["id"]]
    assert held["state"] == "held" and held["hold"]["reason"] == "input_conflict"
    assert [a["state"] for a in h.journal().values()] == ["abandoned"]
    asyncio.run(h.engine.tick())
    assert len(h.journal()) == 1
