"""TB-R5/TB-R7 regression coverage for consult, steer and fallback boundaries."""
from __future__ import annotations

import asyncio

from multiagents.tree import Node
from test_conversation_provider_change import _calls, _events
from test_fo_fallback_options import _opt, _providers, _runner
from test_tb_c_conversation_fingerprint import _change_roster, _consult, _setup
from test_tb_c_no_autocommit_readonly import (
    SCRATCH, invocations, make, run_to_end, wip_commits_anywhere,
)


def test_tb_r7_fallback_option_change_replaces_on_the_fallback(tmp_path, monkeypatch):
    providers, probes = _providers(tmp_path, "acme", "zeta")
    base = {"provider": "zeta", "model": "z1", "conversational": True,
            "models": {"acme": {"model": "a1", "variant": "old"}}}
    runner = _runner(tmp_path, monkeypatch, base, providers)
    worktree = runner.paths.worktree("ag-f0a0c1")
    worktree.mkdir(parents=True)
    runner.tree.add(Node(id="ag-f0a0c1", agent="worker", provider="acme", model="a1",
                         parent=None, depth=1, status="idle", session_id="sess-old",
                         worktree=str(worktree), conversation=True, turns=1))
    first = _consult(runner)
    assert runner.tree.get(first["agent_id"]).launch_fingerprint == {
        "provider": "acme", "model": "a1", "effort": "", "options": {"variant": "old"}}
    _change_roster(runner, base, models={"acme": {"model": "a1", "variant": "new"}})

    second = _consult(runner)

    assert not second.get("error"), second
    assert second["agent_id"] != first["agent_id"]
    assert not _calls(probes["zeta"])
    argv = _calls(probes["acme"])[-1]
    assert "--resume" not in argv
    assert _opt(argv, "--model") == "a1"
    assert _opt(argv, "--variant") == "new"
    event = _events(runner, "conversation_replaced")[0]
    assert event["old_fingerprint"]["options"] == {"variant": "old"}
    assert event["new_fingerprint"]["options"] == {"variant": "new"}


def test_tb_r7_persisted_fingerprint_survives_loss_of_the_in_process_run(tmp_path, monkeypatch):
    runner, probe, base = _setup(tmp_path, monkeypatch, variant="old")
    first = _consult(runner)
    runner.runs.clear()
    _change_roster(runner, base, variant="new")

    second = _consult(runner)

    assert not second.get("error"), second
    assert second["agent_id"] != first["agent_id"]
    assert "--resume" not in _calls(probe)[-1]
    assert len(_events(runner, "conversation_replaced")) == 1


def test_tb_r5_non_writing_consult_never_commits_scratch(tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [SCRATCH], writes=False)
    runner.config.agents["worker"] = runner.config.agents["worker"].replace(conversational=True)

    first = _consult(runner)
    second = _consult(runner)

    assert not first.get("error") and not second.get("error"), (first, second)
    assert first["agent_id"] == second["agent_id"]
    assert invocations(probe) == 2
    assert wip_commits_anywhere(project) == []


def test_tb_r5_non_writing_steer_never_commits_scratch(tmp_path, monkeypatch):
    runner, project, probe = make(tmp_path, monkeypatch, [SCRATCH], writes=False)
    agent_id = run_to_end(runner)

    async def resume():
        result = await runner.steer(agent_id, "continue")
        assert not result.get("error"), result
        await asyncio.wait_for(runner.runs[agent_id].done.wait(), timeout=20)

    asyncio.run(resume())

    assert invocations(probe) == 2
    assert wip_commits_anywhere(project) == []
    assert not runner.tree.get(agent_id).branch
