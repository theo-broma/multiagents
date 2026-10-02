"""Black-box contract for RT-R1..R2 and LM-R1..R2.

Real Runner lifecycle and fake provider CLIs are used. Only provider quota
readings are stubbed, so tests do not depend on a developer's subscriptions.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.runner import Runner  # noqa: E402
from multiagents.tree import Node  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402


def _budgets(monkeypatch, **headroom):
    readings = {name: budget_mod.Budget(name, known=True, headroom=room)
                for name, room in headroom.items()}
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: readings)


def _start(runner, name="worker", **kwargs):
    async def go():
        result = await runner.start(name, "work", **kwargs)
        run = runner.runs.get(result.get("agent_id"))
        if run:
            await asyncio.wait_for(run.done.wait(), 15)
        return result
    return asyncio.run(go())


def _events(runner, kind):
    path = runner.paths.events_file
    return [entry for line in path.read_text().splitlines()
            if (entry := json.loads(line)).get("kind") == kind] if path.exists() else []


def _limit(result, field):
    # Only value and source are H4's contract; D1 adds sibling keys such as
    # source_detail, which its own tests cover.
    entry = result.get("effective_limits", {}).get(field)
    return {k: entry.get(k) for k in ("value", "source")} if isinstance(entry, dict) else entry


def _runner(tmp_path, monkeypatch, *, agent=None, project=None, providers=None):
    monkeypatch.setattr(Runner, "WATCH_POLL_SECONDS", WATCH_POLL)
    if providers is None:
        provider, _ = _fake_cli(tmp_path, "acme")
        providers = {"acme": provider}
    if agent is None:
        agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1"})
    return h.make_runner(tmp_path / "project", monkeypatch,
                         agents={"worker": agent}, providers=providers,
                         project=project or {})


def test_rt_r1_unmodelled_chain_entry_is_not_a_candidate_and_emits_no_skip(tmp_path, monkeypatch):
    # Revised by FS-R1/FS-R4: the project chain adds no candidates, so a chain
    # entry the agent has no model for is not skipped (no route_skipped event)
    # and is never named. The exhausted agent defers.
    main, main_probe = _fake_cli(tmp_path, "acme")
    other, other_probe = _fake_cli(tmp_path, "zeta")
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    r = _runner(tmp_path, monkeypatch, providers={"acme": main, "zeta": other},
                project={"budget": {"fallback_chain": ["zeta"]}})

    result = _start(r)

    assert result.get("deferred"), result
    assert not _calls(main_probe) and not _calls(other_probe)
    assert not _events(r, "route_skipped")
    assert "zeta" not in json.dumps(result), result


def test_rt_r1_nonempty_fallback_routes_with_its_model(tmp_path, monkeypatch):
    main, main_probe = _fake_cli(tmp_path, "acme")
    other, other_probe = _fake_cli(tmp_path, "zeta")
    _budgets(monkeypatch, acme=0.0, zeta=1.0)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "models": {"zeta": "z1"}})
    r = _runner(tmp_path, monkeypatch, agent=agent,
                providers={"acme": main, "zeta": other},
                project={"budget": {"fallback_chain": ["zeta"]}})

    result = _start(r)

    assert result.get("provider") == "zeta", result
    assert not _calls(main_probe)
    assert _flag(_calls(other_probe)[0], "--model") == "z1"
    assert not _events(r, "route_skipped")


def test_rt_r1_unlisted_same_family_sibling_is_not_a_fallback(tmp_path, monkeypatch):
    # Revised by FS-R2: a family sibling is a candidate only if listed in
    # `models:`; with the preferred provider exhausted the start defers.
    main, main_probe = _fake_cli(tmp_path, "acme")
    other, other_probe = _fake_cli(tmp_path, "acme2")
    other["family"] = "acme"
    _budgets(monkeypatch, acme=0.0, acme2=1.0)
    r = _runner(tmp_path, monkeypatch,
                providers={"acme": main, "acme2": other})

    result = _start(r)

    assert result.get("deferred"), result
    assert not result.get("agent_id"), result
    assert not _calls(main_probe) and not _calls(other_probe)


def test_rt_r1_listed_same_family_sibling_inherits_nonempty_model(tmp_path, monkeypatch):
    # The original intent, with the sibling listed (entry names no model, FO-R1c).
    main, main_probe = _fake_cli(tmp_path, "acme")
    other, other_probe = _fake_cli(tmp_path, "acme2")
    other["family"] = "acme"
    _budgets(monkeypatch, acme=0.0, acme2=1.0)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "models": {"acme2": {"effort": "low"}}})
    r = _runner(tmp_path, monkeypatch, agent=agent,
                providers={"acme": main, "acme2": other})

    result = _start(r)

    assert result.get("provider") == "acme2", result
    assert not _calls(main_probe)
    assert _flag(_calls(other_probe)[0], "--model") == "m1"
    assert not _events(r, "route_skipped")


@pytest.mark.parametrize("entry", ["", {"model": ""}, {"effort": "low"}],
                         ids=["empty", "empty-mapping", "options-only"])
def test_rt_r2_steer_refuses_unmodelled_recorded_provider_without_mutation(
        tmp_path, monkeypatch, entry):
    primary, primary_probe = _fake_cli(tmp_path, "acme")
    old, old_probe = _fake_cli(tmp_path, "zeta")
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "models": {"zeta": entry}})
    r = _runner(tmp_path, monkeypatch, agent=agent,
                providers={"acme": primary, "zeta": old})
    worktree = r.paths.worktree("ag-steer")
    worktree.mkdir(parents=True)
    marker = worktree / "keep.txt"
    marker.write_text("unchanged")
    node = Node(id="ag-steer", agent="worker", provider="zeta", model="z1",
                parent=None, depth=1, status="idle", session_id="sess-old",
                worktree=str(worktree))
    r.tree.add(node)

    result = asyncio.run(r.steer(node.id, "continue"))

    assert result.get("steered") is False, result
    error = result.get("error", "")
    assert "worker" in error and "zeta" in error and "models.zeta" in error
    assert not _calls(primary_probe) and not _calls(old_probe)
    after = r.tree.get(node.id)
    assert (after.status, after.session_id, after.worktree) == ("idle", "sess-old", str(worktree))
    assert marker.read_text() == "unchanged"


def test_rt_r2_steer_uses_family_model_on_recorded_sibling(tmp_path, monkeypatch):
    primary, primary_probe = _fake_cli(tmp_path, "acme")
    sibling, sibling_probe = _fake_cli(tmp_path, "acme2")
    sibling["family"] = "acme"
    r = _runner(tmp_path, monkeypatch,
                providers={"acme": primary, "acme2": sibling})
    worktree = r.paths.worktree("ag-sibling")
    worktree.mkdir(parents=True)
    r.tree.add(Node(id="ag-sibling", agent="worker", provider="acme2", model="m1",
                    parent=None, depth=1, status="idle", session_id="sess-old",
                    worktree=str(worktree)))

    result = asyncio.run(r.steer("ag-sibling", "continue"))

    assert result.get("steered") is True, result
    assert not _calls(primary_probe)
    assert _flag(_calls(sibling_probe)[0], "--model") == "m1"


@pytest.mark.parametrize("field,project_key,project_value,agent_value,builtin", [
    ("timeout", "default_timeout", 4, 6, 900),
    ("max_children", "max_children", 1, 3, 2),
    ("silence_timeout", "silence_timeout", 4, 6, 180),
])
@pytest.mark.parametrize("configuration", ["project", "agent", "builtin", "explicit_builtin"])
def test_lm_r1_and_r2_report_project_agent_and_builtin_precedence(
        tmp_path, monkeypatch, field, project_key, project_value, agent_value,
        builtin, configuration):
    _budgets(monkeypatch, acme=1.0)
    fields, limits, expected, source = {
        "project": ({}, {project_key: project_value}, project_value, "project"),
        "agent": ({field: agent_value}, {project_key: project_value}, agent_value, "agent"),
        "builtin": ({}, {}, builtin, "builtin"),
        "explicit_builtin": ({field: builtin}, {project_key: project_value}, builtin, "agent"),
    }[configuration]
    provider, _ = _fake_cli(tmp_path, "acme")
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1", **fields})
    r = _runner(tmp_path, monkeypatch, agent=agent, providers={"acme": provider},
                project={"limits": limits})
    result = _start(r)
    assert _limit(result, field) == {
        "value": expected, "source": source}, (field, configuration, result)


def test_lm_r1_and_r2_explicit_call_timeout_wins(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "timeout": 6})
    r = _runner(tmp_path, monkeypatch, agent=agent,
                project={"limits": {"default_timeout": 4}})
    result = _start(r, timeout=2)
    assert _limit(result, "timeout") == {
        "value": 2, "source": "call"}, result


def _slow_provider(tmp_path, seconds=3):
    script = tmp_path / "slow.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, time\n"
        f"time.sleep({seconds})\n"
        "print(json.dumps({'type': 'text', 'text': 'late answer', 'session': 's1'}), flush=True)\n")
    script.chmod(0o755)
    return {"bin": str(script), "spawn": {"args": ["--model", "{model}"],
                                           "resume": ["--resume", "{session_id}"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session"],
                       "rules": [{"match": {"type": "text"}, "as": "text",
                                  "fields": {"text": "text"}}]}}


# LM-R1 as amended 2026-09-29 (SV-R4: a watchdog never kills): the wall-clock
# observable is a `stuck` report with reason `timeout` once the effective value
# has passed, not a kill. The runs below outlive the effective timeout plus the
# watchdog's polling, and are far shorter than any other candidate value.
# TS-R2: the watchdog polls every WATCH_POLL here instead of production's 5 s.
WATCH_POLL = 0.25
OUTLIVES_TIMEOUT = 3


def _timed_out(runner, agent_id):
    return [event for event in _events(runner, "stuck")
            if event.get("agent") == agent_id and event.get("reason") == "timeout"]


def test_lm_r1_project_timeout_is_enforced_on_start(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    r = _runner(tmp_path, monkeypatch,
                providers={"acme": _slow_provider(tmp_path, seconds=OUTLIVES_TIMEOUT)},
                project={"limits": {"default_timeout": 1}})
    result = _start(r)
    assert _limit(result, "timeout") == {
        "value": 1, "source": "project"}, result
    assert _timed_out(r, result["agent_id"]), _events(r, "stuck")
    # Reported, not killed: the run still finishes with its own answer.
    node = r.tree.get(result["agent_id"])
    assert node.status == "done", node


def test_lm_r1_project_silence_timeout_is_enforced_on_start(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    r = _runner(tmp_path, monkeypatch,
                providers={"acme": _slow_provider(tmp_path, seconds=OUTLIVES_TIMEOUT)},
                project={"limits": {"silence_timeout": 1}})
    _start(r)
    assert any(event.get("reason") == "silence" for event in _events(r, "stuck"))


def test_lm_r1a_parent_cap_refuses_second_child_with_source(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    provider, _ = _fake_cli(tmp_path, "acme")
    agents = {
        "parent": AgentSpec.from_dict("parent", {"provider": "acme", "model": "m1",
                                                  "max_children": 1}),
        "child": AgentSpec.from_dict("child", {"provider": "acme", "model": "m1",
                                                "max_children": 9}),
    }
    r = h.make_runner(tmp_path / "project", monkeypatch, agents=agents,
                      providers={"acme": provider},
                      project={"limits": {"max_children": 5}})
    parent = Node(id="ag-parent", agent="parent", provider="acme", model="m1",
                  parent=None, depth=1, status="running", session_id="s",
                  worktree=str(tmp_path / "project"))
    child = Node(id="ag-first", agent="child", provider="acme", model="m1",
                 parent=parent.id, depth=2, status="running")
    r.tree.add(parent)
    r.tree.add(child)
    h.as_subagent(monkeypatch, agent_id=parent.id, depth=1, can_spawn=True)
    with pytest.raises((PermissionError, RuntimeError)) as exc:
        asyncio.run(r.start("child", "second"))
    assert "1" in str(exc.value) and "agent" in str(exc.value).lower()


def test_lm_r1a_root_is_not_capped_by_max_children(tmp_path, monkeypatch):
    """LM-R1a as amended 2026-09-29: the root orchestrator's top-level agents
    are bounded by max_concurrent only. Replaces the former root-cap test,
    which asserted the refusal this amendment removes."""
    _budgets(monkeypatch, acme=1.0)
    r = _runner(tmp_path, monkeypatch,
                providers={"acme": _slow_provider(tmp_path, seconds=OUTLIVES_TIMEOUT)},
                project={"limits": {"max_children": 1, "max_concurrent": 3}})
    h.as_root(monkeypatch)

    async def scenario():
        started = []
        try:
            for n in range(3):
                result = await r.start("worker", f"task {n}")
                started.append(result.get("agent_id"))
                assert result.get("agent_id") and not result.get("error"), result
            running = [r.tree.get(agent_id) for agent_id in started]
            assert all(node.parent is None and node.status in {"pending", "running"}
                       for node in running), running
        finally:
            for agent_id in started:
                if agent_id:
                    await r.stop(agent_id)

    asyncio.run(scenario())


def test_lm_r1a_parent_cap_is_recorded_at_parent_start(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    provider = _slow_provider(tmp_path, seconds=3)
    parent = AgentSpec.from_dict("parent", {"provider": "acme", "model": "m1",
                                            "max_children": 1, "can_spawn": True})
    child = AgentSpec.from_dict("child", {"provider": "acme", "model": "m1",
                                          "max_children": 9})
    project = {"limits": {"max_children": 5}}
    r = h.make_runner(tmp_path / "project", monkeypatch,
                      agents={"parent": parent, "child": child},
                      providers={"acme": provider}, project=project)

    async def scenario():
        started = await r.start("parent", "work")
        parent_id = started["agent_id"]
        r.tree.add(Node(id="ag-first", agent="child", provider="acme", model="m1",
                        parent=parent_id, depth=2, status="running"))
        # Changing the roster after launch must not rewrite this run's cap.
        changed = AgentSpec.from_dict("parent", {"provider": "acme", "model": "m1",
                                                "max_children": 9, "can_spawn": True})
        r.reload(h.make_config(agents={"parent": changed, "child": child},
                               providers={"acme": provider}, project=project))
        h.as_subagent(monkeypatch, agent_id=parent_id, depth=1, can_spawn=True)
        try:
            with pytest.raises((PermissionError, RuntimeError)) as exc:
                await r.start("child", "second")
            assert "1" in str(exc.value) and "agent" in str(exc.value).lower()
        finally:
            h.as_root(monkeypatch)
            await r.stop(parent_id)

    asyncio.run(scenario())


def test_lm_r1b_consult_uses_project_timeout(tmp_path, monkeypatch):
    provider = _slow_provider(tmp_path, seconds=OUTLIVES_TIMEOUT)
    agent = AgentSpec.from_dict("worker", {"provider": "acme", "model": "m1",
                                           "conversational": True})
    r = _runner(tmp_path, monkeypatch, agent=agent, providers={"acme": provider},
                project={"limits": {"default_timeout": 1}})
    result = asyncio.run(asyncio.wait_for(r.consult("worker", "hello"), 30))
    assert result.get("agent_id"), result
    # The consult's run is watched against the project value (1 s), not the
    # built-in 900 s: it is reported stuck on timeout while it is still running.
    assert _timed_out(r, result["agent_id"]), (result, _events(r, "stuck"))


def test_lm_r1b_steer_keeps_original_call_timeout(tmp_path, monkeypatch):
    _budgets(monkeypatch, acme=1.0)
    count = tmp_path / "invocations.txt"
    script = tmp_path / "two-turns.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, time\n"
        f"p = pathlib.Path({str(count)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        f"if n > 1: time.sleep({OUTLIVES_TIMEOUT})\n"
        "print(json.dumps({'type': 'text', 'text': 'answer', 'session': 's1'}), flush=True)\n")
    script.chmod(0o755)
    provider = {"bin": str(script),
                "spawn": {"args": ["--model", "{model}"],
                          "resume": ["--resume", "{session_id}"]},
                "stream": {"format": "ndjson", "session_id_paths": ["session"],
                           "rules": [{"match": {"type": "text"}, "as": "text",
                                      "fields": {"text": "text"}}]}}
    r = _runner(tmp_path, monkeypatch, providers={"acme": provider},
                project={"limits": {"default_timeout": 10}})
    first = _start(r, timeout=1)
    assert count.read_text() == "1", first
    assert r.tree.get(first["agent_id"]).session_id == "s1"

    agent_id = first["agent_id"]
    assert not _timed_out(r, agent_id), "the first turn finished well inside 1s"

    async def steer_and_wait():
        steered = await r.steer(agent_id, "continue")
        run = r.runs.get(agent_id)
        if run:
            await asyncio.wait_for(run.done.wait(), 20)
        return steered

    steered = asyncio.run(steer_and_wait())

    assert steered.get("steered") is True, steered
    assert count.read_text() == "2"
    # The steered turn is watched against the original call value (1 s), not
    # the project's 10 s: it is reported stuck on timeout before it ends.
    assert _timed_out(r, agent_id), _events(r, "stuck")


def test_lm_r1c_loaded_project_field_presence_survives_config_layers(tmp_path, monkeypatch):
    # The loaded configuration, rather than a hand-built AgentSpec, must know
    # whether the project agent actually set the field.
    import yaml
    from multiagents.config import load
    from multiagents.paths import ProjectPaths
    _budgets(monkeypatch, acme=1.0)
    root = tmp_path / "loaded-project"
    h.make_git_repo(root)
    paths = ProjectPaths(root)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "project.yaml").write_text(yaml.safe_dump({
        "team": "", "limits": {"default_timeout": 4, "max_children": 1, "silence_timeout": 4}}))
    (paths.config / "agents.yaml").write_text(yaml.safe_dump({
        "agents": {"worker": {"provider": "acme", "model": "m1"}}}))
    provider, _ = _fake_cli(tmp_path, "acme")
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({
        "providers": {"acme": provider}}))
    h.as_root(monkeypatch)
    from multiagents.runner import Runner
    r = Runner(paths, load(paths, seed=False))
    result = _start(r)
    for field, value in {"timeout": 4, "max_children": 1,
                         "silence_timeout": 4}.items():
        assert _limit(result, field) == {
            "value": value, "source": "project"}, result

    # A second Runner using only the mounted project layers represents the
    # nested server's view; it must resolve the same project settings.
    h.as_subagent(monkeypatch, agent_id="ag-nested", depth=1, can_spawn=True)
    nested = Runner(paths, load(paths, seed=False))
    nested_result = _start(nested)
    assert nested_result.get("effective_limits") == result.get("effective_limits")
