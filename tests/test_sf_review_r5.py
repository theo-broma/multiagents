"""SF-R3: round-8 review regressions against 7d4f816."""
from __future__ import annotations

import asyncio

import pytest

import test_sf_review_r3 as h
from multiagents import runner as runner_mod
from multiagents.runner import Runner, _Predecessor

w = h.w


async def finished(w, g):
    aid = await h.running_with_session(w, g)
    g.open()
    await w.until(aid)
    return aid


def test_a_second_inflight_steer_cannot_replace_pending_cleanup(w, monkeypatch):
    g = h.steer_world(w)
    state = {"recover": False}
    real_unreserve = w.runner._pc_unreserve

    def unreserve(aid, prior):
        if prior == "running" and not state["recover"]:
            raise OSError("unreserve unavailable")
        return real_unreserve(aid, prior)
    monkeypatch.setattr(w.runner, "_pc_unreserve", unreserve)

    async def go():
        aid = await h.running_with_session(w, g)
        entered = asyncio.Event()
        second_entered = asyncio.Event()
        real_stop = w.runner.stop
        calls = 0

        async def stop(agent_id, *, internal=False):
            nonlocal calls
            result = await real_stop(agent_id, internal=internal)
            if internal:
                calls += 1
                (entered if calls == 1 else second_entered).set()
                await asyncio.sleep(60)
            return result
        monkeypatch.setattr(w.runner, "stop", stop)
        first = asyncio.ensure_future(w.runner.steer(aid, "first steer"))
        await asyncio.wait_for(entered.wait(), 30)
        second = asyncio.ensure_future(w.runner.steer(aid, "second steer"))
        assert await h.pc.await_until(lambda: second.done() or second_entered.is_set())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        if not second.done():
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            result = None
        else:
            result = await second
        hold = w.runner._holds[aid]
        assert hold.steer and "unreserve" in hold.steer["steps"]
        assert hold.steer["prior"] == "running"
        assert result and result.get("steered") is False
        assert "previous steer" in result["error"]
        pending = hold.steer
        result = await w.runner.steer(aid, "try after the first steer returned")
        assert result.get("steered") is False
        assert "previous steer" in result["error"]
        assert hold.steer is pending
        assert "unreserve" in pending["steps"]
        state["recover"] = True
        return aid
    aid = asyncio.run(go())
    w.runner._settle_holds()
    w.runner._settle_holds()
    assert aid not in w.runner._holds
    assert aid not in w.runner._locks
    assert not w.node(aid).cleanup_hold
    assert w.node(aid).slot_owner is None
    assert w.runner.provider_slots()["acme"]["in_use"] == 0


@pytest.mark.parametrize("recoverable", [True, False])
def test_foreign_cleanup_adoption_recovers_identity_or_records_a_terminal_hold(w, monkeypatch,
                                                                             recoverable):
    g = h.steer_world(w)
    aid = asyncio.run(finished(w, g))
    node = w.node(aid)
    held = h.predecessor_hold()
    identity = {"kind": "local" if recoverable else "unknown", "container": ""}
    pid = node.pid if recoverable else None
    start = node.pid_start if recoverable else ""
    held.update(pid=pid, pid_start=start, executor=identity)
    held["steer_cleanup"] = {
        "steps": ["confirm", "status", "lock", "lift"], "provider": "acme",
        "token": "", "prior": None, "queued": None, "captured": True,
        "absent": False, "failure": "", "foreign": True, "launch_owned": False,
        "owner": "dead-steer", "owner_pid": 999999999, "owner_start": "",
        "predecessor": {"pid": pid, "pid_start": start, "executor": identity},
    }
    if not recoverable:
        w.tree().update(aid, pid=None, pid_start="", exec_identity={})
        wrapper = w.runner.paths.run_dir(aid) / "wrapper.pid"
        wrapper.unlink(missing_ok=True)
    w.tree().update(aid, cleanup_hold=held)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    if recoverable:
        assert aid not in recovered._holds
        assert "steer_cleanup" not in recovered.tree.get(aid).cleanup_hold
        assert aid not in recovered._locks
    else:
        cleanup = recovered._holds[aid].steer
        assert cleanup.get("blocked_reason"), "identity loss needs a recorded terminal decision"
        assert "unknown" in cleanup["blocked_reason"]
        assert recovered.tree.get(aid).cleanup_hold["steer_cleanup"]["blocked_reason"]

        def no_retry(*args, **kwargs):
            pytest.fail("an unrecoverable identity was retried indefinitely")
        monkeypatch.setattr(recovered, "_steer_predecessor", no_retry)
        monkeypatch.setattr(runner_mod, "_positively_ended", no_retry)
        for _ in range(3):
            recovered._settle_holds()
        result = asyncio.run(recovered.steer(aid, "try again"))
        assert result.get("steered") is False
        assert "unknown" in result["error"]
        assert recovered._held(aid)


@pytest.mark.parametrize("restart", [False, True])
def test_restore_that_commits_then_raises_is_not_repeated_after_admission(w, monkeypatch, restart):
    g = h.steer_world(w)
    state = {"writes": 0}

    async def go():
        aid = await finished(w, g)
        runner = w.runner
        entry = runner.tree.enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "session_id": w.node(aid).session_id, "model": "acme/m1",
                     "message": "resume", "task": "resume"}, "test",
            deferred_by="test", dispatcher=runner._owner_fields())
        prior = runner._pc_reserve_resume(runner.config.agents["worker"],
                                         w.node(aid), "acme", entry["id"])
        runner.tree.update(aid, cleanup_hold=h.predecessor_hold())
        real_restore = runner.tree.restore_deferred

        def restore(record):
            wrote = real_restore(record)
            state["writes"] += int(wrote)
            if wrote and state["writes"] == 1:
                raise OSError("the restore committed but acknowledgement failed")
            return wrote
        monkeypatch.setattr(runner.tree, "restore_deferred", restore)
        await runner._steer_release(aid, "acme", "", prior, entry, entry["id"],
                                    False, _Predecessor())
        assert any(d["id"] == entry["id"] for d in w.deferred())
        assert runner.tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
        if restart:
            record = runner.tree.get(aid).cleanup_hold
            record["steer_cleanup"]["owner_pid"] = 999999999
            runner.tree.update(aid, cleanup_hold=record)
        return aid, entry
    aid, entry = asyncio.run(go())
    runner = Runner(w.runner.paths, w.runner.config) if restart else w.runner
    runner._settle_holds()
    runner._settle_holds()
    assert not any(d["id"] == entry["id"] for d in w.deferred()), "the admitted entry was resurrected"
    assert aid not in runner._holds
    assert "steer_cleanup" not in runner.tree.get(aid).cleanup_hold
    assert state["writes"] == 1


def test_a_later_failed_admission_can_restore_the_same_entry_with_a_new_receipt(w):
    g = h.steer_world(w)
    aid = asyncio.run(finished(w, g))
    tree = w.tree()
    entry = tree.enqueue(
        "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                 "model": "acme/m1", "message": "resume", "task": "resume"},
        "test", deferred_by="test", dispatcher=w.runner._owner_fields())
    assert tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
    first = dict(entry, restore_token="first-admission")
    assert tree.restore_deferred(first)
    assert tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
    assert not tree.restore_deferred(first)
    second = dict(entry, restore_token="second-admission")
    assert tree.restore_deferred(second)
    assert len([d for d in w.deferred() if d["id"] == entry["id"]]) == 1
    assert tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
    assert not tree.restore_deferred(first)
    assert not tree.restore_deferred(second)
    assert not w.deferred()
