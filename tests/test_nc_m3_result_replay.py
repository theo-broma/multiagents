"""Deterministic M3 git/sqlite crash boundaries and object quarantine."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld
from multiagents import gitops
from multiagents.scheduler.engine import Engine, attempts, save_attempt
from multiagents.scheduler.model import create_record
from multiagents.scheduler.results import Results
from multiagents.scheduler.rpc import Service
from multiagents.tree import Node, now


@pytest.fixture
def activation(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    world.commit_on_main("protected.txt", "original\n")
    world.write_config()
    service = Service(world.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    node = create_record({"kind": "simple", "agent": "guard", "task": "work"}, "root")
    results = Results(world.paths, service.configuration())
    prepared = results.prepare(node, {node["id"]: node})
    run_id = "ag-" + uuid.uuid4().hex[:6]
    attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
               "run_id": run_id, "node_id": node["id"], "state": "launched",
               "locks": [], "at": now(), "readonly_paths": ["protected.txt"], **prepared}
    node.update(state="running", runs=[{"run_id": run_id, "attempt_id": attempt["attempt_id"],
                                       "input_commit": prepared["input_commit"]}])
    with service.store.transaction() as db:
        service.store.save_node(db, node)
        save_attempt(db, attempt)
    results.ensure_branch(node)
    checkout = world.paths.worktree(run_id)
    branch = "agents/guard/" + run_id[3:]
    results.create_checkout(checkout, branch, prepared["input_commit"])
    run = Node(id=run_id, agent="guard", provider="fx", model="fx/m1", parent=None, depth=1,
               branch=branch, worktree=str(checkout), status="done", node_id=node["id"],
               attempt_id=attempt["attempt_id"])
    engine.runner.tree.add(run)
    engine.runner.authority.add(run)
    yield world, engine, node, attempt, run, results
    engine.loop.close()
    world.close()


def commit(run, files):
    checkout = Path(run.worktree)
    for path, text in files.items():
        (checkout / path).write_text(text)
    gitops.run(checkout, "add", "-A", check=True)
    gitops.run(checkout, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
               "commit", "-m", "work", check=True)


def test_nc_r61_replay_after_branch_move_before_sql_commit_integrates_once(activation, monkeypatch):
    world, engine, node, attempt, run, _ = activation
    commit(run, {"work.txt": "work\n"})
    original = Results.move
    crashed = False

    def move(self, ref, sha, expected):
        nonlocal crashed
        original(self, ref, sha, expected)
        if ref == node["branch"] and not crashed:
            crashed = True
            raise RuntimeError("crash after branch move")

    monkeypatch.setattr(Results, "move", move)
    with pytest.raises(RuntimeError, match="crash after branch move"):
        engine.finished(attempt, run)
    after_move = world.tip(node["id"])
    with engine.store.transaction(write=False) as db:
        pending = attempts(db)[attempt["attempt_id"]]
        assert pending["state"] == "captured"
        assert engine.store.nodes(db)[node["id"]]["generations"] == []
    engine.integrate(pending)
    engine.integrate(pending)
    with engine.store.transaction(write=False) as db:
        done = engine.store.nodes(db)[node["id"]]
        transitions = [json.loads(raw)["kind"] for raw, in db.execute("SELECT record FROM notifications")]
    assert done["state"] == "done" and len(done["generations"]) == 1
    assert done["generations"][0]["commit"] == world.tip(node["id"]) == after_move
    assert transitions.count("integrated") == 1


def test_nc_r61_r65_capture_replay_keeps_the_first_readonly_restoration_commit(activation, monkeypatch):
    world, engine, node, attempt, run, results = activation
    commit(run, {"protected.txt": "tampered\n", "work.txt": "work\n"})
    original = Results.move
    crashed = False
    result_ref = "refs/heads/node-results/" + attempt["attempt_id"]

    def move(self, ref, sha, expected):
        nonlocal crashed
        original(self, ref, sha, expected)
        if ref == result_ref and not crashed:
            crashed = True
            raise RuntimeError("crash after capture ref")

    monkeypatch.setattr(Results, "move", move)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        engine.finished(attempt, run)
    captured = results.tip(result_ref)
    engine.finished(attempt, run)
    assert world.tip(node["id"]) == captured
    assert world.blob(captured, "protected.txt") == "original"
    with engine.store.transaction(write=False) as db:
        generation = engine.store.nodes(db)[node["id"]]["generations"][0]
    assert generation["readonly_reverted"] == ["protected.txt"]


def test_nc_r47_corrupt_result_objects_cannot_overwrite_the_project(activation):
    world, engine, node, attempt, run, _ = activation
    commit(run, {"work.txt": "work\n"})
    base = attempt["input_commit"]
    original = world.git("cat-file", "-p", base)
    forged = Path(run.worktree) / ".git" / "objects" / base[:2] / base[2:]
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_bytes(b"corrupt object")
    engine.finished(attempt, run)
    assert world.git("cat-file", "-p", base) == original
    assert world.tip(node["id"]) == base
    with engine.store.transaction(write=False) as db:
        held = engine.store.nodes(db)[node["id"]]
    assert held["state"] == "held" and held["generations"] == []


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("packed", [False, True])
def test_nc_r33_silent_run_counts_its_own_commits_and_not_inherited_work(activation, committed, packed):
    _, engine, _, _, run, _ = activation
    if committed:
        commit(run, {"work.txt": "work\n"})
    if packed:
        gitops.run(Path(run.worktree), "pack-refs", "--all", check=True)
    observation = SimpleNamespace(node_id=run.id, supervisor=SimpleNamespace(steps=0))
    assert engine.runner._did_work(observation) is committed


def test_nc_r61_missing_tree_captures_the_result_before_settling_the_activation(activation, monkeypatch):
    _, engine, node, attempt, run, _ = activation
    commit(run, {"work.txt": "work\n"})
    attempt.update(at=now() - 5, missing_tree_since=now() - 2)
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    monkeypatch.setattr(engine.runner.tree, "get", lambda _: None)
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=True))
    asyncio.run(engine.reconcile())
    with engine.store.transaction(write=False) as db:
        settled = attempts(db)[attempt["attempt_id"]]
        done = engine.store.nodes(db)[node["id"]]
    assert settled["state"] == "recorded" and settled["result"]["commit"]
    assert done["state"] == "done" and done["outcome"] == "failed"
    assert done["generations"] == []


def test_nc_r61_capture_does_not_hold_the_store_write_lock(activation, monkeypatch):
    _, engine, _, attempt, run, _ = activation
    original = Results.capture

    def capture(self, run, attempt, authority):
        with engine.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            assert current["capture_intent"]["id"] == run.id
            assert current["state"] == "launched"
        return original(self, run, attempt, authority)

    monkeypatch.setattr(Results, "capture", capture)
    engine.finished(attempt, run)


@pytest.mark.parametrize("after_capture", [False, True])
def test_nc_r61_reconcile_resumes_a_crash_during_capture(activation, monkeypatch, after_capture):
    _, engine, node, attempt, run, _ = activation
    commit(run, {"work.txt": "work\n"})
    original = Results.capture

    def crash(self, *args):
        if after_capture:
            original(self, *args)
        raise RuntimeError("crash during capture")

    with monkeypatch.context() as patch:
        patch.setattr(Results, "capture", crash)
        with pytest.raises(RuntimeError, match="crash during capture"):
            engine.finished(attempt, run)
    with engine.store.transaction(write=False) as db:
        pending = attempts(db)[attempt["attempt_id"]]
    assert pending["capture_intent"]["status"] == "done"
    asyncio.run(engine.reconcile())
    with engine.store.transaction(write=False) as db:
        settled = attempts(db)[attempt["attempt_id"]]
        done = engine.store.nodes(db)[node["id"]]
    assert settled["state"] == "recorded" and len(done["generations"]) == 1


@pytest.mark.parametrize("tree_entry", [False, True])
def test_nc_r17_r92_a_vanished_checkout_fails_without_holding(activation, monkeypatch, tree_entry):
    _, engine, node, attempt, run, _ = activation
    shutil.rmtree(run.worktree)
    attempt.update(at=now() - 5, missing_tree_since=now() - 2)
    with engine.store.transaction() as db:
        save_attempt(db, attempt)
    if not tree_entry:
        monkeypatch.setattr(engine.runner.tree, "get", lambda _: None)
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=True))
    monkeypatch.setattr(engine, "spawn", lambda _: None)
    asyncio.run(engine.reconcile())
    with engine.store.transaction(write=False) as db:
        done = engine.store.nodes(db)[node["id"]]
        settled = attempts(db)[attempt["attempt_id"]]
    assert done["state"] == "done" and done["outcome"] == "failed"
    assert done.get("hold") is None and done["generations"] == []
    assert settled["state"] == "recorded" and settled["result"]["failure"] == "missing_tree"


@pytest.mark.parametrize("missing_checkout", [False, True])
def test_nc_r92_a_loop_retries_a_missing_tree_only_once_per_round(activation, missing_checkout):
    _, engine, node, attempt, run, _ = activation
    reviewer = create_record({"kind": "simple", "agent": "guard", "task": "review"}, "root")
    loop = create_record({"kind": "loop", "children": [node["id"], reviewer["id"]],
                          "loop": {"verdict_child": reviewer["id"], "max_rounds": 2}}, "root")
    node["parent"] = reviewer["parent"] = loop["id"]
    if missing_checkout:
        shutil.rmtree(run.worktree)
    else:
        attempt["completion_proven"] = "missing_tree"
    with engine.store.transaction() as db:
        for entry in (node, reviewer, loop):
            engine.store.save_node(db, entry)
        save_attempt(db, attempt)
    engine.finished(attempt, run)
    with engine.store.transaction(write=False) as db:
        retried = engine.store.nodes(db)[node["id"]]
        assert retried["state"] == "open"
        assert engine.store.nodes(db)[loop["id"]]["state"] != "held"
    second = {**attempt, "attempt_id": uuid.uuid4().hex, "state": "launched"}
    retried.update(state="running")
    retried["runs"].append({"run_id": run.id, "attempt_id": second["attempt_id"]})
    with engine.store.transaction() as db:
        engine.store.save_node(db, retried)
        save_attempt(db, second)
    engine.finished(second, run)
    with engine.store.transaction(write=False) as db:
        held = engine.store.nodes(db)[loop["id"]]
    assert held["state"] == "held" and held["hold"]["reason"] == "run_failed"


def test_nc_r33_unknown_checkout_work_is_recorded_and_not_called_silent(activation):
    _, engine, _, _, run, _ = activation
    ref = Path(run.worktree) / ".git" / "refs" / "heads" / run.branch
    ref.write_text("not a commit\n")
    observation = SimpleNamespace(node_id=run.id, supervisor=SimpleNamespace(steps=0))
    assert engine.runner._did_work(observation) is True
    events = [json.loads(line) for line in engine.runner.tree.events_path.read_text().splitlines()]
    assert any(event["kind"] == "git_unreadable" for event in events)


def test_nc_r33_a_result_that_does_not_descend_from_its_input_is_refused(activation):
    world, engine, node, attempt, run, _ = activation
    checkout = Path(run.worktree)
    gitops.run(checkout, "checkout", "--orphan", "unrelated", check=True)
    gitops.run(checkout, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
               "commit", "--allow-empty", "-m", "unrelated history", check=True)
    unrelated = gitops.run(checkout, "rev-parse", "HEAD", check=True).out
    gitops.run(checkout, "update-ref", "refs/heads/" + run.branch, unrelated, check=True)
    engine.finished(attempt, run)
    with engine.store.transaction(write=False) as db:
        held = engine.store.nodes(db)[node["id"]]
        result = attempts(db)[attempt["attempt_id"]]["result"]
    assert held["state"] == "held" and held["generations"] == []
    assert "does not descend" in result["capture_error"]
    assert world.tip(node["id"]) == attempt["input_commit"]


@pytest.mark.parametrize("after_move", [False, True])
def test_nc_r33_r61_a_sibling_can_integrate_around_a_crashed_ref_move(activation, monkeypatch, after_move):
    world, engine, _, _, _, results = activation
    children = [create_record({"kind": "simple", "agent": "guard", "task": "work"}, "root") for _ in range(2)]
    group = create_record({"kind": "group", "children": [n["id"] for n in children]}, "root")
    for child in children:
        child["parent"] = group["id"]
    nodes = {n["id"]: n for n in [group, *children]}
    launches = []
    for child in children:
        prepared = results.prepare(child, nodes)
        run_id = "ag-" + uuid.uuid4().hex[:6]
        attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
                   "node_id": child["id"], "run_id": run_id, "state": "launched", "at": now(),
                   "locks": [], "readonly_paths": [], **prepared}
        child.update(state="running", runs=[{"run_id": run_id, "attempt_id": attempt["attempt_id"]}])
        with engine.store.transaction() as db:
            for node in nodes.values():
                engine.store.save_node(db, node)
            save_attempt(db, attempt)
        results.ensure_branch(group)
        checkout = world.paths.worktree(run_id)
        branch = "agents/guard/" + run_id[3:]
        results.create_checkout(checkout, branch, prepared["input_commit"])
        run = Node(id=run_id, agent="guard", provider="fx", model="fx/m1", parent=None, depth=1,
                   worktree=str(checkout), branch=branch, status="done", node_id=child["id"],
                   attempt_id=attempt["attempt_id"])
        engine.runner.tree.add(run)
        engine.runner.authority.add(run)
        launches.append((attempt, run))
    (first, one), (second, two) = launches
    commit(one, {"one.txt": "one\n"})
    commit(two, {"two.txt": "two\n"})
    original = Results.move

    def crash(self, ref, sha, expected):
        if ref == group["branch"]:
            if after_move:
                original(self, ref, sha, expected)
            raise RuntimeError("crash around branch move")
        return original(self, ref, sha, expected)

    with monkeypatch.context() as patch:
        patch.setattr(Results, "move", crash)
        with pytest.raises(RuntimeError, match="crash around branch move"):
            engine.finished(first, one)
    engine.finished(second, two)
    engine.integrate(first)
    assert {"one.txt", "two.txt"} <= world.files(world.tip(group["id"]))
    with engine.store.transaction(write=False) as db:
        for child in children:
            done = engine.store.nodes(db)[child["id"]]
            assert done["state"] == "done" and len(done["generations"]) == 1
