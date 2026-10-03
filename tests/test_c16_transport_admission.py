"""TG-R1: invalid relaunches refuse before changing live or queued work."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

import test_sr_steer_exit_race as h

w = h.w


def mismatch(w, kind):
    spawn = w.p.providers["acme"]["spawn"]
    if kind == "resume":
        spawn["resume"] = [*spawn.get("resume", []), "{prompt}"]
    elif kind == "file":
        spawn["prompt_transport"] = "file"
        spawn["args"] = ["--flag"]
    else:
        spawn["args"] = [*spawn.get("args", []), "{prompt}"]
    w.reload()


@pytest.mark.parametrize("kind", ["args", "resume", "file"])
def test_tg_r1_refused_steer_keeps_live_predecessor_and_node(w, kind):
    h.up(w)

    async def go():
        aid = await h.running(w)
        run = w.runner.runs[aid]
        mismatch(w, kind)
        before = copy.deepcopy(w.runner.tree.read()["nodes"][aid])
        result = await w.server.steer_agent(aid, "change course")
        assert result.get("steered") is False, result
        assert "prompt_transport" in result.get("error", ""), result
        assert w.runner.runs[aid] is run
        assert not run.stop_requested
        assert not run.done.is_set()
        assert h.pc.alive(w.g.pids()[0])
        assert w.g.spawns() == 1
        assert w.runner.tree.read()["nodes"][aid] == before
    asyncio.run(go())


def test_tg_r1_queued_retry_is_recorded_as_refused_without_claiming_node(w):
    h.up(w)
    w.g.set(gate=None)

    async def go():
        aid = await w.started("worker", "original")
        await w.until(aid)
        run = w.runner.runs[aid]
        node = w.node(aid)
        entry = w.runner.tree.enqueue(
            "acme", {"op": "retry", "node_id": aid, "agent": "worker",
                     "session_id": node.session_id, "model": node.model,
                     "prompt_file": run.prompt_file},
            "provider_concurrency", deferred_by=None,
            dispatcher=w.runner._owner_fields())
        w.runner.tree.set_status(aid, "failed", f"queued retry {entry['id']}")
        mismatch(w, "args")
        before = copy.deepcopy(w.runner.tree.read()["nodes"][aid])
        assert await w.runner._drain_queues() == []
        queued = next(d for d in w.runner.tree.read()["deferred"] if d["id"] == entry["id"])
        assert queued["status"] == "refused", queued
        assert "prompt_transport" in queued["reason"]
        assert w.runner.tree.read()["nodes"][aid] == before
        assert w.g.spawns() == 1
    asyncio.run(go())


def test_tg_r1_queued_start_is_refused_and_the_queue_keeps_draining(w):
    h.up(w)
    w.g.set(gate=None)

    async def go():
        entry = w.runner.tree.enqueue(
            "acme", {"op": "start", "agent": "worker", "task": "queued task",
                     "model": "acme/m1"}, "provider_concurrency", deferred_by=None,
            dispatcher=w.runner._owner_fields())
        mismatch(w, "resume")
        assert await w.runner._drain_queues() == []
        queued = next(d for d in w.runner.tree.read()["deferred"] if d["id"] == entry["id"])
        assert queued["status"] == "refused", queued
        assert "prompt_transport" in queued["reason"]
        assert w.g.spawns() == 0
        assert not w.runner.tree.read()["nodes"]
    asyncio.run(go())


def test_tg_r1_consult_refusal_keeps_conversation_turn_unchanged(w):
    h.up(w, conversational=True)
    w.g.set(gate=None)

    async def go():
        first = await w.runner.consult("worker", "first", timeout=20)
        assert not first.get("error"), first
        aid = first["agent_id"]
        mismatch(w, "args")
        before = copy.deepcopy(w.runner.tree.read()["nodes"][aid])
        second = await w.runner.consult("worker", "second", timeout=20)
        assert "prompt_transport" in second.get("error", ""), second
        assert w.runner.tree.read()["nodes"][aid] == before
        assert w.g.spawns() == 1
    asyncio.run(go())


def test_tg_r1_answer_refusal_leaves_question_unanswered(w):
    h.up(w)

    async def go():
        aid = await h.running(w)
        question = w.runner.tree.add_question(aid, "choice", "Continue?", "yes")
        mismatch(w, "args")
        before = copy.deepcopy(w.runner.tree.get_question(question["id"]))
        result = await w.runner.answer_question(question["id"], "yes")
        assert "prompt_transport" in result.get("error", ""), result
        assert w.runner.tree.get_question(question["id"]) == before
        assert h.pc.alive(w.g.pids()[0])
        assert w.g.spawns() == 1
    asyncio.run(go())


def test_tg_r1_deferred_start_records_refusal_and_continues(w):
    h.up(w)

    async def go():
        entries = [w.runner.tree.defer(
            {"agent": "worker", "task": task, "provider": "acme"},
            0, "quota window") for task in ("one", "two")]
        mismatch(w, "args")
        result = await w.runner.resume_deferred()
        assert len(result.get("refused", [])) == 2, result
        assert result["restarted"] == []
        for entry in entries:
            queued = next(d for d in w.runner.tree.read()["deferred"]
                          if d["id"] == entry["id"])
            assert queued["status"] == "refused", queued
            assert "prompt_transport" in queued["reason"]
        assert w.g.spawns() == 0
        assert not w.runner.tree.read()["nodes"]
    asyncio.run(go())


def test_tg_r1_free_retry_refuses_changed_config_before_counting_attempt(w):
    h.up(w)
    w.g.set(silent=True, talk=False, exit=3)

    async def go():
        aid = await h.running(w)
        mismatch(w, "args")
        w.g.open()
        await w.until(aid, states={"failed"})
        assert "prompt_transport" in w.node(aid).reason
        assert w.node(aid).retries == 0
        assert w.g.spawns() == 1
    asyncio.run(go())



def test_tg_r1_commit_fix_refuses_before_counting_or_launching_turn(w):
    h.up(w)

    async def go():
        aid = await h.running(w)
        run = w.runner.runs[aid]
        mismatch(w, "args")
        failed = SimpleNamespace(ok=False, hook="check", err="hook refused", out="")
        result, attempts, _, _, verdict = await w.runner._commit_fix_loop(
            run, w.node(aid), failed, w.node(aid).session_id)
        assert result is failed
        assert attempts == 0
        assert verdict["status"] == "failed"
        assert "prompt_transport" in verdict["error"]
        assert not run.stop_requested
        assert h.pc.alive(w.g.pids()[0])
        assert w.g.spawns() == 1
    asyncio.run(go())


def test_tg_r1_commit_fix_refusal_reaches_result_and_node(w, monkeypatch):
    import json
    from multiagents import gitops

    h.up(w)
    monkeypatch.setattr(gitops, "commit_all", lambda *a, **kw: gitops.GitResult(
        False, "", "hook refused", 1, hook="check"))

    async def go():
        aid = await h.running(w)
        mismatch(w, "args")
        w.g.open()
        await w.until(aid, states={"failed"})
        assert "prompt_transport" in w.node(aid).reason
        result = json.loads((h.run_dir(w, aid) / "result.json").read_text())
        assert result["status"] == "failed"
        assert "prompt_transport" in result["reason"]
        assert "commit-fix resume refused" in result["text"]
        assert w.g.spawns() == 1
    asyncio.run(go())


def inject_launch_reload(w, monkeypatch):
    original = w.runner._launch

    async def reload_before_launch(**kwargs):
        mismatch(w, "args")
        return await original(**kwargs)
    monkeypatch.setattr(w.runner, "_launch", reload_before_launch)


@pytest.mark.parametrize("path", ["start", "steer", "consult", "queued", "fix", "retry"])
def test_tg_r1_reload_at_final_launch_gate_is_reported(w, monkeypatch, path):
    from multiagents import gitops
    import json

    h.up(w, conversational=path == "consult")
    if path in {"start", "consult", "queued"}:
        w.g.set(gate=None)
    if path == "retry":
        w.g.set(silent=True, talk=False, exit=3)

    async def go():
        if path == "start":
            inject_launch_reload(w, monkeypatch)
            result = await w.start("worker", "first")
            assert "prompt_transport" in result.get("error", ""), result
        elif path == "consult":
            first = await w.runner.consult("worker", "first", timeout=20)
            assert not first.get("error"), first
            inject_launch_reload(w, monkeypatch)
            result = await w.runner.consult("worker", "second", timeout=20)
            assert "prompt_transport" in result.get("error", ""), result
        elif path == "queued":
            aid = await w.started("worker", "first")
            await w.until(aid)
            run, node = w.runner.runs[aid], w.node(aid)
            entry = w.runner.tree.enqueue(
                "acme", {"op": "retry", "node_id": aid, "agent": "worker",
                         "session_id": node.session_id, "model": node.model,
                         "prompt_file": run.prompt_file}, "provider_concurrency",
                deferred_by=None, dispatcher=w.runner._owner_fields())
            w.runner.tree.set_status(aid, "failed", f"queued {entry['id']}")
            inject_launch_reload(w, monkeypatch)
            assert await w.runner._drain_queues() == []
            record = next(d for d in w.runner.tree.read()["deferred"]
                          if d["id"] == entry["id"])
            assert record["status"] == "refused"
            assert "prompt_transport" in record["reason"]
            assert "prompt_transport" in w.node(aid).reason
        else:
            aid = await h.running(w)
            run = w.runner.runs[aid]
            inject_launch_reload(w, monkeypatch)
            if path == "steer":
                result = await w.server.steer_agent(aid, "next")
                assert not result.get("steered")
                assert "prompt_transport" in result.get("error", ""), result
                assert w.node(aid).status == "failed"
            elif path == "fix":
                failed = gitops.GitResult(False, "", "hook refused", 1, hook="check")
                _, _, _, _, verdict = await w.runner._commit_fix_loop(
                    run, w.node(aid), failed, w.node(aid).session_id)
                assert verdict["status"] == "failed"
                assert "prompt_transport" in verdict["error"]
            else:
                w.g.open()
                await w.until(aid, states={"failed"})
                assert "prompt_transport" in w.node(aid).reason
                assert run.done.is_set()
                assert not (h.run_dir(w, aid) / "postmortem-crash.txt").exists()
        assert w.g.spawns() <= 1
    asyncio.run(go())


def test_tg_r2_doctor_reports_invalid_transport_and_continues(tmp_path, monkeypatch, capsys):
    from test_c16_transport_args_guard import Layered, _custom

    lay = Layered(tmp_path, monkeypatch,
                  global_providers={"bad": _custom("foo", ["--flag"]),
                                    "good": _custom("stdin", ["--flag"])},
                  agents={"bad": "bad", "good": "good"})
    rc, output = lay.doctor(capsys)
    assert rc == 1
    assert "bad" in output and "invalid spawn.prompt_transport 'foo'" in output
    assert "good" in output and "problem(s)" in output
    assert lay.calls("bad") == []
    assert lay.calls("good") == []


def test_tg_r1_retry_refusal_finishes_probe_and_merges_children(w, monkeypatch):
    h.up(w)
    w.g.set(silent=True, talk=False, exit=3)
    finished, children = [], []

    async def go():
        aid = await h.running(w)
        run = w.runner.runs[aid]
        run.startup_token = "probe-token"
        original = w.runner._startup_finish

        def finish(provider, node_id, token, **kwargs):
            finished.append((node_id, token, kwargs))
            return original(provider, node_id, token, **kwargs)

        async def merge(node_id):
            children.append(node_id)
        monkeypatch.setattr(w.runner, "_startup_finish", finish)
        monkeypatch.setattr(w.runner, "_merge_pending_children", merge)
        mismatch(w, "args")
        w.g.open()
        await w.until(aid, states={"failed"})
        assert run.done.is_set()
        assert any(node_id == aid and token == "probe-token" and kw.get("resolved")
                   for node_id, token, kw in finished)
        assert children == [aid]
        assert "free retry refused" in w.node(aid).reason
        assert "prompt_transport" in w.node(aid).reason
    asyncio.run(go())
