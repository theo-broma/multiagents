"""RM-R2/R3 over authenticated scheduler RPC and the public MCP tools.

Each provider is a real subprocess, blocked by an owned file gate while its
capability is used. No fixture code or production surface is stubbed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nc_fixture.m4_world import M4World, finding
from nc_fixture.agent import task
from nc_fixture.world import call_tool
from test_rm_review_start import git, values

WAIT_BOUND = 4.0


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.05)
    world.max_wait = WAIT_BOUND
    world.agent("worker", "fx", writes=True)
    world.agent("reviewer", "fx", writes=True)
    world.start_scheduler()
    yield world
    world.close()


def launch_review(w, *, text="finished", exit=0, verifies=True):
    target = w.simple("TARGET", "worker", fx={"write": {"reviewed.txt": "v1\n"}, "commit": "target"})
    done = w.wait_state(target, "done", timeout=WAIT_BOUND)
    rid = done["runs"][-1]["run_id"]
    tip = git(w.root, "rev-parse", "refs/heads/nodes/" + target)
    args = {"agent": "reviewer", "task": task("REVIEW", gate="review", text=text, exit=exit)}
    if verifies:
        args["verifies"] = rid
    review_node = w.ok("start_agent", args)["id"]
    call = w.wait_spawn("REVIEW", timeout=WAIT_BOUND)
    token = call["mcp_env"].get("MULTIAGENTS_RPC_TOKEN") or call["env_token"]
    review_run = w.until(lambda: w.get(review_node).get("runs"), WAIT_BOUND,
                         what="review run recorded")[-1]["run_id"]
    return target, rid, tip, review_node, review_run, token, call


def verdict(w, token, value="approved", findings=None, **extra):
    return w.rpc("give_verdict", {"verdict": value, "findings": [] if findings is None else findings, **extra}, token=token)


def assert_structured_verdict(payload, verdict, rid, tip):
    # The schema is deliberately unspecified. Flat or nested JSON fields both
    # qualify; transcript prose and stream events do not count as a record.
    structured = {k: v for k, v in payload.items() if k not in {"result", "text", "events"}}
    assert {verdict, rid, tip} <= {v for v in values(structured) if isinstance(v, str)}, payload


def results(w, run):
    return [call_tool(w, "collect_agent", run), call_tool(w, "check_agent", run)]


def test_rm_r2_a_managed_node_run_is_reviewed_at_its_captured_commit(w):
    _, rid, tip, _, run, _, call = launch_review(w)
    assert git(Path(call["cwd"]), "rev-parse", "HEAD") == tip
    launch_record = json.loads(w.paths.tree_file.read_text())["nodes"][run]
    assert tip in set(values(launch_record)), launch_record
    assert rid in set(values(launch_record)), launch_record


@pytest.mark.parametrize("value, findings", [("approved", []), ("rejected", [finding("wrong output")])])
def test_rm_r3_standalone_tool_verdict_is_structured_and_advisory(w, value, findings):
    target, rid, tip, node, run, token, _ = launch_review(w)
    before = w.get(target)
    base_before = git(w.root, "rev-parse", "HEAD")
    reply = verdict(w, token, value, findings)
    assert reply.get("ok"), reply
    after = w.get(target)
    assert (after["state"], after["outcome"], after["revision"]) == (
        before["state"], before["outcome"], before["revision"])
    assert w.get(node)["state"] == "running", "advisory verdict must not close its node"
    assert git(w.root, "rev-parse", "HEAD") == base_before, "advisory verdict must not merge"
    w.gate("review")
    w.wait_state(node, "done", timeout=WAIT_BOUND)
    for payload in results(w, run):
        assert_structured_verdict(payload, value, rid, tip)


def test_rm_r3_identical_calls_are_idempotent_and_conflicting_calls_preserve_first(w):
    _, rid, tip, _, run, token, _ = launch_review(w)
    first = verdict(w, token)
    assert first.get("ok"), first
    again = verdict(w, token)
    assert again.get("ok"), again
    conflict = verdict(w, token, "rejected", [finding("later disagreement")])
    assert not conflict.get("ok") and "conflict" in json.dumps(conflict).lower(), conflict
    for payload in results(w, run):
        assert_structured_verdict(payload, "approved", rid, tip)


@pytest.mark.parametrize("text, tool", [
    ("VERDICT(rejected, 1): written review", None),
    ("VERDICT(rejected, 1): contradicts the tool", "approved"),
])
def test_rm_r3_text_is_fallback_only_and_tool_disagreement_is_noted(w, text, tool):
    _, rid, tip, node, run, token, _ = launch_review(w, text=text)
    if tool:
        reply = verdict(w, token, tool)
        assert reply.get("ok"), reply
    w.gate("review")
    w.wait_state(node, "done", timeout=WAIT_BOUND)
    for payload in results(w, run):
        assert_structured_verdict(payload, tool or "rejected", rid, tip)
    if tool:
        collected = call_tool(w, "collect_agent", run)
        saved = json.loads((Path(collected["log_dir"]) / "result.json").read_text())
        notes = {k: v for k, v in {**saved, **collected}.items()
                 if k not in {"result", "text", "final_text", "events", "stderr_tail", "summary"}}
        assert "disagree" in json.dumps(notes).lower() or "contradict" in json.dumps(notes).lower(), collected


def test_rm_r3_a_recorded_tool_verdict_survives_reviewer_failure(w):
    _, rid, tip, node, run, token, _ = launch_review(w, exit=1)
    reply = verdict(w, token, "rejected", [finding("defect")])
    assert reply.get("ok"), reply
    w.gate("review")
    w.until(lambda: w.get(node)["state"] != "running", WAIT_BOUND, what="failed reviewer exits")
    for payload in results(w, run):
        assert payload["status"] != "done", payload
        assert_structured_verdict(payload, "rejected", rid, tip)


@pytest.mark.parametrize("value, findings", [
    ("maybe", []), ("", []), ("APPROVED", []),
    ("rejected", [{"summary": "defect", "severity": 17}]),
    ("rejected", [{"severity": "major"}]),
    ("rejected", {"summary": "wrong container", "severity": "major"}),
    ("rejected", ["not a record"]),
])
def test_rm_r3_invalid_verdicts_and_findings_are_refused_without_consuming_the_call(w, value, findings):
    _, _, _, _, _, token, _ = launch_review(w)
    bad = verdict(w, token, value, findings)
    assert not bad.get("ok"), bad
    assert "invalid" in json.dumps(bad).lower(), bad
    good = verdict(w, token)
    assert good.get("ok"), good


@pytest.mark.parametrize("redirect", [{"node_id": "nd-12345678"}, {"commit": "0" * 40},
                                      {"generation_seq": 999}, {"verified_run": "ag-other"}])
def test_rm_r3_caller_cannot_redirect_host_recorded_verdict_identity(w, redirect):
    _, rid, tip, _, run, token, _ = launch_review(w)
    reply = verdict(w, token, **redirect)
    # The contract does not prescribe whether extra identity is rejected or
    # ignored. If accepted, the verdict must still use host launch identity.
    if reply.get("ok"):
        for payload in results(w, run):
            assert_structured_verdict(payload, "approved", rid, tip)
    else:
        good = verdict(w, token)
        assert good.get("ok"), good


def test_rm_r3_run_without_verifies_or_loop_designation_is_refused(w):
    _, _, _, _, _, token, _ = launch_review(w, verifies=False)
    reply = verdict(w, token)
    assert not reply.get("ok") and "forbidden" in json.dumps(reply).lower(), reply


def test_rm_r3_standalone_mcp_tool_accepts_only_verdict_and_findings(w, monkeypatch):
    _, _, _, _, run, token, _ = launch_review(w)
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", run)
    monkeypatch.setenv("MULTIAGENTS_RPC_TOKEN", token)
    try:
        reply = call_tool(w, "give_verdict", verdict="approved", findings=[])
    except TypeError as exc:
        pytest.fail(f"standalone give_verdict must accept only verdict and findings: {exc}")
    assert not reply.get("error"), reply
