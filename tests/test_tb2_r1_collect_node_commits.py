"""TB2-R1: collect_agent reports a node run's commits.

Contract: `context/specs/tooling-batch-2-2026-10.md`. For a run the scheduler
launched, `commits`, `diff_stat` and `no_commits` describe what that run added
to `nodes/<node>`; a node run that added nothing still reports
`no_commits: true`; runs started outside the scheduler are unchanged.

Observed only through the `collect_agent` tool and host git.

Assumptions where the spec is silent (kept loose):
- `commits` is the integer count of commits the run added (as for runs
  outside the scheduler today).
- `diff_stat` is the `git diff --stat` text of those commits; asserted by
  substring (the file names, the "N file(s) changed" line).
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import call_tool  # noqa: E402

WAIT_BOUND = 8.0

COMMIT_TWICE = [
    "echo one > one.txt && git add one.txt && git commit -q -m first",
    "echo two > two.txt && git add two.txt && git commit -q -m second",
]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    until = world.until
    world.until = lambda pred, timeout=30, *a, **kw: until(pred, min(timeout, WAIT_BOUND), *a, **kw)
    yield world
    world.close()


def _collect(w, node_id: str) -> dict:
    done = w.done(node_id)
    out = call_tool(w, "collect_agent", done["runs"][0]["run_id"])
    assert not out.get("error"), out
    return out


def test_tb2_r1_a_node_run_that_commits_reports_its_commit_count(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "hello\n"}, fx={"shell": COMMIT_TWICE})
    out = _collect(w, node)
    assert out.get("commits") == 3, out  # two from the shell, one from `commit`
    assert not out.get("no_commits"), out


def test_tb2_r1_a_single_commit_is_counted_as_one(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "hello\n"})
    out = _collect(w, node)
    assert out.get("commits") == 1, out
    assert not out.get("no_commits"), out
    assert "no_commits_note" not in out, out


def test_tb2_r1_diff_stat_describes_exactly_what_the_run_added(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "hello\n"}, fx={"shell": COMMIT_TWICE})
    out = _collect(w, node)
    stat = out.get("diff_stat") or ""
    for name in ("one.txt", "two.txt", "a.txt"):
        assert name in stat, out
    assert "3 files changed" in stat, out


def test_tb2_r1_a_node_run_that_adds_no_commit_reports_no_commits_true(w):
    w.start_scheduler()
    node = w.coder("A", commit=False)
    out = _collect(w, node)
    assert out.get("no_commits") is True, out
    assert not out.get("commits"), out  # 0
    assert not (out.get("diff_stat") or "").strip(), out


def test_tb2_r1_the_node_branch_is_not_where_commits_are_counted_from_main(w):
    """The run's commits are counted against the node's own input commit, not
    against whatever main happens to be: a commit landing on main while the run
    works must not appear in, or be subtracted from, the run's count."""
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a\n"}, fx={"gate": "ga"})
    w.wait_running(node)
    w.commit_on_main("m.txt", "m\n")
    w.gate("ga")
    out = _collect(w, node)
    assert out.get("commits") == 1, out
    assert "m.txt" not in (out.get("diff_stat") or ""), out
    assert "a.txt" in (out.get("diff_stat") or ""), out


def test_tb2_r1_repeated_collect_gives_the_same_report(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a\n"}, fx={"shell": COMMIT_TWICE})
    first = _collect(w, node)
    run_id = w.done(node)["runs"][0]["run_id"]
    second = call_tool(w, "collect_agent", run_id)
    assert first.get("commits") == 3, first
    assert second.get("commits") == first.get("commits")
    assert second.get("diff_stat") == first.get("diff_stat")
    assert bool(second.get("no_commits")) == bool(first.get("no_commits"))


def test_tb2_r1_a_node_run_never_reports_commits_it_did_not_add(w):
    """Two independent nodes: each run's report covers its own branch only."""
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"})
    b = w.coder("B", {"b.txt": "b\n"}, fx={"shell": COMMIT_TWICE})
    out_a, out_b = _collect(w, a), _collect(w, b)
    assert out_a["commits"] == 1 and "b.txt" not in out_a["diff_stat"], out_a
    assert out_b["commits"] == 3 and "a.txt" not in out_b["diff_stat"].replace("b.txt", ""), out_b


# ---------------------------------------------- runs outside the scheduler

def _standalone(tmp_path, monkeypatch, commits: int):
    from support import c3_harness as h
    block = h.fake_cli(tmp_path, name="p", events=[{"type": "text", "text": "finished"}])
    block["stream"]["rules"] = [
        {"match": {"type": "text"}, "as": "text", "fields": {"text": "text"}},
    ]
    spec = h.AgentSpec("worker", "p", "m", writes=True)
    r = h.make_runner(tmp_path / "project", monkeypatch,
                      agents={"worker": spec}, providers={"p": block})

    async def go():
        started = await r.start("worker", "do the task")
        agent_id = started["agent_id"]
        await asyncio.wait_for(r.runs[agent_id].done.wait(), 30)
        return agent_id
    agent_id = asyncio.run(go())
    wt = Path(r.tree.get(agent_id).worktree)
    for i in range(commits):
        (wt / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=F", "-c", "user.email=f@x.invalid",
                        "commit", "-q", "-m", f"c{i}"], check=True)
    return r, agent_id


def test_tb2_r1_a_run_outside_the_scheduler_still_reports_its_commits(tmp_path, monkeypatch):
    r, agent_id = _standalone(tmp_path, monkeypatch, 2)
    out = r.collect(agent_id)
    assert out["commits"] == 2, out
    assert "f0.txt" in out["diff_stat"] and "f1.txt" in out["diff_stat"], out
    assert not out.get("no_commits"), out


def test_tb2_r1_a_run_outside_the_scheduler_with_no_commit_still_flags_it(tmp_path, monkeypatch):
    r, agent_id = _standalone(tmp_path, monkeypatch, 0)
    out = r.collect(agent_id)
    assert out.get("no_commits") is True, out
    assert not out.get("commits"), out
