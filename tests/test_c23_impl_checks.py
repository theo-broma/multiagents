"""C23 acceptance checks beyond the original handover fixture contract."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_c23_quota_handover import World, TASK, MARKER, wait_until
from multiagents import budget, config, quota_handover as qh
from multiagents.runner import Runner
from multiagents.providers import load_providers


@pytest.mark.parametrize("target", ["beta", "other"])
def test_qh_r8_limits_tag_and_readonly_paths_survive_switch(tmp_path, monkeypatch, target):
    w = World(tmp_path, monkeypatch, models={target: "model-a" if target == "beta" else "model-o"},
              agent_extra={"timeout": 41, "silence_timeout": 19, "max_steps": 37,
                           "max_children": 3, "readonly_paths": ["protected/**"]})
    gate = tmp_path / "release-source"
    w.set_plan("alpha", gate=str(gate))
    async def scenario():
        result = await w.r.start("worker", TASK, timeout=43, budget_tag="handover-work", budget_tokens=1000)
        aid = result["agent_id"]
        await wait_until(lambda: bool(w.calls()))
        original_limits = w.r.launch_limits.lookup(aid)
        gate.touch()
        await w.settle(aid)
        assert [c["instance"] for c in w.calls()] == ["alpha", target]
        node = w.r.tree.get(aid)
        assert node.budget_tag == "handover-work"
        assert w.r.launch_limits.lookup(aid) == original_limits
        assert w.r.runs[aid].spec.readonly_paths == ["protected/**"]
        assert w.r.launch_limits.spec(aid)["readonly_paths"] == ["protected/**"]
        assert w.r.tree.usage_for_tag("handover-work")["total"] == 24
        assert node.usage["cost_usd"] == pytest.approx(.2)
        assert [s["usage"]["cost_usd"] for s in node.segments] == [.1, .1]
    asyncio.run(scenario())


def test_qh_r8_mid_copy_fault_keeps_target_and_tries_continuation(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode="copy", models={"beta": "model-a", "other": "model-o"})
    prefix = json.dumps({"history": "x" * 150000}) + "\n"
    source = w.stores["alpha"] / (w.session_id + ".jsonl")
    target = w.stores["beta"] / (w.session_id + ".jsonl")
    source.write_text(prefix)
    target.write_text(prefix)
    calls = []
    def fail_copy(output, chunk):
        calls.append(len(chunk))
        output.write(chunk)
        raise OSError("injected mid-copy failure")
    monkeypatch.setattr(qh, "copy_chunk", fail_copy)
    asyncio.run(w.start())
    assert calls and calls[0] < len(source.read_bytes())
    assert target.read_text() == prefix
    assert source.read_text().startswith(prefix)
    assert [c["instance"] for c in w.calls()] == ["alpha", "other"]
    assert w.events("handover_failed")
    assert not list(target.parent.glob(".handover-*"))


def test_qh_r20_embedded_costs_are_charged_to_the_serving_instance(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    for provider in w.providers.values():
        provider["billing"] = "metered"
    w.reload()
    asyncio.run(w.start())
    charges = [json.loads(line) for line in w.r.ledger.path.read_text().splitlines()
               if json.loads(line).get("kind") == "charge"]
    assert [(c["provider"], c["usd"]) for c in charges] == [("alpha", .1), ("beta", .1)]


def test_qh_r20_manual_switch_preserves_earlier_segment_usage(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a", "other": "model-o"})
    async def scenario():
        result = await w.start()
        aid = result["agent_id"]
        assert (await w.steer(aid, provider="other"))["steered"]
        node = w.r.tree.get(aid)
        assert [s["provider"] for s in node.segments] == ["alpha", "beta", "other"]
        assert [s["usage"]["cost_usd"] for s in node.segments] == [.1, .1, .1]
        assert node.usage["cost_usd"] == pytest.approx(.3)
    asyncio.run(scenario())


def test_qh_r8_rejected_last_target_preserves_source_session(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    w.set_plan("beta", reject=True)
    result = asyncio.run(w.start())
    node = w.r.tree.get(result["agent_id"])
    assert [c["instance"] for c in w.calls()] == ["alpha", "beta"]
    assert node.provider == "alpha"
    assert node.session_id == w.session_id
    assert node.status == "limited"
    assert len(node.segments) == 1
    assert w.r.runs[node.id].provider.name == "alpha"
    assert w.r.launch_limits.spec(node.id)["provider"] == "alpha"
    assert w.events("handover_failed")


def test_qh_r10_new_source_turn_can_retry_a_previously_rejected_target(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    w.set_plan("beta", reject=True)
    async def scenario():
        result = await w.start()
        w.reset("alpha")
        # C22's legacy chooser also reads this persisted cooldown using the
        # wall clock; reset it alongside the fixture's injected quota clock.
        w.r.tree.set_cooldown("alpha", 0, "fixture quota reset")
        w.set_plan("alpha", quota=True)
        w.set_plan("beta", reject=False)
        # The source is usable at resume admission, then exhausts during its
        # next CLI turn, matching the first start's read_all/finalization seam.
        def reading(name, *args, **kwargs):
            if name == "alpha" and len(w.calls()) < 3:
                return w.readings[name]
            return (budget.Budget(name, known=True, headroom=0)
                    if w.plan(name).get("quota") else w.readings[name])
        monkeypatch.setattr(budget, "read_provider", reading)
        await w.steer(result["agent_id"])
        assert [c["instance"] for c in w.calls()] == ["alpha", "beta", "alpha", "beta"]
        assert w.r.tree.get(result["agent_id"]).status == "done"
    asyncio.run(scenario())


def test_qh_r10_stop_at_transfer_gate_cannot_be_restarted(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode="copy", models={"beta": "model-a"})
    async def scenario():
        reached, release = asyncio.Event(), asyncio.Event()
        async def gate(attempt):
            reached.set()
            await release.wait()
        monkeypatch.setattr(qh, "before_transfer", gate)
        result = await w.r.start("worker", TASK)
        aid = result["agent_id"]
        await asyncio.wait_for(reached.wait(), 8)
        assert "in progress" in (await w.r.steer(aid, "competing steer"))["error"]
        assert "in progress" in w.r.merge_agent(aid)["error"]
        await w.r.stop(aid)
        release.set()
        assert w.r.tree.get(aid).handover_attempt["state"] == "failed"
        await w.r.resume_deferred()
        assert w.r.tree.get(aid).status == "cancelled"
        assert [c["instance"] for c in w.calls()] == ["alpha"]
    asyncio.run(scenario())


def test_qh_r7_verified_live_target_accepts_plain_steer(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, models={"beta": "model-a"})
    gate = tmp_path / "hold-target-tool"
    w.set_plan("beta", tool_gate=str(gate))
    async def scenario():
        result = await w.r.start("worker", TASK)
        aid = result["agent_id"]
        await wait_until(lambda: (w.r.tree.get(aid).handover_attempt or {}).get("state") == "completed")
        assert not gate.exists()
        w.set_plan("beta", tool_gate="")
        answer = await w.r.steer(aid, "Continue the verified conversation")
        assert answer.get("steered"), answer
        await w.settle(aid)
        assert [c["instance"] for c in w.calls()] == ["alpha", "beta", "beta"]
        assert w.r.tree.get(aid).status == "done"
    asyncio.run(scenario())


def test_qh_r13_manual_steer_keeps_reserve_protected(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, reserved="reserve")
    w.set_plan("alpha", quota=False)
    async def scenario():
        result = await w.start()
        refusal = await w.r.steer(result["agent_id"], "Use reserve", provider="reserve")
        assert "protected" in refusal["error"] and "usable" in refusal["error"]
        assert [c["instance"] for c in w.calls()] == ["alpha"]
    asyncio.run(scenario())


@pytest.mark.parametrize("source,target,expected", [
    (b"first\nsecond\n", b"first\n", b"first\nsecond\n"),
    (b"first\n", b"first\nsecond\n", b"first\nsecond\n"),
    (b"first\n", b"first\n", b"first\n"),
])
def test_qh_r30_copy_accepts_both_prefix_directions(tmp_path, source, target, expected):
    a, b = tmp_path / "source", tmp_path / "target"
    a.write_bytes(source)
    b.write_bytes(target)
    qh.transfer_session(a, b)
    assert a.read_bytes() == source
    assert b.read_bytes() == expected


_RESTART_DRIVER = '''import asyncio, json, sys
from pathlib import Path
from pytest import MonkeyPatch
from multiagents import budget, quota_handover as qh
from multiagents.config import load
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
async def gate(attempt):
    Path(sys.argv[4]).write_text(json.dumps(attempt))
    await asyncio.Event().wait()
async def main():
    run=Runner(ProjectPaths(Path(sys.argv[1])),load(ProjectPaths(Path(sys.argv[1])),seed=False))
    aidfile=Path(sys.argv[2])
    if sys.argv[3]=='start':
        result=await run.start('worker', 'Implement the assigned quota handover task')
        aidfile.write_text(result['agent_id'])
    else:
        await run.resume_deferred()
    aid=aidfile.read_text()
    end=asyncio.get_running_loop().time()+10
    while asyncio.get_running_loop().time()<end:
        if run.check(aid)['status'] not in ('running','pending'):
            await asyncio.sleep(.1)
            if run.check(aid)['status'] not in ('running','pending'): return
        await asyncio.sleep(.01)
    raise RuntimeError('run did not settle')
with MonkeyPatch.context() as mp:
    mp.setattr(budget,'read_all',lambda *a,**kw: {n:budget.Budget(n,known=True,headroom=.8)
        for n in ('alpha','beta','reserve','other')})
    mp.setattr(budget,'read_provider',lambda n,*a,**kw: budget.Budget(n,known=True,headroom=0 if n=='alpha' else .8))
    if sys.argv[3]=='start': mp.setattr(qh,'before_transfer',gate)
    asyncio.run(main())
'''


def test_qh_r10_crash_before_transfer_recovers_same_attempt(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, mode="copy", models={"beta": "model-a"})
    driver, aid_file, reached = (tmp_path / p for p in ("driver.py", "agent-id", "transfer-reached"))
    driver.write_text(_RESTART_DRIVER)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    argv = [sys.executable, str(driver), str(w.paths.root), str(aid_file)]
    processes = []
    try:
        with (tmp_path / "first.log").open("w") as log:
            first = subprocess.Popen([*argv, "start", str(reached)], env=env, stdout=log, stderr=log)
            processes.append(first)
            end = time.monotonic() + 8
            while not reached.exists() and first.poll() is None and time.monotonic() < end:
                time.sleep(.01)
            assert reached.exists(), (tmp_path / "first.log").read_text()
            attempt = json.loads(reached.read_text())
            assert attempt["state"] == "prepared"
            assert [c["instance"] for c in w.calls()] == ["alpha"]
            first.kill()
            first.wait(timeout=5)
        with (tmp_path / "second.log").open("w") as log:
            second = subprocess.Popen([*argv, "restart", str(reached)], env=env, stdout=log, stderr=log)
            processes.append(second)
            second.wait(timeout=15)
            assert second.returncode == 0, (tmp_path / "second.log").read_text()
        assert [c["instance"] for c in w.calls()] == ["alpha", "beta"]
        node = Runner(w.paths, config.load(w.paths, seed=False)).tree.get(aid_file.read_text())
        assert node.status == "done"
        assert node.provider == "beta"
        assert node.handover_attempt["attempt"] == attempt["attempt"]
        assert node.handover_attempt["state"] == "completed"
        assert len(w.events("handover_started")) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_qh_r21_scheduler_excluded_at_all_handover_entry_points(tmp_path, monkeypatch):
    from nc_fixture.world import World as SchedulerWorld
    w = SchedulerWorld(tmp_path, monkeypatch)
    sibling = w.provider("fx2", family="fixture", models_include=["*"])
    w.fx.entry.update(family="fixture", models_include=["*"],
                      handover_mode={"local": "shared"},
                      transcript={"limit_markers": [{"match": MARKER, "resets": True}]})
    sibling.entry["handover_mode"] = {"local": "shared"}
    w.project["quota_handover"] = {"enabled": True}
    try:
        w.start_scheduler()
        planned = w.simple("quota", "worker", fx={"text": MARKER, "exit": 1})
        call = w.wait_spawn("quota")
        def stopped():
            nodes = json.loads(w.paths.tree_file.read_text())["nodes"]
            return next((n for n in nodes.values() if n.get("node_id") == planned and n["status"] == "limited"), None)
        managed = w.until(stopped, what="managed quota stop")
        assert call["resume"] is None
        assert sibling.calls() == []
        run = Runner(w.paths, config.load(w.paths, seed=False))
        async def scenario():
            refusal = await run.steer(managed["id"], "switch", provider="fx2")
            assert "scheduler" in refusal["error"]
            # Even stale handover state cannot bypass the managed binding.
            run.tree.update(managed["id"], handover_attempt={"state": "prepared", "owner_pid": 0,
                                                          "from": "fx", "to": "fx2"})
            await run.resume_deferred()
        asyncio.run(scenario())
        assert sibling.calls() == []
        assert not any(e.get("kind", "").startswith("handover_") for e in w.events())
    finally:
        w.close()


@pytest.mark.parametrize("kind", ["local", "docker"])
def test_qh_r8_codex_profile_transfer_copies_only_the_session(tmp_path, kind):
    raw = config.load(None, seed=False).providers["codex"]
    profiles = {name: tmp_path / name for name in ("codex", "codex-b")}
    providers = load_providers({"codex": {**raw, "env": {"MULTIAGENTS_CODEX_PROFILE": str(profiles["codex"])}},
                               "codex-b": {"extends": "codex", "env": {"MULTIAGENTS_CODEX_PROFILE": str(profiles["codex-b"])}}})
    sid = str(uuid.uuid4())
    relative = Path("sessions/2026/10/04") / ("rollout-2026-10-04T01-00-00-" + sid + ".jsonl")
    source, target = profiles["codex"] / relative, profiles["codex-b"] / relative
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
    for name, profile in profiles.items():
        (profile / "auth.json").write_text(name + " credentials")
        (profile / "state_5.sqlite").write_text(name + " index")
        (profile / "sessions" / "unrelated.jsonl").write_text(name + " unrelated")
    executor = SimpleNamespace(kind=kind, private_state=lambda name: {Path("/home/agent/.codex"): profiles[name]})
    a, b = qh.transfer_paths(providers["codex"], providers["codex-b"], tmp_path, sid, executor, executor)
    qh.transfer_session(a, b)
    assert target.read_bytes() == source.read_bytes()
    for name, profile in profiles.items():
        assert (profile / "auth.json").read_text() == name + " credentials"
        assert (profile / "state_5.sqlite").read_text() == name + " index"
        assert (profile / "sessions" / "unrelated.jsonl").read_text() == name + " unrelated"


def test_qh_r19_known_vault_account_matches_authproxy_selection(tmp_path, monkeypatch):
    from multiagents.authproxy import Accounts
    from multiagents.executor.docker import DockerExecutor
    w = World(tmp_path, monkeypatch)
    providers = load_providers({"claude": {"container_account": "a"},
                               "claude-b": {"extends": "claude", "container_account": "b"}})
    executor = DockerExecutor({"auth_proxy": True}, w.paths, providers)
    vault = tmp_path / "vault"
    for label in ("a", "b"):
        path = vault / "accounts" / label / ".credentials.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-" + label}}))
    accounts = Accounts(vault, executor.account_pins())
    monkeypatch.setattr(w.r, "executor", lambda spec=None: executor)
    for name, provider in providers.items():
        actual = accounts.for_agent("agent-" + name, name)
        assert actual in accounts.labels()
        assert w.r._qh_segment(provider, w.r.config.agent("worker"))["account"] == actual
