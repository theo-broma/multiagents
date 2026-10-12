"""Lapsed breakers must spend their trial on a launch, including scheduler runs.

A scheduler activation can refuse after claiming the trial but before spawning.
That leaves the same zero-failure record doctor describes as "cooldown lapsed;
the next run is the trial". The next admission must still get that trial. The
SQLite store, engine admission and provider subprocess are real; only the
initial, transient setup refusal is injected.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.world import World  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.renames import Renames  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts, save_attempt  # noqa: E402
from multiagents.scheduler.model import create_record  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.tree import Tree, now  # noqa: E402

TRIAL_TIMEOUT = 10
POLL_INTERVAL = .05
TRIAL_TAGS = ("A", "B", "C", "REFUSED")


async def until(observed, what, *, tick=None):
    """Wait for an observable boundary, driving the manual scheduler if needed."""
    deadline = time.monotonic() + TRIAL_TIMEOUT
    while True:
        value = observed()
        if value:
            return value
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"did not observe {what} within {TRIAL_TIMEOUT}s"
        if tick is not None:
            try:
                await asyncio.wait_for(tick(), timeout=remaining)
            except TimeoutError:
                pytest.fail(f"scheduler did not settle {what} within {TRIAL_TIMEOUT}s")
        await asyncio.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))


def scheduler_nodes(engine):
    with engine.store.transaction(write=False) as db:
        return engine.store.nodes(db)


@pytest.fixture(params=["pa", "opencode-go"], ids=["direct", "OG-R3-migrated"])
def world(tmp_path, monkeypatch, request):
    w = World(tmp_path, monkeypatch)
    provider = request.param
    script = tmp_path / "capacity.sh"
    script.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  check) exit 0 ;;\n'
        '  budget) echo \'{"known":true,"headroom":1.0}\' ;;\n'
        '  *) exit 64 ;;\n'
        'esac\n')
    fx = w.provider(provider, auth={"script": str(script)})
    w.agent("trial-worker", provider)
    w.write_config()
    tree = Tree(w.paths.tree_file, w.paths.events_file)
    record = provider if provider == "pa" else "opencode"
    # Write the pre-rename spelling directly: a Tree transaction would migrate
    # it before it became a durable pre-rename fixture.
    data = tree.read()
    data["provider_health"][record] = {
        "consecutive_failures": 0,
        "last_reason": "cooldown lapsed; the next run is the trial",
        "tripped": now() - 600,
    }
    data["cooldowns"][record] = {"until": now() - 1, "reason": "expired"}
    w.paths.tree_file.write_text(json.dumps(data))
    service = Service(w.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    health = engine.runner.tree.provider_health()
    assert health[provider]["consecutive_failures"] == 0
    assert engine.runner.tree.cooldown(provider) is None
    if provider != "pa":
        assert "opencode" not in health
        assert "opencode" not in engine.runner.tree.read()["cooldowns"]
        assert Tree(w.paths.tree_file, w.paths.events_file).provider_health() == health
    try:
        yield w, fx, provider, engine
    finally:
        fx.open_all_gates(TRIAL_TAGS)
        # The detached workers are ours; wait for their actual completion.
        deadline = time.monotonic() + TRIAL_TIMEOUT
        try:
            for child in engine.children:
                try:
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=TRIAL_TIMEOUT)
        finally:
            engine.loop.close()
            w.close()


def deposit(engine, provider, tag):
    node = create_record({
        "kind": "simple", "agent": "trial-worker",
        "task": task(tag, gate=tag),
        "pins": {"provider": provider, "model": f"{provider}/m1"},
    }, "root")
    with engine.store.transaction() as db:
        engine.store.save_node(db, node)
    return node


async def launch_trial(world):
    w, fx, provider, engine = world
    for tag in TRIAL_TAGS[:3]:
        deposit(engine, provider, tag)
    before = engine.runner.tree.provider_health()[provider].get("trial_at")
    reading = budget_mod.read_all(
        {provider: engine.runner.providers[provider]},
        lambda _: engine.runner.executor(), project_config=w.paths.config,
        cooldowns=engine.runner.tree.read()["cooldowns"])[provider]
    assert reading.known and reading.headroom == 1.0 and reading.usable
    # An engine probe is observational, including the migrated breaker.
    with engine.store.transaction(write=False) as db:
        node = next(iter(engine.store.nodes(db).values()))
    admitted = await engine.runner.start(
        node["agent"], node["task"], model=node["pins"]["model"],
        launch_context=engine.context(node, probe=True))
    assert admitted.get("admitted") is True, admitted
    assert engine.runner.tree.provider_health()[provider].get("trial_at") == before
    assert fx.spawns() == 0

    # A provider's calls log precedes the worker's launch publication. Wait
    # for the scheduler's running state too, before inspecting its run id.
    await until(lambda: fx.spawns() and any(
        n["state"] == "running" for n in scheduler_nodes(engine).values()),
        "the trial's provider spawn and scheduler launch", tick=engine.tick)
    assert fx.spawns() == 1, (engine.reasons, engine.runner.tree.provider_health())
    claimed = engine.runner.tree.provider_health()[provider]["trial_at"]
    await asyncio.wait_for(engine.tick(), timeout=TRIAL_TIMEOUT)
    nodes = scheduler_nodes(engine)
    running = [n for n in nodes.values() if n["state"] == "running"]
    waiting = [n for n in nodes.values() if n["state"] == "open"]
    assert len(running) == 1 and len(waiting) == len(nodes) - 1, nodes
    assert fx.spawns() == 1, "waiting pins must not launch alongside the trial"
    assert engine.runner.tree.provider_health()[provider]["trial_at"] == claimed
    assert all(engine.reasons[n["id"]][0]["code"] == "admission:refused"
               for n in waiting)

    trial_run = running[0]["runs"][-1]["run_id"]
    trial_result = w.paths.run_dir(trial_run) / "result.json"
    # Only the trial may finish. Later launches stay gated so their completion
    # cannot change the plan while the scheduler is admitting the other pins.
    fx.open_gate(fx.calls()[0]["tag"])
    # ST7: result.json precedes terminal status and final cleanup. Process exit
    # is the settled boundary for the detached supervisors this fixture owns;
    # neither a result file nor cleared breaker health proves that boundary.
    await until(lambda: all(child.poll() is not None for child in engine.children),
                "the trial's detached supervisors to exit")
    assert trial_result.is_file(), "the trial has not published its settled result"
    assert engine.runner.tree.get(trial_run).status == "done"
    assert not engine.runner.tree.provider_health()[provider].get("tripped")
    # A tick may defer a candidate when a concurrent launch changes its plan
    # snapshot. Keep this manually driven scheduler ticking, as its daemon
    # would, until the provider spawns and launch publications have settled.
    await until(lambda: fx.spawns() >= len(nodes) and all(
        n["state"] == "running" for n in scheduler_nodes(engine).values()
        if n["id"] != running[0]["id"]),
        "the waiting pins' provider spawns and scheduler launches", tick=engine.tick)
    assert fx.spawns() == len(nodes), "success must admit the waiting pins"
    fx.open_all_gates(TRIAL_TAGS)
    await until(lambda: all(child.poll() is not None for child in engine.children),
                "all admitted pins' detached supervisors to exit")


def test_lapsed_zero_failure_breaker_admits_one_scheduler_trial(world):
    asyncio.run(launch_trial(world))


@pytest.mark.parametrize("stage", ["begin", "settle", "checkout", "launch"])
def test_refused_scheduler_activation_keeps_the_lapsed_trial_available(
        world, monkeypatch, stage):
    _, fx, provider, engine = world
    node = deposit(engine, provider, "REFUSED")
    refused = []
    activations = []

    async def refuse(attempt):
        result = await engine.runner.start(
            node["agent"], node["task"], model=node["pins"]["model"],
            launch_context=engine.context(node, attempt))
        refused.append(result)
        with engine.store.transaction() as db:
            current = attempts(db)[attempt["attempt_id"]]
            current.update(state="abandoned", refusal=result, ended_at=now())
            save_attempt(db, current)

    def failure(*args, **kwargs):
        raise ValueError("transient activation setup failure")

    async def launch_failure(*args, **kwargs):
        failure()

    async def first_attempt():
        with monkeypatch.context() as patch:
            if stage == "begin":
                patch.setattr(engine.runner.tree, "begin_trial", failure)
            elif stage == "settle":
                patch.setattr(engine.runner, "_settle_effort", failure)
            elif stage == "checkout":
                patch.setattr("multiagents.scheduler.sessions.create_checkout", failure)
            else:
                patch.setattr(engine.runner, "_launch", launch_failure)
            patch.setattr(engine, "spawn", lambda a: activations.append(
                asyncio.create_task(refuse(a))))
            await asyncio.wait_for(engine.tick(), timeout=TRIAL_TIMEOUT)
            await asyncio.wait_for(asyncio.gather(*activations), timeout=TRIAL_TIMEOUT)

    asyncio.run(first_attempt())
    assert len(refused) == 1 and refused[0].get("blocked"), refused
    assert "transient activation setup failure" in str(refused[0]), refused
    assert fx.spawns() == 0
    health = engine.runner.tree.provider_health()[provider]
    assert health["consecutive_failures"] == 0 and health.get("tripped"), health
    assert engine.runner.tree.cooldown(provider) is None
    assert not json.loads(engine.runner.startup.file.read_text())[provider]["runs"]
    # The first node is still waiting; it and three more pins need one actual
    # trial between them, rather than all refusing a claim no run consumed.
    asyncio.run(launch_trial(world))


@pytest.mark.parametrize("replacement", ["new-owner", ""])
def test_a_late_trial_release_cannot_clear_a_replacement_claim(world, replacement):
    _, _, provider, engine = world
    tree = engine.runner.tree
    assert tree.claim_trial(provider, token="old-owner")
    assert tree.claim_trial(provider, window=0, token=replacement)
    before = tree.provider_health()[provider]
    assert not tree.release_trial(provider, "old-owner")
    assert tree.provider_health()[provider] == before
    assert tree.trial_pending(provider)
    tree.note_run_outcome(provider, ok=True)
    assert "trial_at" not in tree.provider_health()[provider]
    assert "trial_token" not in tree.provider_health()[provider]


@pytest.mark.parametrize("primary", ["old-route", "new-route"])
@pytest.mark.parametrize("owned", [False, True])
def test_og_r3_migration_keeps_the_release_token_with_its_claim(
        tmp_path, primary, owned):
    renames = Renames.from_blocks({"new-route": {"renamed_from": ["old-route"]}})
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl", renames=renames)
    secondary = "new-route" if primary == "old-route" else "old-route"
    health = {
        primary: {"consecutive_failures": 3, "tripped": now() - 600,
                  "trial_at": now(), **({"trial_token": "owner"} if owned else {})},
        secondary: {"consecutive_failures": 1, "tripped": now() - 1200,
                    "trial_at": now() - 30, "trial_token": "other"},
    }
    tree.path.write_text(json.dumps({"provider_health": health}))
    before = tree.provider_health()["new-route"]
    assert before["trial_at"] == health[primary]["trial_at"]
    assert not tree.release_trial("new-route", "other")
    assert tree.provider_health()["new-route"] == before
    assert tree.release_trial("new-route", "owner") is owned
    assert tree.trial_pending("new-route") is not owned
