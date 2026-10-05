"""Git effects commit their intent before I/O and checkpoint after I/O."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m3_adv import Harness
from multiagents import gitops
from multiagents.scheduler import effects
from multiagents.scheduler.results import Results, input_generation


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def unlocked(h):
    # Commit too: a retained read transaction can also block a writer's commit.
    with sqlite3.connect(h.service.store.file, timeout=0, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE meta SET value=value WHERE key='plan_revision'")
        db.execute("COMMIT")


def request(h, op, id, request_id=None, **args):
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    return h.service.request({"op": op, "request_id": request_id or op, "token": token,
                              "args": {"id": id, **args}})


def completed_child(h, kind):
    root, (child,) = h.tree(kind, 1)
    attempt, run = h.launch(child["id"])
    h.commit(run, {"output.txt": "work\n"})
    h.engine.finished(attempt, run)
    return root, child


@pytest.mark.parametrize("kind", ["sequence", "group"])
@pytest.mark.parametrize("trigger", ["composites", "close"])
@pytest.mark.parametrize("after_move", [False, True])
def test_nc_r5_r37_r61_completion_ref_intent_replays_without_early_publication(h, monkeypatch, kind, trigger, after_move):
    root, _ = completed_child(h, kind)
    if trigger == "close":
        root = h.nodes()[root["id"]]
        root.update(state="held", hold={"reason": "decision"})
        h.save(root)
    ref = f"refs/heads/node-generations/{root['id']}/1"
    original = Results.move
    crashed = False
    writes = []
    original_git = gitops.run

    def git(*args, **kwargs):
        unlocked(h)
        if "update-ref" in args and ref in args:
            writes.append(ref)
        return original_git(*args, **kwargs)

    def move(self, name, target, expected):
        nonlocal crashed
        unlocked(h)
        if name == ref and not crashed:
            crashed = True
            if after_move:
                original(self, name, target, expected)
            raise RuntimeError("crash at completion ref")
        return original(self, name, target, expected)

    monkeypatch.setattr(gitops, "run", git)
    monkeypatch.setattr(Results, "move", move)
    if trigger == "composites":
        with pytest.raises(RuntimeError, match="crash at completion ref"):
            h.engine.composites()
    else:
        result = request(h, "close_node", root["id"], revision=root["revision"], outcome="approved")
        assert result["error"]["error"] == "internal"
    pending = h.nodes()[root["id"]]
    intent = pending["completion_pending"]
    assert intent["ref"] == ref
    assert pending["state"] != "done" and not pending.get("published")
    assert not pending["generations"]
    assert input_generation(pending, {}, h.nodes()) is None
    assert request(h, "merge_node", root["id"])["error"]["error"] == "not_done"
    asyncio.run(h.engine.reconcile())
    asyncio.run(h.engine.reconcile())
    done = h.nodes()[root["id"]]
    assert done["state"] == "done" and done["outcome"] == "approved"
    assert "completion_pending" not in done
    assert len(done["generations"]) == 1 and writes == [ref]
    assert h.results.tip(ref) == intent["generation"]["commit"]
    with h.service.store.transaction(write=False) as db:
        transitions = [json.loads(raw) for raw, in db.execute("SELECT record FROM notifications")]
    assert sum(t["kind"] == "done" and t["node_id"] == root["id"] for t in transitions) == 1
    if trigger == "close":
        retry = request(h, "close_node", root["id"], revision=root["revision"], outcome="approved")
        assert retry["ok"] and retry["result"]["state"] == "done"


@pytest.mark.parametrize("kind", ["sequence", "group"])
def test_nc_r33_r39_r64_all_git_integration_publication_and_disposal_io_is_unlocked(h, monkeypatch, kind):
    original = gitops.run
    calls = []

    def git(*args, **kwargs):
        unlocked(h)
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(gitops, "run", git)
    root, _ = completed_child(h, kind)
    h.engine.composites()
    merged = request(h, "merge_node", root["id"])
    assert merged["ok"], merged
    assert merged["result"]["published"] == h.results.tip("HEAD")
    disposed = request(h, "dispose_node", root["id"], revision=h.nodes()[root["id"]]["revision"])
    assert disposed["ok"] and disposed["result"]["disposed"]
    assert calls


@pytest.mark.parametrize("after_move", [False, True])
def test_nc_r39_r61_publication_replays_the_journalled_commit_once(h, monkeypatch, after_move):
    root, _ = completed_child(h, "group")
    h.engine.composites()
    before = h.results.tip("HEAD")
    main_ref = h.results.git("symbolic-ref", "-q", "HEAD", check=True).out
    original = Results.move
    crashed = False

    def move(self, ref, target, expected):
        nonlocal crashed
        unlocked(h)
        if ref == main_ref and not crashed:
            crashed = True
            if after_move:
                original(self, ref, target, expected)
            raise RuntimeError("crash publishing")
        return original(self, ref, target, expected)

    monkeypatch.setattr(Results, "move", move)
    result = request(h, "merge_node", root["id"], request_id="publish")
    assert result["error"]["error"] == "internal"
    pending = h.nodes()[root["id"]]
    intent = pending["git_operation"]["publication"]
    assert not pending.get("published") and intent["before"] == before
    asyncio.run(h.engine.reconcile())
    target = h.results.tip("HEAD")
    assert target == intent["target"] != before
    assert not h.results.git("status", "--porcelain").out
    asyncio.run(h.engine.reconcile())
    assert h.results.tip("HEAD") == target
    retry = request(h, "merge_node", root["id"], request_id="publish")
    assert retry["ok"] and retry["result"]["published"] == target


def test_nc_r33_claim_preparation_and_admission_subprocesses_are_unlocked(h, monkeypatch):
    node = h.record()
    h.save(node)
    original = gitops.run

    def git(*args, **kwargs):
        unlocked(h)
        return original(*args, **kwargs)

    monkeypatch.setattr(gitops, "run", git)
    monkeypatch.setattr(h.engine.runner, "adopt", AsyncMock())
    monkeypatch.setattr(h.engine, "reconcile", AsyncMock())
    monkeypatch.setattr(h.engine.runner, "start", AsyncMock(return_value={"admitted": True, "provider": "fx", "model": "fx/m1"}))
    def spawn(attempt):
        unlocked(h)
        # Avoid waiting for a worker: this seam completes the reservation.
        from multiagents.scheduler.engine import save_attempt
        with h.service.store.transaction() as db:
            save_attempt(db, {**attempt, "state": "abandoned"})
    monkeypatch.setattr(h.engine, "spawn", spawn)
    asyncio.run(h.engine.tick())
    assert h.journal()
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    result = h.service.request({"op": "admit_agent", "request_id": "admission", "token": token,
                               "args": {"agent": "coder", "task": "admission"}})
    assert result["ok"], result
    assert result["result"].get("admitted"), result


def test_nc_r64_disposal_replays_after_partial_ref_deletion(h, monkeypatch):
    root, child = completed_child(h, "group")
    h.engine.composites()
    main = h.results.tip("HEAD")
    original = Results.git
    crashed = False

    def git(self, *args, **kwargs):
        nonlocal crashed
        unlocked(h)
        result = original(self, *args, **kwargs)
        if "update-ref" in args and "-d" in args and not crashed:
            crashed = True
            raise RuntimeError("crash disposing")
        return result

    monkeypatch.setattr(Results, "git", git)
    revision = h.nodes()[root["id"]]["revision"]
    result = request(h, "dispose_node", root["id"], request_id="dispose", revision=revision)
    assert result["error"]["error"] == "internal"
    assert not h.nodes()[root["id"]].get("disposed")
    assert h.nodes()[root["id"]]["git_operation"]["kind"] == "dispose"
    assert input_generation(h.nodes()[child["id"]], {}, h.nodes()) is None
    asyncio.run(h.engine.reconcile())
    asyncio.run(h.engine.reconcile())
    retry = request(h, "dispose_node", root["id"], request_id="dispose", revision=revision)
    assert retry["ok"] and retry["result"]["disposed"]
    assert h.results.tip("HEAD") == main
    assert not h.results.tip(f"refs/heads/nodes/{root['id']}")
    assert not h.results.tip(f"refs/heads/node-generations/{root['id']}/1")


def test_nc_r56_supervisor_start_observes_a_committed_steer_command(h, monkeypatch):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    spawned = []

    def spawn(current):
        unlocked(h)
        stored = h.journal()[attempt["attempt_id"]]
        assert stored["steer_commands"] == current["steer_commands"]
        spawned.append(stored)

    monkeypatch.setattr(h.engine, "spawn", spawn)
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    result = h.service.request({"op": "steer_run", "request_id": "steer", "token": token,
                               "args": {"run_id": run.id, "message": "continue"}})
    assert result["ok"] and result["result"].get("command_id"), result
    assert len(spawned) == 1


def test_nc_r61_r80_cancel_during_pending_completion_cannot_become_approved(h, monkeypatch):
    root, _ = completed_child(h, "group")
    original = Results.move
    crashed = False

    def move(self, ref, target, expected):
        nonlocal crashed
        if ref == f"refs/heads/node-generations/{root['id']}/1" and not crashed:
            crashed = True
            raise RuntimeError("crash before completion")
        return original(self, ref, target, expected)

    monkeypatch.setattr(Results, "move", move)
    with pytest.raises(RuntimeError, match="crash before completion"):
        h.engine.composites()
    pending = h.nodes()[root["id"]]
    result = request(h, "cancel_node", root["id"], revision=pending["revision"])
    assert result["ok"] and result["result"]["state"] == "cancelled", result
    asyncio.run(h.engine.reconcile())
    node = h.nodes()[root["id"]]
    assert node["state"] == "cancelled" and node["outcome"] is None
    assert not node.get("completion_pending") and not node.get("published")
    assert input_generation(node, {}, h.nodes()) is None


def test_nc_r61_reads_share_one_snapshot_while_git_checkpoints_can_write(h, monkeypatch):
    from contextlib import contextmanager
    from multiagents.scheduler.store import Store
    root, children = h.tree("group", 2)
    root["template"] = {"instance": "read-snapshot"}
    children[0]["session"] = "B"
    h.save(root, *children)
    original = Store.transaction
    depth = 0

    @contextmanager
    def transaction(self, **kwargs):
        nonlocal depth
        assert depth == 0, "nested snapshots can deadlock a waiting writer's commit"
        depth += 1
        try:
            with original(self, **kwargs) as db:
                yield db
        finally:
            depth -= 1

    monkeypatch.setattr(Store, "transaction", transaction)
    for op, args in (("list_nodes", {}), ("get_node", {"id": children[0]["id"]}), ("scheduler_status", {})):
        with h.service.store.transaction(write=False) as db:
            token = h.service.store.meta(db, "root_token")
        result = h.service.request({"op": op, "request_id": op, "token": token, "args": args})
        assert result["ok"], result


def test_nc_r5_nested_completion_advances_after_retention_in_the_same_evaluation(h):
    group, (child,) = h.tree("group", 1)
    sequence = h.record(kind="sequence", children=[group["id"]])
    group["parent"] = sequence["id"]
    h.save(sequence, group)
    attempt, run = h.launch(child["id"])
    h.commit(run, {"output.txt": "nested\n"})
    h.engine.finished(attempt, run)
    h.engine.composites()
    for node in (group, sequence):
        done = h.nodes()[node["id"]]
        assert done["state"] == "done" and done["outcome"] == "approved"
        assert h.results.tip(f"refs/heads/node-generations/{node['id']}/1") == done["generations"][0]["commit"]


@pytest.mark.parametrize("op", ["scheduler_status", "list_nodes", "get_node", "wait_for_nodes"])
def test_nc_r8_reads_do_not_wait_on_a_writer_holding_the_notification_condition(h, op):
    from concurrent.futures import ThreadPoolExecutor
    node = h.record()
    h.save(node)
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    args = {"id": node["id"]} if op == "get_node" else {"timeout": 0} if op == "wait_for_nodes" else {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        with h.service.changed, h.service.store.transaction():
            future = pool.submit(h.service.request, {"op": op, "request_id": op, "token": token, "args": args})
            result = future.result(timeout=1)
            assert result["ok"], result
