"""SF-R3: round-7 cleanup recovery must finish, not merely retain ownership."""
from __future__ import annotations

import asyncio

import pytest

import test_sf_review_r3 as h
from multiagents.startup import StartupUnavailable

w = h.w


def releases(runner, monkeypatch):
    seen = []
    real = runner._release

    def release(aid):
        if aid in runner._locks:
            seen.append(aid)
        return real(aid)
    monkeypatch.setattr(runner, "_release", release)
    return seen


def assert_finished(w, aid, seen):
    runner = w.runner
    runner._settle_holds()
    runner._settle_holds()
    assert aid not in runner._holds
    assert aid not in runner._locks
    assert not h.lock_held(runner, aid)
    assert seen.count(aid) == 1
    assert not w.node(aid).cleanup_hold
    assert w.node(aid).slot_owner is None
    assert runner.provider_slots()["acme"]["in_use"] == 0


def test_failed_capture_is_recaptured_and_cleanup_completes(w, monkeypatch):
    g = h.steer_world(w)
    seen = releases(w.runner, monkeypatch)

    async def go():
        aid = await h.running_with_session(w, g)
        h.half_open(w.runner, "acme")
        run = w.runner.runs[aid]
        original = run.spec.executor
        run.spec.executor = "bogus"
        with pytest.raises(ValueError):
            await w.server.steer_agent(aid, "steerhold: go")
        assert h.lock_held(w.runner, aid)
        assert w.runner.provider_slots()["acme"]["in_use"] == 1
        run.spec.executor = original
        g.open()
        assert await h.pc.await_until(lambda: not h.pc.alive(g.pids()[0]), 20)
        return aid
    aid = asyncio.run(go())
    assert_finished(w, aid, seen)


def test_startup_refusal_retries_queue_restore_until_it_completes(w, monkeypatch):
    g = h.steer_world(w)
    state = {"recover": False, "restored": 0}

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        entry = w.tree().enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "session_id": w.node(aid).session_id, "model": "acme/m1",
                     "pinned": False, "effort": "", "message": "resume me",
                     "task": "resume me"}, "test", deferred_by="test",
            dispatcher=w.runner._owner_fields())
        # Own an inherited lock so the refusal must release it once too.
        assert w.runner._claim(aid)
        seen = releases(w.runner, monkeypatch)
        real_restore = w.runner.tree.restore_deferred

        def restore(record):
            if not state["recover"]:
                raise OSError("restore unavailable")
            restored = real_restore(record)
            state["restored"] += int(restored)
            return restored

        def claim(*args):
            raise StartupUnavailable("acme", 0)
        monkeypatch.setattr(w.runner.tree, "restore_deferred", restore)
        monkeypatch.setattr(w.runner.startup, "claim", claim)
        outcome, info = await w.runner._pc_dispatch(entry)
        assert outcome == "blocked", (outcome, info)
        assert not any(d["id"] == entry["id"] for d in w.deferred())
        state["recover"] = True
        return aid, entry, seen
    aid, entry, seen = asyncio.run(go())
    assert_finished(w, aid, seen)
    restored = [d for d in w.deferred() if d["id"] == entry["id"]]
    assert len(restored) == 1
    assert restored[0]["seq"] == entry["seq"]
    assert state["restored"] == 1


def test_failed_unreserve_keeps_an_owner_until_reservation_is_released(w, monkeypatch):
    g = h.steer_world(w)
    state = {"recover": False}
    seen = releases(w.runner, monkeypatch)
    real_unreserve = w.runner._pc_unreserve

    def unreserve(*args):
        if not state["recover"]:
            raise OSError("reservation unavailable")
        return real_unreserve(*args)
    monkeypatch.setattr(w.runner, "_pc_unreserve", unreserve)

    async def go():
        aid = await h.running_with_session(w, g)
        h.half_open(w.runner, "acme")
        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.sleep(60)
        h.on_predecessor_stop(w, monkeypatch, hang)
        task = asyncio.ensure_future(w.server.steer_agent(aid, "steerhold: go"))
        await asyncio.wait_for(entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await h.pc.await_until(lambda: not h.pc.alive(g.pids()[0]), 20)
        assert w.node(aid).status == "pending"
        state["recover"] = True
        return aid
    aid = asyncio.run(go())
    assert_finished(w, aid, seen)


def test_lock_release_failure_is_retried_without_leaving_a_durable_hold(w, monkeypatch):
    from multiagents.runner import _Predecessor

    g = h.steer_world(w)
    state = {"recover": False}

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        runner = w.runner
        assert runner._claim(aid)
        seen = releases(runner, monkeypatch)
        real_release = runner._release

        def release(node_id):
            if not state["recover"]:
                raise OSError("lock release unavailable")
            real_release(node_id)
        monkeypatch.setattr(runner, "_release", release)
        await runner._steer_release(aid, "acme", "", None, None, "", False,
                                    _Predecessor())
        assert aid in runner._holds
        assert "lock" in runner._holds[aid].steer["steps"]
        assert w.node(aid).cleanup_hold
        state["recover"] = True
        return aid, seen
    aid, seen = asyncio.run(go())
    assert_finished(w, aid, seen)


def test_a_new_runner_adopts_and_finishes_pending_cleanup(w, monkeypatch):
    from multiagents.runner import Runner, _Predecessor

    g = h.steer_world(w)

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        runner = w.runner
        entry = runner.tree.enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "session_id": w.node(aid).session_id, "model": "acme/m1",
                     "message": "resume me", "task": "resume me"}, "test",
            deferred_by="test", dispatcher=runner._owner_fields())
        prior = runner._pc_reserve_resume(runner.config.agents["worker"],
                                         w.node(aid), "acme", entry["id"])
        assert runner._claim(aid)

        def unavailable(*args):
            raise OSError("storage unavailable")
        monkeypatch.setattr(runner, "_pc_unreserve", unavailable)
        monkeypatch.setattr(runner.tree, "restore_deferred", unavailable)
        await runner._steer_release(aid, "acme", "", prior, entry, entry["id"],
                                    False, _Predecessor())
        record = dict(w.node(aid).cleanup_hold)
        assert {"unreserve", "restore"} <= set(record["steer_cleanup"]["steps"])
        # Model the old process going away: its flock is gone, and the
        # durable record still contains the cleanup steps it could not finish.
        runner._release(aid)
        record["owner_pid"] = 999999999
        runner.tree.update(aid, cleanup_hold=record)
        return aid, entry
    aid, entry = asyncio.run(go())
    recovered = Runner(w.runner.paths, w.runner.config)
    seen = releases(recovered, monkeypatch)
    recovered._settle_holds()
    recovered._settle_holds()
    assert aid not in recovered._holds
    assert aid not in recovered._locks
    assert seen.count(aid) == 1
    assert not recovered.tree.get(aid).cleanup_hold
    assert recovered.tree.get(aid).slot_owner is None
    assert recovered.provider_slots()["acme"]["in_use"] == 0
    assert sum(d["id"] == entry["id"] for d in w.deferred()) == 1


def test_restore_recovery_does_not_require_a_live_foreign_predecessors_lock(w, monkeypatch):
    from multiagents.runner import Runner, _Predecessor

    g = h.steer_world(w)

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        runner = w.runner
        entry = runner.tree.enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "session_id": w.node(aid).session_id, "model": "acme/m1",
                     "message": "resume me", "task": "resume me"}, "test",
            deferred_by="test", dispatcher=runner._owner_fields())
        prior = runner._pc_reserve_resume(runner.config.agents["worker"],
                                         w.node(aid), "acme", entry["id"])
        foreign = h.predecessor_hold()
        runner.tree.update(aid, cleanup_hold=foreign)

        def unavailable(*args):
            raise OSError("restore unavailable")
        monkeypatch.setattr(runner.tree, "restore_deferred", unavailable)
        await runner._steer_release(aid, "acme", "", prior, entry, entry["id"],
                                    False, _Predecessor())
        record = dict(w.node(aid).cleanup_hold)
        record["steer_cleanup"]["owner_pid"] = 999999999
        runner.tree.update(aid, cleanup_hold=record)
        return aid, entry, foreign
    aid, entry, foreign = asyncio.run(go())
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    assert aid not in recovered._holds
    assert aid not in recovered._locks
    assert recovered.tree.get(aid).cleanup_hold == foreign
    assert sum(d["id"] == entry["id"] for d in w.deferred()) == 1


def test_predecessor_completion_preserves_another_steers_pending_mirror(w, monkeypatch):
    from multiagents.runner import Runner, _Predecessor

    g = h.steer_world(w)
    state = {"recover": False}

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        predecessor_owner = w.runner
        node = w.node(aid)
        assert predecessor_owner._claim(aid)
        hold = predecessor_owner._reserve_launch(
            aid, "acme", "", predecessor_owner.executor(
                predecessor_owner.config.agents["worker"]))
        hold.phase = "cleanup"
        hold.pid, hold.pid_start = node.pid, node.pid_start
        hold.record.update(pid=node.pid, pid_start=node.pid_start)
        predecessor_owner._persist_hold(aid, hold)
        steer_owner = Runner(predecessor_owner.paths, predecessor_owner.config)
        entry = steer_owner.tree.enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "session_id": node.session_id, "model": "acme/m1",
                     "message": "resume me", "task": "resume me"}, "test",
            deferred_by="test", dispatcher=steer_owner._owner_fields())
        steer_owner.tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
        real_restore = steer_owner.tree.restore_deferred

        def restore(record):
            if not state["recover"]:
                raise OSError("restore unavailable")
            return real_restore(record)
        monkeypatch.setattr(steer_owner.tree, "restore_deferred", restore)
        await steer_owner._steer_release(aid, "acme", "", None, entry,
                                        entry["id"], False, _Predecessor())
        predecessor_owner._end_hold(aid)
        assert aid not in predecessor_owner._holds
        assert aid not in predecessor_owner._locks
        record = steer_owner.tree.get(aid).cleanup_hold
        assert record and "restore" in record["steer_cleanup"]["steps"]
        assert record["owner"] == steer_owner._hold_owner
        state["recover"] = True
        return aid, entry, steer_owner
    aid, entry, runner = asyncio.run(go())
    runner._settle_holds()
    runner._settle_holds()
    assert aid not in runner._holds
    assert not runner.tree.get(aid).cleanup_hold
    assert runner.provider_slots()["acme"]["in_use"] == 0
    assert sum(d["id"] == entry["id"] for d in w.deferred()) == 1


def test_adoption_releases_its_new_lock_when_only_the_old_lift_was_pending(w, monkeypatch):
    from multiagents.runner import Runner, _Predecessor

    g = h.steer_world(w)

    async def go():
        aid = await h.running_with_session(w, g)
        g.open()
        await w.until(aid)
        runner = w.runner
        assert runner._claim(aid)

        def unavailable(*args, **kwargs):
            raise OSError("hold lift unavailable")
        monkeypatch.setattr(runner, "_lift_hold", unavailable)
        await runner._steer_release(aid, "acme", "", None, None, "", False,
                                    _Predecessor())
        assert aid not in runner._locks
        record = dict(w.node(aid).cleanup_hold)
        assert record["steer_cleanup"]["steps"] == ["lift"]
        record["owner_pid"] = 999999999
        runner.tree.update(aid, cleanup_hold=record)
        return aid
    aid = asyncio.run(go())
    recovered = Runner(w.runner.paths, w.runner.config)
    seen = releases(recovered, monkeypatch)
    recovered._settle_holds()
    recovered._settle_holds()
    assert aid not in recovered._holds
    assert aid not in recovered._locks
    assert not h.lock_held(recovered, aid)
    assert seen.count(aid) == 1
    assert not recovered.tree.get(aid).cleanup_hold
    assert recovered.provider_slots()["acme"]["in_use"] == 0
