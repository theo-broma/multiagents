"""RF-R1..R8: refusal is a visible, resumable non-success outcome.

The ag-da2c22 stream is not in this checkout. Its quoted final response in
BRIEF.md (lines 1602-1604) is the recorded fallback fixture below. All other
provider stream examples in this file are explicitly synthetic.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from support import c3_harness as h
from multiagents.providers import Provider
from multiagents.tree import Node, TERMINAL


SHIPPED = Path(__file__).resolve().parents[1] / "src/multiagents/defaults/providers.yaml"
RECORDED_AGY_RESPONSE = "This request was blocked by Gemini's filters…"


def _provider(tmp_path, events, *, name="p", refusal_markers=None, status_map=None,
              stderr="", exit_code=0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    block = h.fake_cli(tmp_path, name=name, events=events, stderr=stderr,
                       exit_code=exit_code)
    block["stream"]["rules"] = [
        {"match": {"type": "text"}, "as": "text", "fields": {"text": "text"}},
        {"match": {"type": "result"}, "as": "result",
         "fields": {"status": "status", "text": "text"},
         **({"status_map": status_map} if status_map else {})},
        {"match": {"type": "progress"}, "as": "step",
         "fields": {"status": "status"}},
    ]
    if refusal_markers is not None:
        block["refusal_markers"] = refusal_markers
    return block


def _runner(tmp_path, monkeypatch, block, *, writes=True, conversational=False):
    spec = h.AgentSpec("worker", "p", "m", writes=writes,
                       conversational=conversational)
    return h.make_runner(tmp_path / "project", monkeypatch,
                         agents={"worker": spec}, providers={"p": block})


def _run(runner):
    async def go():
        started = await runner.start("worker", "do the task")
        agent_id = started["agent_id"]
        await asyncio.wait_for(runner.runs[agent_id].done.wait(), 30)
        return agent_id
    return asyncio.run(go())


def _refusal_block(tmp_path, *, message="Denied by fixture filter", events=None,
                   stderr=""):
    # Synthetic provider and synthetic fixed wording.
    return _provider(tmp_path, events or [{"type": "text", "text": message}],
                     refusal_markers=[r"Denied by fixture filter"], stderr=stderr)


def test_rf_r1_refused_is_terminal_and_visible_in_wait_check_collect(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch, _refusal_block(tmp_path))
    agent_id = _run(r)
    node = r.tree.get(agent_id)
    waited = asyncio.run(r.wait_for_any([agent_id], 0))
    assert "refused" in TERMINAL
    assert node.status == "refused"
    assert "filter" in node.reason.lower() and len(node.reason) <= 200
    assert waited["changed"][0]["status"] == "refused"
    assert waited["changed"][0]["reason"] == node.reason
    assert r.check(agent_id)["status"] == "refused"
    assert r.collect(agent_id)["status"] == "refused"


def test_rf_r1_repeated_refusals_do_not_cool_or_exhaust_provider(tmp_path, monkeypatch):
    r = _runner(tmp_path, monkeypatch, _refusal_block(tmp_path))
    r.tree.set_budget("refusal-fixture", 100)
    before = r.tree.read()
    for _ in range(5):
        agent_id = _run(r)
        assert r.tree.get(agent_id).status == "refused"
    health = r.tree.provider_health().get("p", {})
    assert health.get("consecutive_failures", 0) == 0
    assert r.tree.read().get("budgets") == before.get("budgets")
    assert r.tree.read().get("cooldowns") == before.get("cooldowns")
    assert r.tree.cooldown("p") is None


def test_rf_r1_refused_child_does_not_auto_merge_into_parent(tmp_path, monkeypatch):
    # The fake CLI makes a real commit on the child branch before printing a
    # synthetic fixed refusal. A merge would visibly move the parent's HEAD.
    script = tmp_path / "refusing-writer.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import pathlib, subprocess\n"
        "pathlib.Path('child-work.txt').write_text('child work\\n')\n"
        "subprocess.run(['git', 'add', 'child-work.txt'], check=True)\n"
        "subprocess.run(['git', '-c', 'user.name=fixture', "
        "'-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'child work'], "
        "check=True, stdout=subprocess.DEVNULL)\n"
        "print('{\"type\":\"text\",\"text\":\"Denied by fixture filter\"}')\n")
    script.chmod(0o755)
    block = _refusal_block(tmp_path / "provider")
    block["bin"] = str(script)
    r = _runner(tmp_path, monkeypatch, block)
    parent_path = tmp_path / "parent-worktree"
    parent_branch = h.gitops.create_worktree(r.paths.root, parent_path, "agents/parent")
    r.tree.add(Node(id="ag-parent", agent="parent", provider="p", model="m",
                    parent=None, depth=1, status="failed", branch=parent_branch,
                    worktree=str(parent_path)))
    parent_head = h.gitops.head_sha(parent_path)
    h.as_subagent(monkeypatch, agent_id="ag-parent", depth=1, can_spawn=True)
    agent_id = _run(r)
    child = r.tree.get(agent_id)
    assert child.status == "refused"
    assert child.parent == "ag-parent"
    assert h.gitops.head_sha(parent_path) == parent_head
    assert not (parent_path / "child-work.txt").exists()
    assert child.branch and h.gitops.branch_exists(r.paths.root, child.branch)


def test_rf_r1_resume_cli_lists_refused_branch(tmp_path, monkeypatch, capsys):
    import multiagents.cli as cli

    r = _runner(tmp_path, monkeypatch,
                _provider(tmp_path, [{"type": "text", "text": "finished"}]))
    agent_id = _run(r)
    node = r.tree.get(agent_id)
    assert node.branch
    r.tree.set_status(agent_id, "refused", "fixture refusal")
    code = cli.cmd_resume(argparse.Namespace(
        path=str(r.paths.root), no_launch=True, resume=True, wait=False,
        unattended=0, team="", supervise=True))
    output = capsys.readouterr().out
    assert code == 0
    assert "branch(es) still held by unfinished agents" in output
    assert agent_id in output and node.branch in output


def test_rf_r1_refused_run_can_be_steered_on_same_node(tmp_path, monkeypatch):
    block = _refusal_block(tmp_path, events=[
        {"type": "text", "text": "Denied by fixture filter", "session_id": "sess-1"}])
    block["stream"]["session_id_paths"] = ["session_id"]
    r = _runner(tmp_path, monkeypatch, block)
    agent_id = _run(r)
    assert r.tree.get(agent_id).status == "refused"
    steered = asyncio.run(r.steer(agent_id, "try again"))
    assert steered.get("steered") is True, steered
    assert steered.get("agent_id") == agent_id


def test_rf_r2_declared_structured_signal_stays_sticky(tmp_path, monkeypatch):
    # Synthetic structured refusal followed by a provider progress status.
    events = [{"type": "text", "text": "some output"},
              {"type": "result", "status": "FILTERED"},
              {"type": "progress", "status": "SUCCESS"}]
    block = _provider(tmp_path, events,
                      status_map={"status": {"FILTERED": "REFUSED"}})
    r = _runner(tmp_path, monkeypatch, block)
    agent_id = _run(r)
    assert r.tree.get(agent_id).status == "refused"
    assert "FILTERED" in r.tree.get(agent_id).reason


def test_rf_r2_provider_declaration_controls_marker_and_quoted_phrase_stays_done(
        tmp_path, monkeypatch):
    # The negative fixture required by RF-R2: successful prose quotes a stock
    # phrase inside a longer answer. This is synthetic around the recorded quote.
    message = f"The sample says, '{RECORDED_AGY_RESPONSE}' and then continues."
    block = _provider(tmp_path, [{"type": "text", "text": message}],
                      refusal_markers=[r"This request was blocked by Gemini's filters.*"])
    r = _runner(tmp_path, monkeypatch, block)
    assert r.tree.get(_run(r)).status == "done"

    bare = _provider(tmp_path / "bare", [{"type": "text", "text": RECORDED_AGY_RESPONSE}],
                     refusal_markers=[])
    r2 = _runner(tmp_path / "second", monkeypatch, bare)
    assert r2.tree.get(_run(r2)).status == "done"


def test_rf_r2_final_assistant_message_only_controls_marker(tmp_path, monkeypatch):
    events = [{"type": "text", "text": "Denied by fixture filter"},
              {"type": "text", "text": "The issue was resolved and work is done."}]
    r = _runner(tmp_path, monkeypatch, _refusal_block(tmp_path, events=events))
    assert r.tree.get(_run(r)).status == "done"


def test_rf_r2_truncation_precedes_refusal(tmp_path, monkeypatch):
    block = _refusal_block(tmp_path, stderr="time limit")
    block["truncation_markers"] = ["time limit"]
    r = _runner(tmp_path, monkeypatch, block)
    assert r.tree.get(_run(r)).status == "truncated"


def test_rf_r3_shipped_claude_structured_signals():
    blocks = yaml.safe_load(SHIPPED.read_text())["providers"]
    claude = Provider.from_dict("claude", blocks["claude"])
    refusal = claude.parse_line(json.dumps({"type": "result", "subtype": "success",
                                             "stop_reason": "refusal"}))
    turns = claude.parse_line(json.dumps({"type": "result", "subtype": "error_max_turns"}))
    assert refusal.status == "REFUSED"  # synthetic event schema
    assert turns.status == "TRUNCATED"  # synthetic event schema


def test_rf_r3_shipped_opencode_terminal_signals():
    blocks = yaml.safe_load(SHIPPED.read_text())["providers"]
    opencode = Provider.from_dict("opencode", blocks["opencode"])
    # The contract leaves the native values to source research. Assert the
    # shipped declaration has both categories, then exercise each declared
    # value through the public parser. No guessed value becomes a contract.
    rules = blocks["opencode"]["stream"]["rules"]
    mapped = [(path, native, normalized)
              for rule in rules if rule.get("match", {}).get("type") == "step_finish"
              for path, values in rule.get("status_map", {}).items()
              for native, normalized in values.items()]
    assert any(kind == "REFUSED" for _, _, kind in mapped)
    assert any(kind == "TRUNCATED" for _, _, kind in mapped)
    for path, native, normalized in mapped:
        assert path == "part.reason"
        event = opencode.parse_line(json.dumps({"type": "step_finish",
                                                "part": {"reason": native}}))
        assert event.status == normalized


def test_rf_r4_recorded_agy_response_is_refused(tmp_path, monkeypatch):
    blocks = yaml.safe_load(SHIPPED.read_text())["providers"]
    agy = blocks["agy"]
    # Recorded text from BRIEF.md, wrapped in a synthetic result event because
    # the original NDJSON stream is absent from this checkout.
    event = {"event": "result", "result": {"status": "SUCCESS",
                                              "response": RECORDED_AGY_RESPONSE}}
    script = h.fake_cli(tmp_path, events=[event])
    agy = {**agy, "bin": script["bin"], "spawn": script["spawn"]}
    r = h.make_runner(tmp_path / "project", monkeypatch,
                      agents={"worker": h.AgentSpec("worker", "agy", "m")},
                      providers={"agy": agy})
    agent_id = _run(r)
    assert r.tree.get(agent_id).status == "refused"
    assert "Gemini" in r.tree.get(agent_id).reason


@pytest.mark.parametrize("writes,commit,flag", [(True, False, True),
                                                 (True, True, False),
                                                 (False, False, False)])
def test_rf_r5_no_commits_flag_only_for_empty_writing_run(
        tmp_path, monkeypatch, writes, commit, flag):
    r = _runner(tmp_path, monkeypatch,
                _provider(tmp_path, [{"type": "text", "text": "finished"}]),
                writes=writes)
    agent_id = _run(r)
    if commit:
        node = r.tree.get(agent_id)
        worktree = Path(node.worktree)
        (worktree / "change.txt").write_text("change")
        subprocess.run(["git", "-C", str(worktree), "add", "change.txt"], check=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-m", "change"],
                       check=True, capture_output=True)
    assert r.tree.get(agent_id).status == "done"
    results = [r.check(agent_id), r.collect(agent_id),
               asyncio.run(r.wait_for_any([agent_id], 0))["changed"][0]]
    for result in results:
        assert result.get("no_commits", False) is flag, result
        if flag:
            notes = [v for v in result.values() if isinstance(v, str)
                     and "commit" in v.lower() and v != result.get("status")]
            assert any(len(v.splitlines()) == 1 for v in notes), result


@pytest.mark.parametrize("status", ["refused", "failed", "truncated", "limited", "done"])
def test_rf_r6_merge_reports_prior_non_done_status(tmp_path, monkeypatch, status):
    r = _runner(tmp_path, monkeypatch,
                _provider(tmp_path, [{"type": "text", "text": "work"}]))
    agent_id = _run(r)
    node = r.tree.get(agent_id)
    worktree = Path(node.worktree)
    (worktree / "change.txt").write_text("change")
    subprocess.run(["git", "-C", str(worktree), "add", "change.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-m", "change"],
                   check=True, capture_output=True)
    if status != "done":
        r.tree.set_status(agent_id, status, "fixture outcome")
    # HA-R11: the target must be a recorded worktree, so merge into a second
    # agent's rather than an arbitrary checkout.
    target = r.tree.get(_run(r)).worktree
    result = r.merge_agent(agent_id, into=target)
    assert result["result"] == "merged", result
    if status == "done":
        assert "status_before_merge" not in result
        assert "warning" not in result
    else:
        assert result.get("status_before_merge") == status
        assert "not" in result.get("warning", "").lower()


def test_rf_r8_refused_consult_is_not_advice_and_next_turn_resumes(tmp_path, monkeypatch):
    block = _provider(tmp_path, [{"type": "text", "text": "Denied by fixture filter",
                                  "session_id": "sess-refused"}],
                      refusal_markers=[r"Denied by fixture filter"])
    block["stream"]["session_id_paths"] = ["session_id"]
    r = _runner(tmp_path, monkeypatch, block, conversational=True)
    first = asyncio.run(r.consult("worker", "first", timeout=30))
    assert first["status"] == "refused", first
    assert first.get("reply") in (None, ""), first
    assert first.get("reason")
    assert any("rephras" in str(v).lower() or "consult again" in str(v).lower()
               for v in first.values())
    second = asyncio.run(r.consult("worker", "second", timeout=30))
    assert second["agent_id"] == first["agent_id"]
    assert second["turn"] == first["turn"] + 1
