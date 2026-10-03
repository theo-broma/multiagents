"""SF-R3: round-6 review regressions against 58492da."""
from __future__ import annotations

import asyncio
import os

import pytest

import test_sf_review_r3 as h
import test_sf_review_r5 as previous
from multiagents import runner as runner_mod
from multiagents.runner import Runner

w = h.w


def unknown_hold(w, aid, *, pid=None, start="", kind="unknown"):
    held = h.predecessor_hold("dead-predecessor")
    held.update(owner_pid=999999999, executor={"kind": kind, "container": ""})
    held["steer_cleanup"] = {
        "steps": ["confirm", "status", "lock", "lift"], "provider": "acme",
        "token": "", "prior": None, "queued": None, "captured": True,
        "absent": False, "failure": "", "foreign": False, "launch_owned": False,
        "owner": "dead-steer", "owner_pid": 999999999, "owner_start": "",
        "predecessor": {"pid": pid, "pid_start": start,
                        "executor": {"kind": kind, "container": ""}},
    }
    w.tree().update(aid, pid=None, pid_start="", exec_identity={}, cleanup_hold=held)
    (w.runner.paths.run_dir(aid) / "wrapper.pid").unlink(missing_ok=True)
    return held


@pytest.mark.parametrize("restart", [False, True])
def test_stop_agent_explicitly_releases_a_terminal_steer_hold(w, monkeypatch, restart):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    unknown_hold(w, aid)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    reason = recovered._holds[aid].steer["blocked_reason"]
    assert f"stop_agent {aid}" in reason
    for _ in range(3):
        recovered._settle_holds()
    assert recovered._held(aid)
    assert aid in recovered._locks
    if restart:
        recovered._release(aid)
        record = recovered.tree.get(aid).cleanup_hold
        record["owner_pid"] = record["steer_cleanup"]["owner_pid"] = 999999999
        recovered.tree.update(aid, cleanup_hold=record)
        recovered = Runner(w.runner.paths, w.runner.config)
    releases = []
    real_release = recovered._release

    def release(agent_id):
        if agent_id in recovered._locks:
            releases.append(agent_id)
        return real_release(agent_id)
    monkeypatch.setattr(recovered, "_release", release)

    def no_unidentified_kill(node):
        pytest.fail("the explicit release must not signal an unidentified predecessor")
    monkeypatch.setattr(recovered, "stop_detached", no_unidentified_kill)
    result = asyncio.run(recovered.stop(aid))
    assert result.get("released_steer_hold") is True
    recovered._settle_holds()
    recovered._settle_holds()
    assert aid not in recovered._holds
    assert aid not in recovered._locks
    assert not w.node(aid).cleanup_hold
    assert w.node(aid).status == "cancelled"
    assert recovered.provider_slots()["acme"]["in_use"] == 0
    assert releases == [aid]
    events = w.p.events_of("steer_hold_released")
    assert len(events) == 1
    assert events[0]["agent"] == aid
    assert events[0]["reason"] == reason


@pytest.mark.parametrize("source", ["wrapper", "recorded"])
def test_adoption_never_confirms_a_bare_pid_without_its_start_time(w, monkeypatch, source):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    unknown_hold(w, aid, pid=os.getpid() if source == "recorded" else None, kind="local")
    if source == "wrapper":
        (w.runner.paths.run_dir(aid) / "wrapper.pid").write_text(f"{os.getpid()}\n")
    real_ended = runner_mod._positively_ended

    def ended(pid, start, probe):
        assert not pid or start, "a recovered bare PID was polled as a historical process"
        return real_ended(pid, start, probe)
    monkeypatch.setattr(runner_mod, "_positively_ended", ended)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    cleanup = recovered._holds[aid].steer
    assert "unknown" in cleanup.get("blocked_reason", "")
    assert f"stop_agent {aid}" in cleanup["blocked_reason"]
    for _ in range(3):
        recovered._settle_holds()
    assert recovered._held(aid)
    assert aid in recovered._locks
    assert recovered.tree.get(aid).cleanup_hold["steer_cleanup"]["blocked_reason"]
    asyncio.run(recovered.stop(aid))
    assert not recovered._held(aid)


def test_the_first_steer_capture_persists_the_historical_start_time(w, monkeypatch):
    g = h.steer_world(w)
    real_unreserve = w.runner._pc_unreserve
    state = {"recover": False}

    def unreserve(aid, prior):
        if not state["recover"]:
            raise OSError("keep the capture durable for recovery")
        return real_unreserve(aid, prior)
    monkeypatch.setattr(w.runner, "_pc_unreserve", unreserve)

    async def go():
        aid = await h.running_with_session(w, g)
        predecessor = w.runner._steer_predecessor(aid)
        assert predecessor.pid_start
        g.open()
        await w.until(aid)
        await w.runner._steer_release(aid, "acme", "", "running", None, "", False,
                                      predecessor)
        return aid, predecessor
    aid, predecessor = asyncio.run(go())
    saved = w.node(aid).cleanup_hold["steer_cleanup"]["predecessor"]
    assert saved["pid"] == predecessor.pid
    assert saved["pid_start"] == predecessor.pid_start
    state["recover"] = True
    w.runner._settle_holds()
    assert not w.node(aid).cleanup_hold


@pytest.mark.parametrize("outcome", ["admitted", "cancelled", "removed"])
def test_restore_receipts_are_pruned_when_an_entry_leaves_the_queue(w, outcome):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    tree = w.tree()
    entry = tree.enqueue(
        "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                 "model": "acme/m1", "message": "resume", "task": "resume"},
        "test", deferred_by="test", dispatcher=w.runner._owner_fields())
    assert tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
    restore = dict(entry, restore_token="failed-admission")
    assert tree.restore_deferred(restore)
    assert tree.read()["deferred_restores"][entry["id"]] == [restore["restore_token"]]
    if outcome == "admitted":
        prior = w.runner._pc_reserve_resume(w.runner.config.agents["worker"],
                                            w.node(aid), "acme", entry["id"])
        w.runner._pc_unreserve(aid, prior)
    elif outcome == "cancelled":
        assert tree.exit_deferred(entry["id"], "cancelled")
    else:
        assert tree.drop_deferred(entry["id"])
    assert entry["id"] not in tree.read().get("deferred_restores", {})
    assert not tree.restore_deferred(restore)
    assert not w.deferred()


@pytest.mark.parametrize("restart", [False, True])
def test_pruning_preserves_an_unacknowledged_restore_in_its_single_owner(w, monkeypatch, restart):
    g = h.steer_world(w)
    state = {"raised": False}

    async def go():
        aid = await previous.finished(w, g)
        runner = w.runner
        entry = runner.tree.enqueue(
            "acme", {"op": "resume", "node_id": aid, "agent": "worker",
                     "model": "acme/m1", "message": "resume", "task": "resume"},
            "test", deferred_by="test", dispatcher=runner._owner_fields())
        prior = runner._pc_reserve_resume(runner.config.agents["worker"], w.node(aid),
                                          "acme", entry["id"])
        real_write = runner.tree._write_unlocked

        def write(data):
            real_write(data)
            if not state["raised"] and any(
                    d.get("restore_token") for d in data["deferred"] if isinstance(d, dict)):
                state["raised"] = True
                raise OSError("restore committed before the writer raised")
        monkeypatch.setattr(runner.tree, "_write_unlocked", write)
        await runner._steer_release(aid, "acme", "", prior, entry, entry["id"],
                                    False, runner_mod._Predecessor())
        assert state["raised"]
        hold = runner._holds[aid]
        assert "restore" in hold.steer["steps"]
        token = hold.steer["queued"]["restore_token"]
        assert not hold.steer["queued"].get("restore_done")
        assert runner.tree.exit_deferred(entry["id"], "restarted", agent_id=aid)
        data = runner.tree.read()
        assert entry["id"] not in data.get("deferred_restores", {})
        assert data["nodes"][aid]["cleanup_hold"]["steer_cleanup"]["queued"]["restore_done"] == token
        # A stale memory mirror must not undo the acknowledgement transfer.
        runner._persist_hold(aid, hold)
        assert runner.tree.get(aid).cleanup_hold["steer_cleanup"]["queued"]["restore_done"] == token
        if restart:
            runner._release(aid)
            record = runner.tree.get(aid).cleanup_hold
            record["owner_pid"] = record["steer_cleanup"]["owner_pid"] = 999999999
            runner.tree.update(aid, cleanup_hold=record)
        return aid, entry
    aid, entry = asyncio.run(go())
    runner = Runner(w.runner.paths, w.runner.config) if restart else w.runner
    runner._settle_holds()
    runner._settle_holds()
    assert not w.deferred()
    assert not runner.tree.read().get("deferred_restores")
    assert aid not in runner._holds
    assert aid not in runner._locks
    assert not w.node(aid).cleanup_hold


def test_operator_release_keeps_retrying_an_unfinished_lock_release(w, monkeypatch):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    unknown_hold(w, aid)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    state = {"recover": False, "releases": 0}
    real_release = recovered._release

    def release(agent_id):
        if not state["recover"]:
            raise OSError("lock release unavailable")
        state["releases"] += int(agent_id in recovered._locks)
        return real_release(agent_id)
    monkeypatch.setattr(recovered, "_release", release)
    result = asyncio.run(recovered.stop(aid))
    assert result["released_steer_hold"]
    assert result["cleanup_pending"]
    cleanup = w.node(aid).cleanup_hold["steer_cleanup"]
    assert cleanup["operator_release"]
    assert not cleanup.get("blocked_reason")
    assert "lock" in cleanup["steps"]
    assert aid in recovered._locks
    state["recover"] = True
    recovered._settle_holds()
    recovered._settle_holds()
    assert aid not in recovered._holds
    assert aid not in recovered._locks
    assert not w.node(aid).cleanup_hold
    assert state["releases"] == 1
    assert len(w.p.events_of("steer_hold_released")) == 1


def test_operator_release_survives_finishing_the_steer_half_before_a_restart(w, monkeypatch):
    g = h.steer_world(w)
    aid = asyncio.run(previous.finished(w, g))
    held = unknown_hold(w, aid)
    held["steer_cleanup"]["foreign"] = True
    w.tree().update(aid, cleanup_hold=held)
    recovered = Runner(w.runner.paths, w.runner.config)
    recovered._settle_holds()
    assert recovered._holds[aid].steer["launch_owned"]
    real_settle = recovered._settle_holds
    calls = 0

    def settle_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            real_settle()
    monkeypatch.setattr(recovered, "_settle_holds", settle_once)
    assert asyncio.run(recovered.stop(aid))["released_steer_hold"]
    assert recovered._holds[aid].steer is None
    record = recovered.tree.get(aid).cleanup_hold
    assert "steer_cleanup" not in record
    assert record["operator_release"]
    recovered._release(aid)
    record["owner_pid"] = 999999999
    recovered.tree.update(aid, cleanup_hold=record)
    restarted = Runner(w.runner.paths, w.runner.config)
    restarted._settle_holds()
    assert aid not in restarted._holds
    assert aid not in restarted._locks
    assert not restarted.tree.get(aid).cleanup_hold
    assert restarted.provider_slots()["acme"]["in_use"] == 0


def test_an_automatic_consult_timeout_does_not_release_unknown_liveness(w, monkeypatch):
    from types import SimpleNamespace

    h.pc.gated(w, "acme", max_concurrent=1)
    w.agent("advisor", "acme", "acme/m1", conversational=True)
    w.up()
    state = {}

    class Deadline:
        async def wait(self):
            raise TimeoutError("the consult deadline passed")

    async def launch(*, node_id, **kwargs):
        state["aid"] = node_id
        unknown_hold(w, node_id)
        w.runner._settle_holds()
        assert w.runner._holds[node_id].steer["blocked_reason"]
        return SimpleNamespace(done=Deadline())
    monkeypatch.setattr(w.runner, "_launch", launch)
    monkeypatch.setattr(w.runner, "stop_detached", lambda node: False)
    result = asyncio.run(w.runner.consult("advisor", "question", timeout=1))
    aid = state["aid"]
    assert result.get("timed_out")
    assert w.runner._held(aid)
    assert aid in w.runner._locks
    assert w.node(aid).cleanup_hold["steer_cleanup"]["blocked_reason"]
    assert not w.p.events_of("steer_hold_released")
    asyncio.run(w.runner.stop(aid))
    assert not w.runner._held(aid)
