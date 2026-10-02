"""Unusual configuration and admission timing under PC-R1..R5.

All processes are local harness CLIs. No production provider is contacted.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import random
import sys
import threading
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import pc_harness as pc
import sc_harness as sc
import sv_harness as sv
from multiagents.config import deep_merge
from multiagents import budget
from multiagents import tree as tree_module
from multiagents.providers import load_providers
from multiagents.runner import ProviderFull
from multiagents.tree import Node


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch, at=None)
    # Concurrency has no calendar dependency. Leave real time visible to
    # subprocesses too, and disable shipped CLIs in this synthetic project.
    world.p.providers.update({name: {"enabled": False}
                             for name in ("claude", "codex", "opencode", "agy")})
    monkeypatch.setattr(budget, "read_all", lambda providers, *a, **kw: {
        name: budget.Budget(name, known=False)
        for name, provider in providers.items() if provider.enabled})
    yield world
    for g in world.fakes.values():
        if hasattr(g, "open"):
            g.open()
            for tag in g._holds:
                g.release(tag)
    world.down()


def build(w, limit=1):
    g = pc.gated(w, "acme", max_concurrent=limit)
    w.agent("worker", "acme", "acme/m1")
    w.agent("advisor", "acme", "acme/m1", conversational=True)
    w.p.project["limits"]["max_concurrent"] = 64
    w.up()
    return g


@pytest.mark.parametrize("literal", ["1.0", '"2"', "true", "false", ".nan", ".inf", "[]", "{}"])
def test_r1_yaml_types_are_not_coerced(literal):
    data = yaml.safe_load("acme:\n  bin: acme\n  max_concurrent: " + literal)
    with pytest.raises(ValueError, match="max_concurrent"):
        load_providers(data)


def test_r1_generated_large_integer_limits_and_null_overlay():
    rng = random.Random(14537)
    for bits in (1, 31, 53, 63, 127, 1024):
        value = max(1, rng.getrandbits(bits))
        raw = {"acme": {"bin": "acme", "max_concurrent": value},
               "child": {"extends": "acme", "bin": "child"}}
        providers = load_providers(raw)
        assert providers["acme"].max_concurrent == value
        assert providers["child"].max_concurrent is None
        overlaid = deep_merge(raw, {"acme": {"max_concurrent": None}})
        assert load_providers(overlaid)["acme"].max_concurrent is None


def test_r3a_generated_clock_rollbacks_do_not_reorder_durable_queue(w, monkeypatch):
    build(w)
    tree = w.tree()
    rng = random.Random(14537)
    expected = {"acme": [], "beta": []}
    for i in range(80):
        # Tied and decreasing wall timestamps are legitimate: FIFO must use
        # durable sequences, including after a new Tree instance opens disk.
        stamp = rng.choice([0.0, 1.0, 1000.0, -1.0])
        monkeypatch.setattr(tree_module, "now", lambda at=stamp: at)
        provider = rng.choice(list(expected))
        entry = tree.enqueue(provider, {"op": "start", "agent": "worker", "task": str(i)}, "full")
        expected[provider].append(entry["id"])
        if i % 7 == 0:
            tree = tree_module.Tree(tree.path, tree.events_path)
    for provider, ids in expected.items():
        entries = tree_module.pc_waiting(tree.read()["deferred"], provider)
        assert [entry["id"] for entry in entries] == ids
        assert [entry["seq"] for entry in entries] == list(range(1, len(ids) + 1))


def test_r3a_queued_start_keeps_its_selected_model_after_roster_reload(w, monkeypatch):
    build(w)
    runner = w.runner
    # Queue via the same producer used by start(), with its selected model
    # recorded. Inspect the dispatch arguments before any process is launched.
    result = runner._pc_queue_start(
        (ProviderFull("acme", 1, ["ag-holder"]), "acme/m1"),
        "worker", "queued-original-model", model=None, timeout=None,
        workdir=None, verifies="", budget_tag="")
    assert pc.deferred_for_pc(result)
    entry = runner.tree.read()["deferred"][0]
    w.p.agents["worker"]["model"] = "acme/m2"
    w.reload()
    selected = []

    async def inspect_start(agent, task, **kwargs):
        selected.append(kwargs.get("model") or w.runner.config.agent(agent).model)
        return {"agent_id": "ag-observed"}

    monkeypatch.setattr(w.runner, "start", inspect_start)
    asyncio.run(w.runner._pc_dispatch(entry))
    assert selected == ["acme/m1"], selected


def test_r3c_consult_deadline_is_timeout_plus_slack_while_conversation_lock_is_held(w, monkeypatch):
    build(w)
    g = w.fakes["acme"]
    # PC-R3c: the one deadline is start + timeout + slack. Shorten the slack
    # (CF-R7's 60 s) so the structure is checkable in seconds.
    monkeypatch.setattr(type(w.runner), "CONSULT_LOCK_SLACK_SECONDS", 2.0)
    timeout, slack = 1, 2.0

    async def go():
        async with w.runner._conversation_turn("advisor", 30) as held:
            assert held
            began = asyncio.get_running_loop().time()
            second = asyncio.create_task(
                w.server.consult("advisor", "expired-question", timeout=timeout))
            await asyncio.sleep(timeout + slack * 0.5)
            early = second.done()          # past `timeout`, before the deadline
            result = await asyncio.wait_for(second, timeout + slack + 5)
            return early, asyncio.get_running_loop().time() - began, result

    early, elapsed, result = asyncio.run(go())
    assert not early, "gave up before timeout + slack had passed"
    assert elapsed >= timeout + slack - 0.2, elapsed
    assert result.get("error"), result
    assert g.spawns() == 0, g.calls()
    assert not w.deferred(), w.deferred()


def test_r3d_queued_start_with_invalid_recorded_model_is_blocked_not_reresolved(w):
    g = build(w)
    runner = w.runner
    result = runner._pc_queue_start(
        (ProviderFull("acme", 1, ["ag-holder"]), "acme/m1"),
        "worker", "queued-invalid-model", model=None, timeout=None,
        workdir=None, verifies="", budget_tag="")
    assert pc.deferred_for_pc(result)
    # The roster moves on: m1 leaves the catalog and the agent now names m2.
    w.p.providers["acme"]["models_include"] = ["acme/m2"]
    w.p.agents["worker"]["model"] = "acme/m2"
    w.p.agents["advisor"]["model"] = "acme/m2"
    entry_id = result["deferred_id"]

    def settled():
        return any(e.get("id") == entry_id and e.get("blocked") for e in w.deferred())

    async def go():
        # Raising the limit wakes the queue with a free slot: the entry is tried.
        pc.set_limit(w, "acme", 3)
        return await pc.await_until(settled, timeout=10)

    assert asyncio.run(go()), (w.deferred(), g.spawns())
    entries = [e for e in w.deferred() if e.get("id") == entry_id]
    assert entries, w.deferred()
    assert g.spawns() == 0, g.calls()      # never launched, on m2 or anything
    assert not [n for n in w.runner.tree.read()["nodes"].values()
                if n.get("model") == "acme/m2"], "re-resolved to another model"


@pytest.mark.parametrize("new_limit", [3, None])
def test_r2a_reload_wakes_multiple_waiters_without_holder_release(w, monkeypatch, new_limit):
    build(w)
    runner = w.runner
    spec = runner.config.agent("worker")
    runner._admission_add(spec, Node(id="ag-holder", agent="worker", provider="acme",
                                    model="acme/m1", parent=None, depth=1, status="running"))
    for i in range(2):
        runner.tree.enqueue("acme", {"op": "start", "agent": "worker", "task": f"queued-{i}"}, "full")
    launched = []

    async def start(agent, task, queued, **kwargs):
        node_id = f"ag-queue{len(launched)}"
        runner._admission_add(spec, Node(id=node_id, agent=agent, provider="acme",
                             model="acme/m1", parent=None, depth=1, status="pending"), queued["id"])
        launched.append(node_id)
        return {"agent_id": node_id}

    monkeypatch.setattr(runner, "start", start)

    async def go():
        pc.set_limit(w, "acme", new_limit)
        assert await pc.await_until(lambda: len(launched) == 2, timeout=5)
        assert runner.tree.get("ag-holder").status == "running"
        assert not w.deferred()

    asyncio.run(go())


@pytest.mark.parametrize("limit", [None, 1, 3])
def test_r2_r5_threaded_admissions_do_not_exceed_limit_or_leak(w, limit):
    build(w, limit)
    runner = w.runner
    spec = runner.config.agent("worker")
    barrier = threading.Barrier(12)
    # Exercise the actual atomic reservation from separate threads. The
    # server's async launch/monitor tasks belong to its single event loop.
    def reserve(i):
        barrier.wait(timeout=10)
        node = Node(id=f"ag-race{i:02d}", agent="worker", provider="acme",
                    model="acme/m1", parent=None, depth=1, status="pending")
        try:
            runner._admission_add(spec, node)
        except ProviderFull:
            return None
        return node.id
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(12)))
    expected = 12 if limit is None else limit
    assert sum(r is not None for r in results) == expected, results
    assert len(runner._slot_holders(w.tree().read()["nodes"]).get("acme", [])) == expected
    if limit is None:
        assert not w.deferred()
        assert not pc.pc_events(w)
    for node_id in filter(None, results):
        runner.tree.set_status(node_id, "cancelled", "reservation abandoned before launch")
    assert not runner._slot_holders(w.tree().read()["nodes"]).get("acme")
    runner._admission_add(spec, Node(id="ag-after", agent="worker", provider="acme",
                                    model="acme/m1", parent=None, depth=1, status="pending"))


def test_r3a_transiently_blocked_head_keeps_sequence_and_does_not_block_tail(w, monkeypatch):
    build(w)
    runner = w.runner
    head = runner.tree.enqueue("acme", {"op": "start", "agent": "worker", "task": "head"}, "full")
    tail = runner.tree.enqueue("acme", {"op": "start", "agent": "worker", "task": "tail"}, "full")
    blocked = {"head"}
    launched = []

    async def start(agent, task, queued, **kwargs):
        if task in blocked:
            raise RuntimeError("auth temporarily unavailable")
        node_id = "ag-" + task
        runner._admission_add(runner.config.agent(agent),
                              Node(id=node_id, agent=agent, provider="acme", model="acme/m1",
                                   parent=None, depth=1, status="pending"), queued["id"])
        launched.append(task)
        return {"agent_id": node_id}

    monkeypatch.setattr(runner, "start", start)
    asyncio.run(runner._drain_queues())
    assert launched == ["tail"]
    entries = runner.tree.read()["deferred"]
    assert len(entries) == 1 and entries[0]["id"] == head["id"]
    assert entries[0]["seq"] == head["seq"] < tail["seq"]
    assert entries[0]["status"] == "waiting" and "auth" in entries[0]["blocked"]
    runner.tree.set_status("ag-tail", "cancelled", "test reservation released")
    blocked.clear()
    asyncio.run(runner._drain_queues())
    assert launched == ["tail", "head"]
    assert not runner.tree.read()["deferred"]


def test_r3a_queued_resume_dispatches_node_and_message_through_steer(w, monkeypatch):
    build(w)
    runner = w.runner
    node = Node(id="ag-resume", agent="worker", provider="acme", model="acme/m1",
                parent=None, depth=1, status="done", session_id="ses-kept", effort="high")
    runner.tree.add(node)
    entry = runner.tree.enqueue("acme", {"op": "resume", "node_id": node.id,
        "agent": "worker", "session_id": node.session_id, "model": node.model,
        "effort": node.effort, "message": "continue\nwith unicode: e\u0301"}, "full")
    observed = []

    async def steer(node_id, message, queued):
        observed.append((node_id, message, queued["spec"]))
        runner.tree.exit_deferred(queued["id"], "restarted", agent_id=node_id)
        return {"agent_id": node_id, "steered": True}

    async def forbidden_start(*args, **kwargs):
        pytest.fail("a queued resume was dispatched as a fresh start")

    monkeypatch.setattr(runner, "steer", steer)
    monkeypatch.setattr(runner, "start", forbidden_start)
    outcome, _ = asyncio.run(runner._pc_dispatch(entry))
    assert outcome == "launched"
    assert observed == [(node.id, entry["spec"]["message"], entry["spec"])]
    assert runner.tree.get(node.id).session_id == "ses-kept"
    assert runner.tree.get(node.id).effort == "high"


def test_r2_r3a_process_race_then_restart_preserves_fifo_and_releases_slots(tmp_path):
    p = sv.Project(tmp_path)
    try:
        cfg = p.root / ".multiagents" / "config" / "providers.yaml"
        data = yaml.safe_load(cfg.read_text())
        data["providers"]["svstub"]["max_concurrent"] = 1
        cfg.write_text(yaml.safe_dump(data))
        servers = [p.server() for _ in range(6)]

        def start(pair):
            i, server = pair
            sid = f"race-session-{i}"
            plan = sv.plan_token([sv.step_start(sid),
                                  ["pidfile", str(p.marker(f"r{i}.pid"))],
                                  ["wait_for", str(p.marker(f"r{i}.go")), 180],
                                  sv.text(sid, "finished normally")])
            return server.call("start_agent", 90, args={"agent": "worker", "task": f"racer-{i} " + plan})

        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            results = list(pool.map(start, enumerate(servers)))
        admitted = [i for i, r in enumerate(results) if r.get("agent_id") and not r.get("deferred")]
        assert len(admitted) == 1, results
        assert sv.wait_until(lambda: len(p.invocations()) == 1, 10)
        waiting = sorted(p.tree.read()["deferred"], key=lambda e: e["seq"])
        assert len(waiting) == 5
        assert len({e["seq"] for e in waiting}) == 5
        # Restart every server; queued work and ordering must come from disk.
        for server in servers:
            server.kill()
        root = p.server()
        previous = admitted[0]
        for expected in waiting:
            p.marker(f"r{previous}.go").write_text("go")
            task = expected["spec"]["task"]
            current = int(task.split()[0].split("-")[1])
            for _ in range(20):
                root.call("wait_for_agents", 20, args={"timeout": 1})
                if p.marker(f"r{current}.pid").exists():
                    break
            assert p.marker(f"r{current}.pid").exists(), (task, p.tree.read()["deferred"])
            unfinished = [i for i in range(6) if p.marker(f"r{i}.pid").exists()
                          and sv.alive(sv.read_pid(p.marker(f"r{i}.pid")))]
            assert unfinished == [current], unfinished
            previous = current
        p.marker(f"r{previous}.go").write_text("go")
        for _ in range(20):
            root.call("wait_for_agents", 20, args={"timeout": 1})
            if not p.tree.read()["deferred"] and not any(sv.alive(sv.read_pid(p.marker(f"r{i}.pid"))) for i in range(6)):
                break
        assert not p.tree.read()["deferred"]
        result = root.call("start_agent", 30, args={"agent": "worker", "task": "after-all"})
        assert result.get("agent_id") and not result.get("deferred"), result
    finally:
        for i in range(6):
            p.marker(f"r{i}.go").write_text("go")
        p.cleanup()
