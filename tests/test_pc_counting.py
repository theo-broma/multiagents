"""PC-R2 / PC-R2a — counting and reservation across the whole project.
Contract: `context/specs/provider-concurrency.md` (amendments included).

In-process tests drive the server's tool functions over a project with blocking
fake CLIs (`pc_harness.Gated`): a run holds its slot while its CLI blocks, and
the test ends it by opening the gate, stopping it, or killing the process.
Cross-process tests use real `python -m multiagents.server` processes through
`sv_harness` (a root and a nested server, a stub agent that waits for a marker
file).

Assumptions (deliberately loose):
- "Queued": a `deferred: true` start result whose reason names
  `provider_concurrency` (PC-R3).
- A queued task is launched by `wait_for_agents` (PC-R3a drains there).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import pc_harness as pc  # noqa: E402
import sc_harness as sc  # noqa: E402
import sv_harness as sv  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = sc.World(tmp_path, monkeypatch)
    yield world
    for g in list(world.fakes.values()):
        if hasattr(g, "open"):
            g.open()
    world.down()


def world_with(w, limit=1, **kw):
    g = pc.gated(w, "acme", max_concurrent=limit, **kw)
    w.agent("worker", "acme", "acme/m1")
    g.close()
    w.up()
    return g


async def full(w, g, n=1):
    ids = [await w.started("worker", f"holder{i}") for i in range(n)]
    assert await pc.await_until(lambda: g.spawns() == n), "holders did not start"
    return ids


async def pump(w, pred, rounds=15):
    for _ in range(rounds):
        if pred():
            return True
        await w.server.wait_for_agents(timeout=2)
    return pred()


async def drain(w, rounds=6):
    for _ in range(rounds):
        await w.server.wait_for_agents(timeout=2)


# ------------------------------------------------------------- release (R2) --

def test_r2_a_finished_run_frees_its_slot(w):
    g = world_with(w)

    async def go():
        (a,) = await full(w, g)
        assert pc.deferred_for_pc(await w.start("worker", "blocked"))
        g.open()
        await w.until(a)
        # FIFO: the queued entry takes the freed slot first. Wait for it to
        # launch and to end, so nothing is queued or running when "after" comes.
        assert await pump(w, lambda: g.spawns() == 2), "the queued start never launched"
        await w.settle()
        return await w.start("worker", "after")
    r = asyncio.run(go())
    assert r.get("agent_id") and not r.get("deferred"), r


def test_r2_a_failed_run_frees_its_slot(w):
    g = world_with(w)
    pc_ctl = Path(g.base + ".ctl.json")

    async def go():
        import json
        ctl = json.loads(pc_ctl.read_text())
        ctl["exit"] = 3
        pc_ctl.write_text(json.dumps(ctl))
        (a,) = await full(w, g)
        assert pc.deferred_for_pc(await w.start("worker", "blocked"))
        g.open()
        await w.until(a)
        # FIFO: the queued entry takes the freed slot first. Wait for it to
        # launch and to end, so nothing is queued or running when "after" comes.
        assert await pump(w, lambda: g.spawns() == 2), "the queued start never launched"
        await w.settle()
        return await w.start("worker", "after")
    r = asyncio.run(go())
    assert r.get("agent_id") and not r.get("deferred"), r


def test_r2_a_stopped_run_frees_its_slot(w):
    g = world_with(w)

    async def go():
        (a,) = await full(w, g)
        await w.server.stop_agent(a)
        await w.until(a, timeout=20)
        return await w.start("worker", "after")
    r = asyncio.run(go())
    assert r.get("agent_id") and not r.get("deferred"), r


def test_r2_a_run_killed_with_sigkill_frees_its_slot(w):
    g = world_with(w)
    g.talk_first()      # it has said something, so no free retry relaunches it

    async def go():
        (a,) = await full(w, g)
        pid = g.pids()[0]
        pc.kill9(pid)
        assert await pc.await_until(lambda: not pc.alive(pid))
        # the existing liveness checks notice the death; no explicit action
        assert await pc.await_until(lambda: w.status(a) not in ("running", "starting"), 30)
        # One start only: admitted at once, or queued once and launched by the
        # release. A retry loop of starts would queue entries ahead of itself.
        r = await w.start("worker", "after")
        launched = await pump(w, lambda: g.spawns() == 2)
        return r, launched
    r, launched = asyncio.run(go())
    assert r.get("agent_id") and launched, r
    assert "after" in g.argv_text(1)


def test_r2_a_slot_is_held_for_the_whole_life_of_the_process(w):
    g = world_with(w)

    async def go():
        await full(w, g)
        await asyncio.sleep(2.0)
        return await w.start("worker", "still blocked")
    assert pc.deferred_for_pc(asyncio.run(go()))


# ---------------------------------------------------- which provider (R2) --

def test_r2_a_run_that_fell_back_counts_against_the_provider_it_ran_on(w):
    a = pc.gated(w, "acme", max_concurrent=1)
    b = pc.gated(w, "beta", max_concurrent=1)
    w.agent("worker", "acme", "acme/m1", models={"beta": "beta/b1"})
    w.agent("only-beta", "beta", "beta/b1")
    a.close(); b.close()
    w.up()

    async def go():
        first = await w.start("worker", "takes acme")
        await pc.await_until(lambda: a.spawns() == 1)
        second = await w.start("worker", "falls to beta")
        await pc.await_until(lambda: b.spawns() == 1)
        third = await w.start("only-beta", "beta is now full")
        return first, second, third
    first, second, third = asyncio.run(go())
    assert second.get("provider") == "beta", second
    assert pc.deferred_for_pc(third), third
    assert a.spawns() == 1


def test_r2a_one_provider_being_full_never_delays_another(w):
    a = pc.gated(w, "acme", max_concurrent=1)
    b = pc.gated(w, "beta", max_concurrent=1)
    w.agent("on-a", "acme", "acme/m1")
    w.agent("on-b", "beta", "beta/b1")
    a.close(); b.close()
    w.up()

    async def go():
        await w.start("on-a", "fills a")
        await pc.await_until(lambda: a.spawns() == 1)
        queued = await w.start("on-a", "queued on a")
        started = time.monotonic()
        other = await w.start("on-b", "free provider")
        ok = await pc.await_until(lambda: b.spawns() == 1, 10)
        return queued, other, ok, time.monotonic() - started
    queued, other, ok, took = asyncio.run(go())
    assert pc.deferred_for_pc(queued)
    assert other.get("agent_id") and not other.get("deferred"), other
    assert ok and took < 10


# -------------------------------------------- tree-wide limit, together (R2a) --

def tree_world(w, tree_limit, provider_limit):
    w.p.project["limits"]["max_concurrent"] = tree_limit
    a = pc.gated(w, "acme", max_concurrent=provider_limit)
    b = pc.gated(w, "beta", max_concurrent=provider_limit)
    w.agent("on-a", "acme", "acme/m1")
    w.agent("on-b", "beta", "beta/b1")
    a.close(); b.close()
    w.up()
    return a, b


def test_r2a_the_tree_wide_limit_still_applies_on_top(w):
    a, b = tree_world(w, 1, 5)

    async def go():
        await w.start("on-a", "one")
        await pc.await_until(lambda: a.spawns() == 1)
        r = await w.start("on-b", "two")
        await asyncio.sleep(1.0)
        return r
    r = asyncio.run(go())
    assert not (r.get("agent_id") and b.spawns()), "tree limit of 1 was ignored"
    assert b.spawns() == 0


def test_r2a_a_start_refused_by_the_tree_limit_leaves_no_provider_slot_held(w):
    a, b = tree_world(w, 1, 1)

    async def go():
        (first,) = [await w.started("on-a", "holder")]
        await pc.await_until(lambda: a.spawns() == 1)
        refused = await w.start("on-b", "refused by the tree limit")
        assert not (refused.get("agent_id") and not refused.get("deferred")), refused
        a.open()
        await w.until(first)
        await w.settle()
        b.close()
        after = await w.start("on-b", "after")
        return refused, after
    refused, after = asyncio.run(go())
    assert after.get("agent_id") and not after.get("deferred"), (refused, after)


def test_r2a_queued_work_holds_neither_a_provider_nor_a_tree_slot(w):
    a, b = tree_world(w, 2, 1)

    async def go():
        await w.start("on-a", "holder")
        await pc.await_until(lambda: a.spawns() == 1)
        queued = await w.start("on-a", "queued")
        other = await w.start("on-b", "uses the second tree slot")
        ok = await pc.await_until(lambda: b.spawns() == 1, 10)
        return queued, other, ok
    queued, other, ok = asyncio.run(go())
    assert pc.deferred_for_pc(queued), queued
    assert other.get("agent_id") and not other.get("deferred") and ok, other


# ----------------------------------------------------- lowering a limit (R2a) --

def test_r2a_lowering_a_limit_never_stops_running_agents_and_blocks_new_ones(w):
    g = world_with(w, limit=2)

    async def go():
        a, b = await full(w, g, 2)
        pc.set_limit(w, "acme", 1)
        blocked = await w.start("worker", "while over the limit")
        g.pids()
        statuses = (w.status(a), w.status(b))
        return a, b, blocked, statuses
    a, b, blocked, statuses = asyncio.run(go())
    assert pc.deferred_for_pc(blocked), blocked
    assert all(s in ("running", "starting") for s in statuses), statuses


def test_r2a_after_lowering_new_admissions_wait_until_the_count_is_under_the_limit(w):
    g = world_with(w, limit=2)
    g.hold("holder0")
    g.hold("holder1")

    async def go():
        a, b = await full(w, g, 2)
        pc.set_limit(w, "acme", 1)
        g.release("holder0")
        await w.until(a)
        await w.server.wait_for_agents(timeout=1)
        still = await w.start("worker", "one run still holds, limit 1")
        # one run left, limit 1: the count is not under the limit yet
        for _ in range(3):
            await w.server.wait_for_agents(timeout=1)
        early = g.spawns()
        g.release("holder1")
        await w.until(b)
        # now under the limit: the queued entry is the one admitted
        launched = await pump(w, lambda: g.spawns() == 3)
        return still, early, launched
    still, early, launched = asyncio.run(go())
    assert pc.deferred_for_pc(still), still
    assert early == 2, "a start launched while the count was at the limit"
    assert launched, "the queued start was not admitted once under the limit"
    assert "one run still holds" in g.argv_text(2)


@pytest.mark.parametrize("new", [2, None], ids=["raised", "removed"])
def test_r2a_raising_or_removing_the_limit_wakes_the_queue(w, new):
    g = world_with(w, limit=1)

    async def go():
        await full(w, g)
        queued = await w.start("worker", "queued")
        assert pc.deferred_for_pc(queued)
        pc.set_limit(w, "acme", new)
        g.open()
        await drain(w)
        return await pc.await_until(lambda: g.spawns() == 2, 20)
    assert asyncio.run(go()), "the queued start was not launched after the limit was raised"


def test_r2a_raising_the_limit_launches_the_queued_start_while_the_holder_still_runs(w):
    g = world_with(w, limit=1)

    async def go():
        await full(w, g)
        await w.start("worker", "queued")
        pc.set_limit(w, "acme", 2)
        for _ in range(8):
            await w.server.wait_for_agents(timeout=2)
            if g.spawns() == 2:
                break
        return g.spawns()
    assert asyncio.run(go()) == 2


# ----------------------------------------------- racing admissions (R2) --

def test_r2_two_concurrent_admissions_for_one_free_slot_admit_exactly_one(w):
    g = world_with(w, limit=1)

    async def go():
        rs = await asyncio.gather(*[w.start("worker", f"racer{i}") for i in range(6)])
        await pc.await_until(lambda: g.spawns() >= 1)
        await asyncio.sleep(1.0)
        return rs, g.spawns()
    rs, spawned = asyncio.run(go())
    assert spawned == 1, spawned
    assert sum(1 for r in rs if r.get("agent_id") and not r.get("deferred")) == 1, rs
    assert sum(1 for r in rs if pc.deferred_for_pc(r)) == 5, rs


# --------------------------------------------- real processes (R2, R2a) --

@pytest.fixture
def cp(tmp_path):
    made = []

    def build(limit=1):
        p = sv.Project(tmp_path)
        made.append(p)
        cfg = p.root / ".multiagents" / "config" / "providers.yaml"
        data = yaml.safe_load(cfg.read_text())
        data["providers"]["svstub"]["max_concurrent"] = limit
        cfg.write_text(yaml.safe_dump(data))
        return p
    yield build
    for p in made:
        p.cleanup()


def hold(p, name, talk=False):
    """`talk`: print some text first, so a holder killed while it waits has
    not "said nothing" and is not given the free retry."""
    first = [sv.text("sv-" + name, "working")] if talk else []
    return sv.plan_token(first + [["pidfile", str(p.marker(name + ".pid"))],
                                  ["wait_for", str(p.marker(name + ".go")), 60]])


def nested(p):
    parent = "ag-par001"
    p.tree.add(sv.Node(id=parent, agent="spawner", provider="svstub", model="m", parent=None,
                       depth=1, status="running", task="parent", session=sv.ORCH_SESSION,
                       started_at=sv.tree_now()))
    return p.server(agent_id=parent, depth=1)


def test_r2_runs_in_a_nested_server_count_against_the_roots_view(cp):
    p = cp(1)
    root = p.server()
    child = nested(p)
    held = child.start("held " + hold(p, "a"))
    sv.read_pid(p.marker("a.pid"))
    r = root.call("start_agent", 60, args={"agent": "worker", "task": "root start " + hold(p, "b")})
    assert r.get("deferred") and "provider_concurrency" in str(r.get("reason", "")), r
    assert len(p.invocations()) == 1
    assert held


def test_r2_a_nested_start_sees_the_roots_runs_too(cp):
    p = cp(1)
    root = p.server()
    child = nested(p)
    root.start("held " + hold(p, "a"))
    sv.read_pid(p.marker("a.pid"))
    r = child.call("start_agent", 60, args={"agent": "worker", "task": "child " + hold(p, "b")})
    assert r.get("deferred") and "provider_concurrency" in str(r.get("reason", "")), r
    assert len(p.invocations()) == 1


def test_r2_racing_admissions_in_separate_processes_admit_exactly_one(cp):
    import concurrent.futures as cf
    p = cp(1)
    servers = [p.server() for _ in range(4)]

    def go(i_s):
        i, s = i_s
        return s.call("start_agent", 90, args={"agent": "worker", "task": f"racer{i} " + hold(p, f"r{i}")})
    with cf.ThreadPoolExecutor(4) as pool:
        rs = list(pool.map(go, enumerate(servers)))
    time.sleep(1.5)
    admitted = [r for r in rs if r.get("agent_id") and not r.get("deferred")]
    assert len(admitted) == 1, rs
    assert len(p.invocations()) == 1, p.invocations()


def test_r2a_a_server_dying_does_not_release_its_live_agents_slot(cp):
    p = cp(1)
    first = p.server()
    first.start("held " + hold(p, "a"))
    pid = sv.read_pid(p.marker("a.pid"))
    first.kill()
    if not sv.alive(pid):
        pytest.skip("agents die with their server here; nothing to hold the slot")
    second = p.server()
    r = second.call("start_agent", 60, args={"agent": "worker", "task": "later " + hold(p, "b")})
    assert r.get("deferred") and "provider_concurrency" in str(r.get("reason", "")), r
    assert len(p.invocations()) == 1


def test_r2_an_agent_killed_with_sigkill_frees_its_slot_across_processes(cp):
    import os, signal
    p = cp(1)
    root = p.server()
    other = p.server()
    root.start("held " + hold(p, "a", talk=True))
    pid = sv.read_pid(p.marker("a.pid"))
    os.kill(pid, signal.SIGKILL)
    assert sv.wait_until(lambda: not sv.alive(pid), 10)

    # One start only: admitted at once, or queued once and launched by the
    # release. Re-starting in a loop would queue entries ahead of itself.
    r = other.call("start_agent", 60, args={"agent": "worker", "task": "after " + hold(p, "b")})

    def launched():
        if len(p.invocations()) >= 2:
            return True
        other.call("wait_for_agents", 30, args={"timeout": 2})
        return len(p.invocations()) >= 2
    assert r.get("agent_id") and sv.wait_until(launched, 60, 0.5), \
        ("the dead run's slot was never reclaimed", r)
    assert len(p.invocations()) == 2
