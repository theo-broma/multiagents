"""RM-R2: exact review inputs and fail-closed start_agent refusals.

The spec does not name the reviewed-commit field. Search structured launch
records for the exact value instead of prescribing an undocumented spelling.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from c3_harness import AgentSpec, fake_cli, make_runner
from multiagents import server
from multiagents.tree import Node
from test_rm_markers import WAIT_BOUND, finished


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from values(value)
    else:
        yield obj


@pytest.fixture
def review(tmp_path, monkeypatch):
    p = fake_cli(tmp_path, events=[{"type": "text", "text": "finished"}])
    worker = fake_cli(tmp_path, name="worker", events=[{"type": "text", "text": "worker ready"}])
    script = Path(worker["bin"])
    gate = tmp_path / "release-worker"
    script.write_text(script.read_text().replace("sys.exit(0)",
                      "from pathlib import Path\nwhile not Path(" + repr(str(gate)) +
                      ").exists():\n    time.sleep(0.01)"))
    r = make_runner(tmp_path, monkeypatch,
                    agents={"worker": AgentSpec("worker", "worker", "m", writes=True),
                            "reviewer": AgentSpec("reviewer", "p", "m", writes=True)},
                    providers={"p": p, "worker": worker}, project={"limits": {"allow_workdir_override": True}})
    monkeypatch.setattr(server, "runner", lambda: r)
    return r


async def target(r, *, running=False):
    rid = (await server.start_agent("worker", "work"))["agent_id"]
    if running:
        deadline = asyncio.get_running_loop().time() + WAIT_BOUND
        while not r.check(rid)["total_events"]:
            assert asyncio.get_running_loop().time() < deadline, r.check(rid)
            await asyncio.sleep(0.01)
        assert r.check(rid)["status"] == "running"
    else:
        (r.paths.root / "release-worker").touch()
        await finished(r, rid)
    root = Path(r.tree.get(rid).worktree)
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-m", "distinct reviewed tip")
    return rid, root, git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("running", [False, True], ids=["finished-target", "running-target"])
def test_rm_r2_review_starts_at_exact_target_tip_and_records_it(review, running):
    async def scenario():
        rid, root, tip = await target(review, running=running)
        out = await server.start_agent("reviewer", "review", verifies=rid)
        assert not out.get("error"), out
        bid = out["agent_id"]
        try:
            record = review.tree.read()["nodes"][bid]
            assert git(Path(record["worktree"]), "rev-parse", "HEAD") == tip
            assert tip in set(values(record)), "launch record must identify the reviewed commit"
            assert record["branch"] != review.tree.get(rid).branch
            git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-m", "later target work")
            assert git(Path(record["worktree"]), "rev-parse", "HEAD") == tip
            await finished(review, bid)
            target_tip = git(root, "rev-parse", "HEAD")
            review_root = Path(record["worktree"])
            git(review_root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-m", "reviewer note on own branch")
            assert git(review_root, "rev-parse", "HEAD^") == tip
            assert git(root, "rev-parse", "HEAD") == target_tip
            assert tip in set(values(review.tree.read()["nodes"][bid]))
        finally:
            if review.check(bid)["status"] in {"running", "pending"}:
                await review.stop(bid)
            (review.paths.root / "release-worker").touch()
            await finished(review, rid)
    asyncio.run(scenario())


@pytest.mark.parametrize("cause, words", [
    ("unknown", ("unknown", "not found")),
    ("not-visible", ("visible", "forbidden", "scope", "descendant")),
    ("disposed", ("disposed", "discarded")),
    ("no-tip", ("branch", "commit")),
    ("unresolvable", ("resolve", "tip", "revision", "ref", "commit")),
    ("workdir", ("workdir",)),
    ("node-id", ("run",)),
])
def test_rm_r2_start_refuses_each_invalid_review_target(review, monkeypatch, tmp_path, cause, words):
    async def scenario():
        rid, root, _ = await target(review)
        kwargs = {"verifies": rid}
        if cause == "unknown":
            kwargs["verifies"] = "ag-unknown"
        elif cause == "node-id":
            kwargs["verifies"] = "nd-12345678"
        elif cause == "disposed":
            discarded = review.discard_agent(rid, force=True)
            assert discarded.get("discarded"), discarded
        elif cause == "no-tip":
            review.tree.add(Node(id="ag-no-tip", agent="worker", provider="p", model="m",
                                 parent=None, depth=1, status="done"))
            kwargs["verifies"] = "ag-no-tip"
        elif cause == "unresolvable":
            git(root, "update-ref", "-d", "refs/heads/" + review.tree.get(rid).branch)
        elif cause == "workdir":
            kwargs["workdir"] = str(tmp_path)
        elif cause == "not-visible":
            review.tree.add(Node(id="ag-other", agent="worker", provider="p", model="m",
                                 parent=None, depth=1, status="running"))
            monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-other")
            monkeypatch.setenv("MULTIAGENTS_CAN_SPAWN", "1")
        out = await server.start_agent("reviewer", "review", **kwargs)
        if out.get("agent_id"):
            await finished(review, out["agent_id"])
        assert out.get("error"), f"{cause} must refuse instead of launching: {out}"
        assert any(word in json.dumps(out["error"]).lower() for word in words), out
        if cause == "node-id":
            assert "nd-12345678" in json.dumps(out) and "run" in json.dumps(out).lower(), out
    asyncio.run(scenario())


def test_rm_r2_start_without_verifies_keeps_base_branch(review):
    async def scenario():
        _, _, tip = await target(review)
        base = git(review.paths.root, "rev-parse", "HEAD")
        out = await server.start_agent("reviewer", "ordinary run")
        assert not out.get("error"), out
        bid = out["agent_id"]
        assert git(Path(review.tree.get(bid).worktree), "rev-parse", "HEAD") == base != tip
        await finished(review, bid)
    asyncio.run(scenario())


def test_rm_r3_standalone_tool_also_works_without_the_scheduler(review, monkeypatch):
    async def scenario():
        rid, _, tip = await target(review)
        out = await server.start_agent("reviewer", "review", verifies=rid)
        assert not out.get("error"), out
        bid = out["agent_id"]
        try:
            monkeypatch.setenv("MULTIAGENTS_AGENT_ID", bid)
            try:
                reply = server.give_verdict(verdict="approved", findings=[])
            except TypeError as exc:
                pytest.fail(f"standalone give_verdict must accept only verdict and findings: {exc}")
            assert not reply.get("error"), reply
            monkeypatch.delenv("MULTIAGENTS_AGENT_ID")
            await finished(review, bid)
            from test_rm_standalone import assert_structured_verdict
            for payload in (server.collect_agent(bid), server.check_agent(bid)):
                assert_structured_verdict(payload, "approved", rid, tip)
        finally:
            monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)
            if review.check(bid)["status"] in {"running", "pending"}:
                await review.stop(bid)
    asyncio.run(scenario())
