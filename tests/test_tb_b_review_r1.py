"""TB-R4 regressions for review ag-e2ca36 and classification decision e55b3e8c."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import c3_harness as h
from nc_fixture.m3_adv import Harness
from multiagents import cli
from multiagents.runner import Runner
from multiagents.scheduler import rate_limits
from multiagents.scheduler.engine import save_attempt
from multiagents.supervisor import looks_like_quota_failure
from test_tb_b_rate_limit_resume import make_world, rl, LONG_WAIT


def classify(status, stderr, *, code=1, text=""):
    run = SimpleNamespace(final_status=status, final_assistant_message="",
                          provider=h.make_provider(), stop_requested=False)
    return Runner._classify(Runner.__new__(Runner), run, code, text, stderr)


@pytest.mark.parametrize("stderr", [
    'Traceback (most recent call last):\n  File "worker.py", line 429, in run\nTypeError: broken',
    "Error: processed 429 files before crashing",
    "Error: request 429 failed",
    "Error: 1429 files processed",
    "Error: response code 4290",
    "429",
])
def test_tb_r4_numbers_in_an_ordinary_crash_are_not_rate_limits(stderr):
    assert not Runner._rate_limit_signal("failed", stderr)
    assert not looks_like_quota_failure("failed", stderr)
    assert classify("failed", stderr) == "failed"


@pytest.mark.parametrize("signal", [
    "429 Too Many Requests", "status 429", "status: 429", "HTTP 429",
    "HTTP/1.1 429", "code 429", "code=429", "Error: HTTP 429 Too Many Requests",
    "Error: 429 (rate limit exceeded)", "rate_limit: 429",
    "rate-limited request (429)", "rate limited - the request will be retried",
    "Selected model is at capacity", "Model at capacity; retry later",
])
@pytest.mark.parametrize("channel", ["stderr", "status"])
def test_tb_r4_provider_status_forms_and_capacity_are_rate_limits(signal, channel):
    status, stderr = ("failed", signal) if channel == "stderr" else (signal, "")
    assert Runner._rate_limit_signal(status, stderr)
    assert classify(status, stderr) == "rate_limited"


def test_tb_r4_capacity_overrides_a_provider_refusal_status():
    assert classify("REFUSED", "Selected model is at capacity") == "rate_limited"


@pytest.mark.parametrize("text", ["Selected model is at capacity", "HTTP 429 Too Many Requests"])
def test_tb_r4_an_answer_discussing_capacity_or_429_is_not_a_signal(text):
    assert classify("success", "", code=0, text=text) == "done"
    assert classify("failed", "ordinary crash", text=text) == "failed"


def test_tb_r4_a_successful_answer_outranks_stale_rate_limit_stderr():
    assert classify("success", "HTTP 429 Too Many Requests", code=0, text="finished") == "done"


def run_to_end(runner):
    async def go():
        started = await runner.start("worker", "work")
        run = runner.runs[started["agent_id"]]
        await asyncio.wait_for(run.done.wait(), 15)
        return run.node_id
    return asyncio.run(go())


@pytest.mark.parametrize("stderr", ["HTTP 429 Too Many Requests", "Selected model is at capacity"])
def test_tb_r4_a_quiet_rate_limit_keeps_empty_text_and_no_crash_summary(tmp_path, monkeypatch, stderr):
    provider = h.fake_cli(tmp_path, "p", exit_code=1, stderr=stderr)
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           providers={"p": provider},
                           agents={"worker": h.AgentSpec("worker", "p", "m", writes=True)})
    run_id = run_to_end(runner)
    record = json.loads((runner.paths.run_dir(run_id) / "result.json").read_text())
    assert record["status"] == "rate_limited" and record["cause"] == "rate_limited"
    assert record["text"] == ""
    assert not record.get("warnings")
    assert runner.tree.get(run_id).summary == ""
    assert not runner.tree.provider_health().get("p", {}).get("consecutive_failures")


def test_tb_r4_a_traceback_on_line_429_counts_as_a_crash(tmp_path, monkeypatch):
    stderr = 'Traceback (most recent call last):\n  File "worker.py", line 429, in run\nTypeError: broken'
    provider = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "started work"}],
                          exit_code=1, stderr=stderr)
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           providers={"p": provider},
                           agents={"worker": h.AgentSpec("worker", "p", "m", writes=True)})
    run_id = run_to_end(runner)
    assert runner.tree.get(run_id).status == "failed"
    assert runner.tree.provider_health()["p"]["consecutive_failures"] == 1
    assert runner.tree.cooldown("p") is None
    result = json.loads((runner.paths.run_dir(run_id) / "result.json").read_text())
    assert result["status"] == "failed" and "cause" not in result


def test_tb_r4_a_model_capacity_refusal_resumes_the_same_session(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch, cooldown=0.1)
    try:
        # Change this test's generated CLI signal, keeping its real session
        # transport and the same scheduler path as the HTTP-429 contract.
        script = world.rl.dir / "agent.py"
        script.write_text(script.read_text().replace("HTTP 429 Too Many Requests",
                                                    "Selected model is at capacity"))
        world.rl.queue(rl(), {})
        world.start_scheduler()
        node = world.simple("capacity", "rlagent")
        done = world.wait_state(node, "done", LONG_WAIT)
        first, resumed = world.rl.calls()
        assert resumed["resume"] == first["session"]
        assert done["outcome"] == "completed"
        assert done["runs"][0]["cause"] == "rate_limited"
        assert not world.tree_json().get("provider_health", {}).get("rl", {}).get("consecutive_failures")
    finally:
        world.close()


@pytest.mark.parametrize("interruptions", [1, 2])
def test_tb_r4_usage_counts_each_interrupted_turn_and_resume_once(tmp_path, monkeypatch, interruptions):
    world = make_world(tmp_path, monkeypatch, cooldown=0.1)
    try:
        # The shared fixture omits opencode's canonical total; supply it in
        # this test's generated CLI to check the aggregate token count too.
        script = world.rl.dir / "agent.py"
        script.write_text(script.read_text().replace(
            "def emit(kind, part):",
            'def emit(kind, part):\n'
            '    if "tokens" in part:\n'
            '        tokens = part["tokens"]\n'
            '        tokens["total"] = tokens["input"] + tokens["output"]'))
        world.rl.queue(*([rl()] * interruptions), {})
        world.start_scheduler()
        node = world.simple("usage", "rlagent")
        done = world.wait_state(node, "done", LONG_WAIT)
        assert done["outcome"] == "completed"
        assert world.rl.spawns() == interruptions + 1
        run_id = done["runs"][0]["run_id"]
        usage = world.tree_nodes()[run_id]["usage"]
        assert usage["input"] == 111 * interruptions + 1
        assert usage["output"] == 222 * interruptions + 1
        assert usage["total"] == 333 * interruptions + 2
        # result.json is turn-local; Tree.update receives the complete total.
        result = json.loads((world.paths.run_dir(run_id) / "result.json").read_text())
        assert result["usage"]["input"] == 1 and result["usage"]["output"] == 1
        assert result["usage"]["total"] == 2
    finally:
        world.close()


@pytest.fixture
def pending(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    node = harness.record()
    harness.save(node)
    attempt, run = harness.launch(node["id"])
    harness.engine.runner.tree.update(run.id, status="rate_limited", session_id="old-session",
                                      turn_started_at=10)
    node = harness.nodes()[node["id"]]
    node["pending_resume"] = {"at": 0, "attempt": 1}
    attempt.update(state="suspended", rate_limit_pending=True, launch_in_progress=True,
                   rate_limit_resume={"previous_turn": 10, "message": "continue"})
    with harness.service.store.transaction() as db:
        harness.service.store.save_node(db, node)
        save_attempt(db, attempt)
    yield harness, attempt
    harness.close()


@pytest.mark.parametrize("exception", [KeyError("broken key"), TypeError("broken call"),
                                       RuntimeError("programming error")])
def test_tb_r4_unexpected_resume_errors_propagate_and_leave_the_journal_pending(pending, monkeypatch, exception):
    harness, attempt = pending

    async def broken(*args, **kwargs):
        raise exception

    monkeypatch.setattr(harness.engine.runner, "_steer", broken)
    with pytest.raises(type(exception), match=str(exception)):
        asyncio.run(rate_limits.command(harness.service.store, harness.engine.runner, attempt))
    current = harness.journal()[attempt["attempt_id"]]
    assert current["state"] == "suspended"
    assert current["rate_limit_resume"] == attempt["rate_limit_resume"]
    assert "resume_refusal" not in current
    assert harness.nodes()[attempt["node_id"]]["pending_resume"] == {"at": 0, "attempt": 1}


@pytest.mark.parametrize("error", ["session_unavailable", "no_session"])
def test_tb_r4_expected_session_loss_relaunches_fresh(pending, monkeypatch, error):
    harness, attempt = pending
    runner = harness.engine.runner

    async def refused(*args, **kwargs):
        return {"steered": False, "error": error}

    async def dead(*args):
        return True

    monkeypatch.setattr(runner, "_steer", refused)
    monkeypatch.setattr(runner, "_steer_predecessor_dead", dead)
    assert asyncio.run(rate_limits.command(harness.service.store, runner, attempt))
    node = harness.nodes()[attempt["node_id"]]
    assert node["state"] == "open" and node.get("outcome") is None
    assert "pending_resume" not in node
    assert node["rate_limit_fresh_from"]["run_id"] == attempt["run_id"]


def test_tb_r4_resume_cli_lists_a_rate_limited_branch(tmp_path, monkeypatch, capsys):
    provider = h.fake_cli(tmp_path, "p", events=[{"type": "text", "text": "done"}])
    runner = h.make_runner(tmp_path / "project", monkeypatch,
                           providers={"p": provider},
                           agents={"worker": h.AgentSpec("worker", "p", "m", writes=True)})
    run_id = run_to_end(runner)
    runner.tree.set_status(run_id, "rate_limited", "rate_limited")
    node = runner.tree.get(run_id)
    assert node.branch
    assert cli.cmd_resume(argparse.Namespace(path=str(runner.paths.root), no_launch=True,
                                            resume=True, wait=False, unattended=0,
                                            team="", supervise=True)) == 0
    output = capsys.readouterr().out
    assert "branch(es) still held by unfinished agents" in output
    assert run_id in output and node.branch in output


def test_tb_r4_rate_limited_conversation_retains_its_session(tmp_path, monkeypatch):
    runner = h.make_runner(tmp_path / "project", monkeypatch)
    runner.tree.add(h.Node(id="ag-abcdef", agent="advisor", provider="p", model="m",
                           parent=None, depth=1, status="rate_limited", conversation=True,
                           session_id="interrupted-session"))
    found = runner._find_conversation("advisor")
    assert found is not None and found.session_id == "interrupted-session"
