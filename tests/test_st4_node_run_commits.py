"""ST4-R1/R2: run-local commit reports through MCP and real scheduler runs.

Contract: stabilization.md at 6d9f23de. Only public RPC/tool results and
host Git are observed. The provider CLI is a real fixture subprocess.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m4_world import M4World, commit_entry, verdict_entry  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT_BOUND = 8
LOOP_BOUND = 16  # four serial fixture activations and Git capture


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.max_wait = LOOP_BOUND
    # The tester is allowed to commit tests, while its CLI permission profile
    # is readonly. ST4 applies to every kind and permission tier.
    world.agent("tester", "fx", writes=True, permission="readonly")
    world.agent("coder", "fx", writes=True)
    yield world
    world.close()


def report(w, run_id, surface):
    if surface == "collect_agent":
        result = call_tool(w, surface, run_id)
        assert not result.get("error"), result
        return result
    reply = call_tool(w, "wait_for_agents", [run_id], timeout=0)
    assert not reply.get("error"), reply
    entries = reply.get("changed", []) + reply.get("already_finished", [])
    found = [entry for entry in entries if entry.get("agent_id") == run_id]
    assert found, reply
    return found[0]


@pytest.mark.parametrize("surface", ["collect_agent", "wait_for_agents"])
def test_st4_r1_tester_commit_is_reported_on_the_node_run(w, surface):
    w.start_scheduler(timeout=WAIT_BOUND)
    node = w.simple("tester", "tester", fx=commit_entry("tests/contract.txt", "one\n"))
    done = w.wait_state(node, "done", timeout=WAIT_BOUND)
    run_id = done["runs"][0]["run_id"]
    result = report(w, run_id, surface)
    assert result.get("commits") == 1, result
    assert not result.get("no_commits"), result
    assert "no_commits_note" not in result, result
    assert "tests/contract.txt" in result.get("diff_stat", ""), result
    assert not result.get("commits_pending"), result


@pytest.mark.parametrize("surface", ["collect_agent", "wait_for_agents"])
@pytest.mark.parametrize("agent", ["tester", "coder"])
def test_st4_r2_a_recorded_empty_range_reports_no_commits(w, surface, agent):
    w.start_scheduler(timeout=WAIT_BOUND)
    node = w.simple("empty", agent)
    done = w.wait_state(node, "done", timeout=WAIT_BOUND)
    result = report(w, done["runs"][0]["run_id"], surface)
    assert result.get("no_commits") is True, result
    assert result.get("commits") == 0, result
    assert not result.get("diff_stat", "").strip(), result
    assert not result.get("commits_pending"), result


@pytest.mark.parametrize("surface", ["collect_agent", "wait_for_agents"])
def test_st4_r2_finished_run_before_generation_capture_is_pending(w, surface):
    w.start_scheduler(timeout=WAIT_BOUND)
    node = w.simple("pending", "tester", fx=commit_entry(
        "tests/pending.txt", "one\n", gate="finish"))
    running = w.wait_running(node, timeout=WAIT_BOUND)
    run_id = running["runs"][-1]["run_id"]
    assert not any(g.get("run_id") == run_id for g in running["generations"]), running
    w.wait_spawn("pending", timeout=WAIT_BOUND)
    # NC-R57 keeps the worker alive across scheduler shutdown. Finish with
    # no scheduler present to publish its generation: a deterministic public
    # lifecycle boundary, without patching result capture or its database.
    w.stop_scheduler(timeout=WAIT_BOUND)
    w.gate("finish")
    w.until(lambda: call_tool(w, "check_agent", run_id).get("status") == "done",
            timeout=WAIT_BOUND, what="the worker to finish before generation capture")
    result = report(w, run_id, surface)
    w.start_scheduler(timeout=WAIT_BOUND)
    captured = w.wait_state(node, "done", timeout=WAIT_BOUND)
    assert any(g.get("run_id") == run_id for g in captured["generations"]), captured
    after = report(w, run_id, surface)
    assert result.get("commits_pending") is True, result
    assert not result.get("no_commits"), result
    assert "no_commits_note" not in result, result
    assert after.get("commits") == 1, after
    assert not after.get("commits_pending"), after
    assert not after.get("no_commits"), after
    assert "tests/pending.txt" in after.get("diff_stat", ""), after


@pytest.mark.parametrize("surface", ["collect_agent", "wait_for_agents"])
def test_st4_r2_a_later_loop_run_does_not_change_an_earlier_range(w, surface):
    worker = w.provider("fx-worker")
    reviewer = w.provider("fx-reviewer")
    w.agent("loop-worker", "fx-worker", writes=True)
    w.agent("loop-reviewer", "fx-reviewer", writes=False, can_spawn=True)
    worker.queue(commit_entry("first.txt", "first\n"),
                 commit_entry("later.txt", "later\n", gate="second"))
    reviewer.queue(verdict_entry("rejected"), verdict_entry("approved"))
    w.start_scheduler(timeout=WAIT_BOUND)
    loop, node, _ = w.mkloop(2, worker="loop-worker", reviewer="loop-reviewer")
    w.until(lambda: len(w.get(node)["runs"]) == 2, timeout=LOOP_BOUND,
            what="the second loop activation")
    first_id = w.get(node)["runs"][0]["run_id"]
    first = report(w, first_id, surface)
    w.gate("second", fx=worker)
    w.wait_state(loop, "done", timeout=LOOP_BOUND)
    after = report(w, first_id, surface)
    assert first.get("commits") == 1, first
    assert after.get("commits") == 1, after
    assert after.get("diff_stat") == first.get("diff_stat"), (first, after)
    assert "first.txt" in after.get("diff_stat", ""), after
    assert "later.txt" not in after.get("diff_stat", ""), after
    assert not after.get("no_commits"), after
