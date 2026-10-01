"""Batch M robustness: failure cleanup, competing admissions and clock steps.

All injected faults are local; no provider service is contacted. Clock cases
enumerate boundaries, and the generated age property uses seed 6023.
"""
from __future__ import annotations

import asyncio
import random
import threading
from types import SimpleNamespace

import pytest

from multiagents import budget as bmod
from multiagents import gitops
from test_m_routing_fixes import Aged, FULL, STANDING, Standing, _budgets


@pytest.fixture
def standing(tmp_path, monkeypatch):
    s = Standing(tmp_path, monkeypatch, cap=1, running=0)
    monkeypatch.setattr(s.runner, "_refresh_conversation", lambda *a: ("", "", None))
    return s


@pytest.mark.parametrize("stage", ["authority", "branch", "provider", "view", "turn_write"])
def test_reservation_is_released_on_prelaunch_exception(standing, monkeypatch, stage):
    """RM-R1a: faults between reservation and _launch must leave no slot."""
    runner = standing.runner

    def fault(*args, **kwargs):
        raise OSError("injected prelaunch fault")

    if stage == "authority":
        monkeypatch.setattr(runner, "authoritative", fault)
    elif stage == "branch":
        monkeypatch.setattr(runner, "unrecorded_branch_ok", fault)
    elif stage == "provider":
        monkeypatch.setattr(type(runner.providers["acme"]), "available", fault)
    elif stage == "view":
        monkeypatch.setattr(runner, "_worktree_view", fault)
    else:
        original = runner.tree.update

        def write(node_id, **changes):
            if "turns" in changes:
                return fault()
            return original(node_id, **changes)

        monkeypatch.setattr(runner.tree, "update", write)

    with pytest.raises(OSError, match="injected prelaunch fault"):
        asyncio.run(runner.consult("advisor", "q", timeout=1))
    assert standing.standing_node().status == "idle", "aborted turn leaked its reserved slot"


@pytest.mark.parametrize("exception", [RuntimeError, OSError, ValueError, asyncio.CancelledError])
def test_launch_exceptions_release_resumed_slot(standing, monkeypatch, exception):
    async def fault(*args, **kwargs):
        raise exception("injected launch fault")

    monkeypatch.setattr(type(standing.runner.executor()), "start", fault)
    if exception is RuntimeError:
        result = asyncio.run(standing.runner.consult("advisor", "q", timeout=1))
        assert result.get("error")
    else:
        with pytest.raises(exception):
            asyncio.run(standing.runner.consult("advisor", "q", timeout=1))
    assert standing.standing_node().status in {"idle", "failed"}
    with standing.runner.startup._lock():
        records = standing.runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records


def test_worktree_recreation_failure_releases_slot(standing, monkeypatch):
    runner = standing.runner
    runner.tree.update(STANDING, worktree=str(runner.paths.worktree("ag-absent")))

    def fault(*args, **kwargs):
        raise gitops.GitError("injected worktree failure")

    monkeypatch.setattr(gitops, "create_worktree", fault)
    with pytest.raises(gitops.GitError):
        asyncio.run(runner.consult("advisor", "q", timeout=1))
    assert standing.standing_node().status == "idle"


def test_unavailable_provider_releases_slot(standing, monkeypatch):
    monkeypatch.setattr(type(standing.runner.providers["acme"]), "available", lambda self: False)
    with pytest.raises(FileNotFoundError):
        asyncio.run(standing.runner.consult("advisor", "q", timeout=1))
    assert standing.standing_node().status == "idle"


@pytest.mark.parametrize("stage", ["authority", "branch"])
def test_refused_refresh_releases_slot(standing, monkeypatch, stage):
    if stage == "authority":
        monkeypatch.setattr(standing.runner, "authoritative", lambda *a: None)
    else:
        monkeypatch.setattr(standing.runner, "unrecorded_branch_ok", lambda *a: False)
    result = asyncio.run(standing.runner.consult("advisor", "q", timeout=1))
    assert result.get("error")
    assert standing.standing_node().status == "idle"


def test_start_competing_with_two_resumes_cannot_overbook(standing, monkeypatch):
    """RM-R1a: start has passed preflight but is still awaiting telemetry."""
    from dataclasses import replace

    runner = standing.runner
    runner.config.agents["critic"] = runner.config.agents["advisor"].replace(name="critic")
    other = replace(standing.standing_node(), id="ag-critic", agent="critic", session_id="sess-critic")
    runner.tree.add(other)
    entered, telemetry_release = threading.Event(), threading.Event()
    observed = []

    def telemetry(*args, **kwargs):
        entered.set()
        assert telemetry_release.wait(10), "test telemetry barrier timed out"
        return {"acme": bmod.Budget("acme", known=True, headroom=1.0)}

    monkeypatch.setattr(bmod, "read_all", telemetry)

    async def compete():
        consult_launched, release_consult = asyncio.Event(), asyncio.Event()

        async def launch(**kwargs):
            observed.append(sum(n.get("status") in {"pending", "running"}
                                for n in runner.tree.read()["nodes"].values()))
            runner.tree.set_status(kwargs["node_id"], "running")
            if kwargs.get("session_id"):
                consult_launched.set()
                await release_consult.wait()
            done = asyncio.Event()
            done.set()
            return SimpleNamespace(handle=None, done=done, awaiting=None, text_parts=[], ticket=None)

        monkeypatch.setattr(runner, "_launch", launch)
        task = asyncio.create_task(runner.start("worker", "q"))
        first = None
        waiting = None
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            first = asyncio.create_task(runner.consult("advisor", "q", timeout=1))
            waiting = asyncio.create_task(consult_launched.wait())
            completed, _ = await asyncio.wait({first, waiting}, timeout=10,
                                               return_when=asyncio.FIRST_COMPLETED)
            assert completed, "consult neither refused nor reached launch"
            if first in completed:
                try:
                    result = first.result()
                    assert result.get("error"), result
                except RuntimeError as exc:
                    assert "max_concurrent=1" in str(exc)
            with pytest.raises(RuntimeError, match="max_concurrent=1"):
                await runner.consult("critic", "q", timeout=1)
            telemetry_release.set()
            try:
                await asyncio.wait_for(task, 10)
            except RuntimeError as exc:
                assert "max_concurrent=1" in str(exc)
        finally:
            telemetry_release.set()
            release_consult.set()
            if waiting is not None:
                waiting.cancel()
            await asyncio.gather(task, *([first] if first is not None else []),
                                 *([waiting] if waiting is not None else []), return_exceptions=True)

    asyncio.run(compete())
    assert max(observed) <= 1, f"max_concurrent=1 admitted occupancy {observed}"


@pytest.mark.parametrize("stage", ["limits", "worktree", "prompt", "launch"])
@pytest.mark.parametrize("exception", [RuntimeError, OSError, asyncio.CancelledError])
def test_startup_claim_is_released_after_failure(standing, monkeypatch, stage, exception):
    runner = standing.runner
    _budgets(monkeypatch, acme=1.0)

    def fault(*args, **kwargs):
        raise exception("injected start fault")

    if stage == "limits":
        monkeypatch.setattr(runner, "_limits_detail", fault)
    elif stage == "worktree":
        monkeypatch.setattr(gitops, "create_worktree", fault)
    elif stage == "prompt":
        monkeypatch.setattr(runner, "compose_prompt", fault)
    else:
        async def launch(**kwargs):
            return fault()
        monkeypatch.setattr(runner, "_launch", launch)

    async def run():
        try:
            return await runner.start("worker", "q")
        except exception:
            return None

    asyncio.run(run())
    with runner.startup._lock():
        records = runner.startup._read()
    assert not (records.get("acme") or {}).get("runs"), records


@pytest.mark.parametrize("future", [300.0, 300.001, 301.0, 3600.0, 1_800_000_000_000.0])
def test_future_read_at_routes_as_unknown(tmp_path, monkeypatch, future):
    wall = 1_800_000_000.0
    monkeypatch.setattr(bmod.time, "time", lambda: wall)
    a = Aged(tmp_path, monkeypatch, {**FULL, "read_at": wall + future})
    from multiagents.paths import global_config_dir
    reading = bmod.read_provider("acme", a.runner.providers["acme"],
                                 a.runner.executor(), global_config_dir(), a.runner.paths.config)
    assert reading.stale == (future > 300.0)
    assert reading.usable == (future > 300.0)


def _aging_script(monkeypatch, wall, field, calls):
    """A provider whose reading ages with the patched clock: each call reports
    the same underlying measurement (taken 3590 s before `wall` at start), and
    carries a call number in `note` so a re-read is distinguishable."""
    taken = wall[0] - 3590

    def script(*a):
        calls.append(wall[0])
        extra = ({"read_at": taken} if field == "read_at"
                 else {"stale_seconds": wall[0] - taken})
        return bmod.Budget("acme", known=True, headroom=0.0, note=f"read{len(calls)}", **extra)

    monkeypatch.setattr(bmod, "_from_script", script)


def test_cached_timestamp_reading_does_not_get_younger_after_clock_step(tmp_path, monkeypatch):
    """RM-R4f: a backward step invalidates the entry and re-reads in the same call."""
    wall = [1_800_000_000.0]
    calls = []
    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    _aging_script(monkeypatch, wall, "read_at", calls)
    bmod.invalidate_cache()
    first = bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600)
    assert not first.stale and first.note.startswith("read1")
    wall[0] += 20                       # inside the TTL: a plain cache hit, now expired
    assert bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600).stale
    assert len(calls) == 1
    wall[0] -= 5                        # the step: the reading is honestly 3605 s old
    second = bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600)
    assert len(calls) == 2, "a backward step did not trigger a re-read"
    assert second.note.startswith("read2"), "the invalidated budget was returned"
    assert second.stale, "backward clock step made the timestamp reading younger"


@pytest.mark.parametrize("field", ["read_at", "stale_seconds"])
def test_reading_stays_expired_when_clock_steps_back_within_cache_lifetime(tmp_path, monkeypatch, field):
    """RM-R4d/RM-R4f: a step that does not cross the cache stamp still forces a
    same-call re-read; the re-read, aged by the same clock, is still expired."""
    wall = [1_800_000_000.0]
    calls = []
    monkeypatch.setattr(bmod.time, "time", lambda: wall[0])
    _aging_script(monkeypatch, wall, field, calls)
    bmod.invalidate_cache()
    assert not bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600).stale
    wall[0] += 20
    assert bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600).stale
    assert len(calls) == 1
    wall[0] -= 5
    after = bmod.read_provider("acme", object(), None, tmp_path, max_reading_age=3600)
    assert len(calls) == 2, f"{field}: a backward step did not trigger a re-read"
    assert after.note.startswith("read2"), f"{field}: the invalidated budget was returned"
    assert after.stale, (
        f"{field}: an expired reading became authoritative after a backward step")


def test_generated_timestamp_ages_never_decrease_after_backward_steps():
    rng = random.Random(6023)
    now = 1_800_000_000.0
    for _ in range(100):
        age = rng.uniform(600, 3599)
        backwards = rng.uniform(1, 299)
        reading = bmod.Budget("acme", known=True, headroom=0.0, read_at=now - age)
        before = bmod._reading_age(reading, now, cached_at=now)
        after = bmod._reading_age(reading, now - backwards, cached_at=now)
        assert after >= before, f"seed=6023 age={age} backwards={backwards}: {before} -> {after}"
