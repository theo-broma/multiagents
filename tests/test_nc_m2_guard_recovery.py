"""M2 guards for surviving mutations and launch/recovery races (NC-R17..R61)."""
from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.world import World, run_id_of, call_tool  # noqa: E402
from multiagents.config import load  # noqa: E402
from multiagents.runner import Runner, ProviderFull  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts, save_attempt  # noqa: E402
from multiagents.scheduler.model import create_record  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.scheduler.store import issue_run_capability, token_hash  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402


@pytest.fixture
def local(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.write_config()
    service = Service(world.paths.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    yield world, engine
    for child in engine.children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
    engine.loop.close()
    world.close()


def deposit(engine, *, state="open", locks=()):
    node = create_record({"kind": "simple", "agent": "worker", "task": "work",
                          "locks": list(locks)}, "root")
    node["state"] = state
    if state == "held":
        node["hold"] = {"reason": "termination_unconfirmed"}
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    return node


def claim(engine, node, *, state="claimed"):
    record = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
              "node_id": node["id"], "run_id": "ag-" + uuid.uuid4().hex[:6],
              "state": state, "locks": node["locks"], "retry_count": 2, "at": now() - 5}
    with engine.store.transaction() as db:
        save_attempt(db, record)
    return record


def read(engine):
    with engine.store.transaction(write=False) as db:
        return engine.store.nodes(db), attempts(db)


def revoked(engine, token):
    with engine.store.transaction(write=False) as db:
        return db.execute("SELECT revoked FROM capabilities WHERE hash=?", (token_hash(token),)).fetchone()[0]


def test_nc_r26_a_claim_holds_its_named_locks_before_a_tree_run_exists(local):
    _, engine = local
    holder = deposit(engine, locks=["schema"])
    waiter = deposit(engine, locks=["schema"])
    claim(engine, holder)
    nodes, journal = read(engine)
    assert engine.lock_blockers(waiter, nodes, journal) == [{"code": "lock", "detail": ["schema"]}]
    assert not engine.view(waiter, nodes, journal)["eligible"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_nc_r26_r58_result_capture_and_revocation_require_confirmed_death(local, monkeypatch, confirmed):
    world, engine = local
    node = deposit(engine, state="running", locks=["schema"])
    attempt = claim(engine, node, state="launched")
    token = issue_run_capability(world.paths.root, attempt["run_id"], node["id"], {"read"})
    run = SimpleNamespace(id=attempt["run_id"], status="done", session_id="session",
                          branch="branch", turn_started_at=0)
    monkeypatch.setattr(engine.runner.tree, "get", lambda _: run)
    monkeypatch.setattr(engine, "spawn", lambda _: None)
    monkeypatch.setattr(engine.runner, "_steer_predecessor", lambda _: object())
    monkeypatch.setattr(engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=confirmed))
    asyncio.run(engine.reconcile())
    nodes, journal = read(engine)
    assert journal[attempt["attempt_id"]]["state"] == ("recorded" if confirmed else "launched")
    assert nodes[node["id"]]["state"] == ("done" if confirmed else "running")
    assert revoked(engine, token) == int(confirmed)


@pytest.mark.parametrize("state", ["open", "held", "cancelled", "done"])
def test_nc_r17_r58_missing_tree_evidence_revokes_tokens_without_reopening_terminal_nodes(local, state):
    world, engine = local
    node = deposit(engine, state=state)
    attempt = claim(engine, node)
    token = issue_run_capability(world.paths.root, attempt["run_id"], node["id"], {"read"})
    engine.paths.run_dir(attempt["run_id"]).mkdir(parents=True)
    asyncio.run(engine.reconcile())
    nodes, journal = read(engine)
    assert nodes[node["id"]]["state"] == ("held" if state == "open" else state)
    assert journal[attempt["attempt_id"]]["state"] == "claimed"
    assert revoked(engine, token) == 1


@pytest.mark.parametrize("state", ["held", "cancelled", "done"])
def test_nc_r17_recording_launch_evidence_never_reopens_a_stopped_node(local, state):
    _, engine = local
    node = deposit(engine, state=state)
    attempt = claim(engine, node)
    engine.launched(attempt, SimpleNamespace(id=attempt["run_id"]))
    nodes, journal = read(engine)
    assert nodes[node["id"]]["state"] == state
    assert journal[attempt["attempt_id"]]["state"] == "launched"
    assert len(nodes[node["id"]]["runs"]) == 1


@pytest.mark.parametrize("state", ["held", "cancelled", "done", "suspended"])
def test_nc_r56_resume_admission_preserves_non_running_node_states(local, state):
    _, engine = local
    node = deposit(engine, state=state)
    attempt = claim(engine, node, state="recorded")
    engine.runner.tree.add(Node(id=attempt["run_id"], agent="worker", provider="fx", model="fx/m1",
                                parent=None, depth=1, status="done", session_id="session"))
    with engine.store.transaction() as db:
        result = engine.resume_admission(attempt["run_id"], db)
    assert not result.get("admitted")
    assert result.get("error") or result.get("blocked")
    nodes, journal = read(engine)
    assert nodes[node["id"]]["state"] == state
    assert journal[attempt["attempt_id"]]["state"] == "recorded"


@pytest.mark.parametrize("change", ["state", "revision", "lock"])
def test_nc_r17_r26_claim_rechecks_changes_that_arrive_during_admission(local, monkeypatch, change):
    _, engine = local
    node = deposit(engine, locks=["schema"])
    probes = []

    async def admit(*args, **kwargs):
        probes.append(kwargs["launch_context"])
        with engine.store.transaction() as db:
            current = engine.store.nodes(db)[node["id"]]
            if change == "state":
                current["state"] = "cancelled"
            elif change == "revision":
                current["revision"] += 1
            else:
                holder = create_record({"kind": "simple", "agent": "worker", "task": "other",
                                        "locks": ["schema"]}, "root")
                engine.store.save_node(db, holder)
                save_attempt(db, {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
                                  "node_id": holder["id"], "run_id": "ag-other", "state": "claimed",
                                  "locks": ["schema"], "retry_count": 0, "at": now()})
            engine.store.save_node(db, current)
        return {"admitted": True}

    monkeypatch.setattr(engine.runner, "start", admit)
    monkeypatch.setattr(engine.runner, "adopt", AsyncMock())
    monkeypatch.setattr(engine, "reconcile", AsyncMock())
    spawned = []
    monkeypatch.setattr(engine, "spawn", spawned.append)
    asyncio.run(engine.tick())
    _, journal = read(engine)
    assert spawned == []
    assert len(probes) == 1 and probes[0].admission_only
    assert not any(a["node_id"] == node["id"] for a in journal.values())


def test_nc_r19_replaying_an_entry_after_the_store_commit_migrates_it_once(local):
    _, engine = local
    entry = engine.runner.tree.enqueue("fx", {"op": "start", "agent": "worker", "task": "work"}, "full")
    engine.migrate()
    with engine.runner.tree.transaction() as tree:
        tree["deferred"].append(copy.deepcopy(entry))
    engine.migrate()
    nodes, _ = read(engine)
    assert sum(n.get("migration_id") == entry["id"] for n in nodes.values()) == 1
    assert engine.runner.tree.read()["deferred"] == []


def test_nc_r18_probe_context_uses_the_creators_actual_depth(local):
    _, engine = local
    parent = Node(id="ag-parent", agent="worker", provider="fx", model="fx/m1", parent=None, depth=3)
    engine.runner.tree.add(parent)
    node = deposit(engine)
    node["created_by"] = parent.id
    context = engine.context(node, probe=True)
    assert context.caller == parent.id and context.run_parent == parent.id
    assert context.depth == 4 and context.admission_only


def test_nc_r17_slow_worker_boot_keeps_the_inherited_claim_lock(local, monkeypatch):
    _, engine = local
    node = deposit(engine)
    attempt = claim(engine, node)
    popen = subprocess.Popen

    def slow_boot(argv, **kwargs):
        return popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)

    monkeypatch.setattr("multiagents.scheduler.engine.subprocess.Popen", slow_boot)
    engine.spawn(attempt)
    asyncio.run(engine.reconcile())
    _, journal = read(engine)
    assert journal[attempt["attempt_id"]]["state"] == "claimed"


def test_nc_r56_unknown_start_keywords_return_a_structured_refusal(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        world.start_scheduler()
        runner = Runner(world.paths, load(world.paths, seed=False))
        result = asyncio.run(runner.start("worker", "work", wait_for_slot=False))
        assert isinstance(result, dict) and result.get("error") == "invalid"
        assert world.list() == []
    finally:
        world.close()


def test_nc_r54_r56_internal_start_metadata_is_not_forwarded_to_node_rpc(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        world.start_scheduler()
        runner = Runner(world.paths, load(world.paths, seed=False))
        refused = asyncio.run(runner.start("worker", "work", deferred_id="df-existing"))
        assert refused.get("blocked") and world.list() == []
        started = asyncio.run(runner.start("worker", task("A"), deferred_id="", queued=None,
                                           recorded_provider="", _cap_raced=True))
        assert started.get("node_id") and started.get("agent_id")
        assert len(world.list()) == 1
        assert len(world.fx.by_tag("A")) == 1
    finally:
        world.close()


def test_nc_r18_setup_failure_after_startup_claim_releases_it(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, gate=False)
    try:
        world.write_config()
        runner = Runner(world.paths, load(world.paths, seed=False))
        claimed = []
        finished = []
        monkeypatch.setattr(runner.startup, "claim", lambda provider, run: claimed.append((provider, run)) or "token")
        monkeypatch.setattr(runner, "_startup_finish", lambda provider, run, token: finished.append((provider, run, token)))

        def disk_full(*args):
            raise OSError("disk full")

        monkeypatch.setattr(runner, "_settle_effort", disk_full)
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(runner.start("worker", "work"))
        assert len(claimed) == 1
        assert finished == [(*claimed[0], "token")]
    finally:
        world.close()


def test_nc_r18_queue_write_failure_takes_no_startup_claim(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, gate=False)
    try:
        world.write_config()
        runner = Runner(world.paths, load(world.paths, seed=False))
        claimed = []
        monkeypatch.setattr(runner.startup, "claim", lambda *args: claimed.append(args) or "token")
        monkeypatch.setattr(runner, "_pc_full", lambda *args: ProviderFull("fx", 1, ["ag-holder"]))

        def disk_full(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(runner.tree, "enqueue", disk_full)
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(runner.start("worker", "work"))
        assert claimed == []
    finally:
        world.close()


def test_nc_r58_an_adopted_worker_revokes_by_subject_without_the_original_token_hash(local, monkeypatch):
    world, engine = local
    node = deposit(engine, state="running")
    attempt = claim(engine, node, state="launched")
    token = issue_run_capability(world.paths.root, attempt["run_id"], node["id"], {"read"})
    run = SimpleNamespace(id=attempt["run_id"], status="done")
    done = asyncio.Event()
    done.set()
    runner = SimpleNamespace(tree=SimpleNamespace(get=lambda _: run, active=lambda: []),
                             adopt=AsyncMock(), steer=AsyncMock(), shutdown=AsyncMock(),
                             runs={run.id: SimpleNamespace(done=done, capability_hash="")})
    monkeypatch.setattr("multiagents.scheduler.worker.Runner", lambda *args: runner)
    from multiagents.scheduler.worker import supervise
    asyncio.run(supervise(world.paths.root, attempt["attempt_id"]))
    assert revoked(engine, token) == 1


def test_nc_r58_worker_revokes_finished_run_capability_while_scheduler_is_dead(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        world.start_scheduler()
        node = world.simple("A", fx={"gate": "ga"})
        run = run_id_of(world.wait_running(node)["active_run"])
        store = Service(world.paths.root, now()).store
        with store.transaction(write=False) as db:
            assert db.execute("SELECT count(*) FROM capabilities WHERE subject=? AND revoked=0", (run,)).fetchone()[0] == 1
        world.kill9()
        world.gate("ga")

        def no_capability():
            with store.transaction(write=False) as db:
                return db.execute("SELECT count(*) FROM capabilities WHERE subject=? AND revoked=0", (run,)).fetchone()[0] == 0

        world.until(no_capability, what="independent worker token revocation")
    finally:
        world.close()


def test_nc_r54_disabling_the_gate_keeps_managed_steers_refused_and_new_starts_legacy(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    try:
        world.start_scheduler()
        node = world.simple("A", fx={"gate": "ga"})
        run = run_id_of(world.wait_running(node)["active_run"])
        world.project["scheduler"]["enabled"] = False
        world.write_config()
        refused = call_tool(world, "steer_agent", run, "continue")
        assert refused.get("error") == "scheduler_disabled"
        assert len(world.fx.by_tag("A")) == 1
        assert world.tree_nodes()[run]["status"] == "running"
        async def legacy_turn():
            from multiagents import server
            server._reset()
            started = await server.start_agent("worker", task("LEGACY"))
            assert started.get("agent_id") and not started.get("node_id")
            await asyncio.to_thread(world.until, lambda: world.fx.by_tag("LEGACY"),
                                    what="legacy launch while gate is off")
            await server.wait_for_agents([started["agent_id"]], timeout=10)

        asyncio.run(legacy_turn())
    finally:
        world.gate("ga")
        world.close()
