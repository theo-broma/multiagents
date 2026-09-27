"""CI-R2 gap — a failed `git add` is a failed commit, never "nothing to commit".

Contract: context/specs/commit-identity.md, CI-R2 plus "Decisions, 2026-09-27
(orchestrator, after tester ag-c057bd on CI-R5)", item "CI-R2 gap (new)".

The case: `index.lock` is held and the work is unstaged. `git add -A` cannot
take the lock, so nothing gets staged; an index that is then compared with
HEAD shows no difference. Reporting that as a clean tree loses the agent's
work silently, which is exactly what CI-R2 forbids.

For a linked worktree the index, and so its lock, lives in the worktree's own
gitdir (`git rev-parse --absolute-git-dir`), not in the main repository's.

Black box: `gitops.commit_all`'s result, and a real Runner run's result.json and
event log, as in `test_commit_identity.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from multiagents import gitops
from test_commit_identity import (  # noqa: E402
    _run_to_end, _runner, commit_count, git, make_repo)


def _identity(monkeypatch) -> None:
    """A configured identity, so the only thing wrong is the lock."""
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "t")
        monkeypatch.setenv(f"{var}_EMAIL", "t@example.invalid")


def _hold_index_lock(worktree: Path) -> Path:
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())
    lock = git_dir / "index.lock"
    lock.write_text("")
    return lock


# --------------------------------------------------------------------------
# Controls — the fixture really produces a failing `git add`


def test_control_git_add_fails_while_the_worktrees_index_lock_is_held(tmp_path, monkeypatch):
    _identity(monkeypatch)
    repo = make_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "agents/worker/abc123")
    (wt / "work.txt").write_text("x")
    lock = _hold_index_lock(wt)
    assert lock.parent != repo / ".git", "a linked worktree has its own gitdir"

    add = git(wt, "add", "-A", check=False)
    assert add.returncode != 0 and "index.lock" in add.stderr, add
    # And the staged diff is empty, which is what a naive commit_all mistakes
    # for a clean tree.
    assert git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0


# --------------------------------------------------------------------------
# gitops level


def test_ci_r2_commit_all_with_a_failing_git_add_is_not_ok(tmp_path, monkeypatch):
    _identity(monkeypatch)
    repo = make_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "agents/worker/abc123")
    before = commit_count(wt)
    (wt / "work.txt").write_text("agent output\n")
    _hold_index_lock(wt)

    try:
        result = gitops.commit_all(wt, "wip")
    except Exception as exc:                        # the raising form is allowed
        reported = str(exc)
    else:
        assert not result.ok, (
            "a failed `git add -A` was reported as success "
            f"({result.out!r}); the work is unstaged and never committed")
        reported = f"{result.err}\n{result.out}"
    assert "index.lock" in reported, f"git's stderr must reach the caller: {reported!r}"
    assert commit_count(wt) == before


# --------------------------------------------------------------------------
# runner level: the run that owns the end-of-run commit


def test_ci_r2_a_run_whose_git_add_fails_reports_commit_failed(tmp_path, monkeypatch):
    _identity(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    # The agent leaves unstaged work and a held lock in its worktree's gitdir.
    runner = _runner(project, (
        "echo done > work.txt; "
        'touch "$(git rev-parse --absolute-git-dir)/index.lock"; '
        "echo CI_R2_ADD_ANSWER all finished"))

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert (Path(node.worktree) / "work.txt").exists(), "fixture: the agent wrote its work"
    shown = git(project, "show", f"{node.branch}:work.txt", check=False)
    assert shown.returncode != 0, "fixture: the work cannot have reached the branch"

    events = [json.loads(line) for line in
              runner.tree.events_path.read_text().splitlines() if line.strip()]
    failed = [e for e in events
              if e.get("agent") == agent_id and e.get("kind") == "commit_failed"]
    assert failed, (
        "a failed `git add` must be recorded as commit_failed; events were "
        f"{[e.get('kind') for e in events if e.get('agent') == agent_id]}")
    assert any("index.lock" in json.dumps(e) for e in failed), failed

    result = json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())
    text = result.get("text", "")
    assert "CI_R2_ADD_ANSWER" in text, "the agent's answer must be kept"
    assert "index.lock" in text, f"the result text must name the failure: {text!r}"
    # CI-R2 decision: a failed commit does not by itself change the status.
    assert node.status == "done", (node.status, node.reason)
