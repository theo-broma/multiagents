"""A response-less trailing error must retain a completed provider verdict."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import c3_harness as h


VERDICT = "The review is complete: approved."


def run_results(tmp_path, monkeypatch, results):
    provider = h.fake_cli(tmp_path, events=[{"type": "text", "text": "Review started."}] + [
        {"type": "result", "subtype": status, "result": response,
         "requested_session": "lost-session" if status == "SESSION_LOST" else ""}
        for status, response in results
    ])
    runner = h.make_runner(
        tmp_path / "project", monkeypatch,
        agents={"worker": h.AgentSpec("worker", "fake", "m")},
        providers={"fake": provider},
    )

    async def go():
        started = await runner.start("worker", "go")
        agent = started["agent_id"]
        await asyncio.wait_for(runner.runs[agent].task, timeout=15)
        return agent

    return runner, asyncio.run(go())


@pytest.mark.parametrize("success", ["SUCCESS", "OK", "COMPLETED"])
@pytest.mark.parametrize("response", [None, ""])
def test_trailing_error_retains_verdict(tmp_path, monkeypatch, success, response):
    runner, agent = run_results(tmp_path, monkeypatch, [
        (success, VERDICT), ("ERROR", response),
    ])
    assert runner.tree.get(agent).status == "done"
    assert runner.runs[agent].final_status == success
    assert VERDICT in runner.collect(agent, mode="full")["text"]
    events = [json.loads(line) for line in
              (runner.paths.run_dir(agent) / "stream.jsonl").read_text().splitlines()]
    diagnostic = next(event for event in events if event["status"] == "ERROR")
    assert diagnostic["warning"]
    warnings = [diagnostic["warning"]]
    result = json.loads((runner.paths.run_dir(agent) / "result.json").read_text())
    assert result["warnings"] == warnings
    assert runner.collect(agent, mode="full")["warnings"] == warnings
    assert runner.collect(agent)["warnings"] == warnings
    assert runner.tree.read()["nodes"][agent]["warnings"] == warnings
    assert json.loads(diagnostic["raw"])["subtype"] == "ERROR"


@pytest.mark.parametrize("results", [
    [("ERROR", VERDICT)],
    [("SUCCESS", VERDICT), ("ERROR", "A replacement response")],
    [("SUCCESS", ""), ("ERROR", None)],
    [("ERROR", None)],
    [("SUCCESS", ""), ("ERROR", VERDICT)],
])
def test_errors_without_completed_response_or_with_replacement_fail(
        tmp_path, monkeypatch, results):
    runner, agent = run_results(tmp_path, monkeypatch, results)
    assert runner.tree.get(agent).status == "failed"
    assert runner.runs[agent].final_status == "ERROR"


@pytest.mark.parametrize(("status", "expected"), [
    ("SESSION_LOST", "failed"), ("REFUSED", "refused"), ("TRUNCATED", "truncated"),
])
def test_flow_control_status_after_success_is_preserved(
        tmp_path, monkeypatch, status, expected):
    runner, agent = run_results(tmp_path, monkeypatch, [
        ("SUCCESS", VERDICT), (status, None),
    ])
    node = runner.tree.get(agent)
    assert node.status == expected
    assert runner.runs[agent].final_status == status
    result = json.loads((runner.paths.run_dir(agent) / "result.json").read_text())
    assert result["status"] == expected
    assert not result.get("warnings")
    assert not node.warnings
    if status == "SESSION_LOST":
        assert node.reason == "session_lost"
        assert node.session_id == ""
        assert node.requested_session == "lost-session"
        assert result["requested_session"] == "lost-session"
        assert runner.collect(agent, mode="full")["reason"] == "session_lost"
