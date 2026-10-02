"""bug-ba55a9 — the WIP auto-commit must not commit an unmerged index.

Contract ids BA-R1..BA-R3 (task description of ticket bug-ba55a9):

- BA-R1: `gitops.commit_all` on a worktree whose index has unmerged entries
  (`git ls-files -u` non-empty) refuses: a failed result whose error says the
  index has unmerged entries, no commit, and the unmerged state left intact.
- BA-R2: a merge or squash in progress with NO unmerged entries (a clean
  squash staged, not yet committed) is committed normally, and the leftover
  SQUASH_MSG / MERGE_HEAD never makes a later commit be refused.
- BA-R3: when the end-of-run WIP commit is refused for BA-R1, the run records
  a visible event and the node result says the work-in-progress commit was
  refused because of unmerged entries.

Black box: `commit_all`'s result and git's own view of the repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multiagents import gitops
from test_commit_identity import (  # noqa: E402
    _run_to_end, _runner, commit_count, files_under, git, make_repo)

MARKER = "<<<<<<<"


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "t")
        monkeypatch.setenv(f"{var}_EMAIL", "t@example.test")


def _commit(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def _unmerged(repo: Path) -> str:
    return git(repo, "ls-files", "-u").stdout


def _head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _git_path(repo: Path, name: str) -> Path:
    p = Path(git(repo, "rev-parse", "--git-path", name).stdout.strip())
    return p if p.is_absolute() else repo / p


def _setup(tmp_path: Path, linked: bool, kind: str) -> Path:
    """A work tree with `f.txt` committed, a branch `other` that diverges from
    it, and the work tree's own branch diverging the other way.

    kind: "edit" (both edit f.txt), "theirs-deleted" (other deletes it, we edit
    it), "ours-deleted" (we delete it, other edits it).
    Returns the work tree to merge in; `other` is NOT merged yet.
    """
    main = make_repo(tmp_path / "main")
    (main / "f.txt").write_text("base\n")
    (main / "keep.txt").write_text("keep\n")
    _commit(main, "base")
    git(main, "branch", "other")
    if linked:
        wt = tmp_path / "wt"
        git(main, "worktree", "add", "-q", str(wt), "-b", "agents/worker/abc123")
    else:
        wt = main
    if linked:
        other = tmp_path / "other-wt"
        git(main, "worktree", "add", "-q", str(other), "other")
    else:
        other = wt
        git(main, "checkout", "-q", "other")
    if kind == "theirs-deleted":
        (other / "f.txt").unlink()
    else:
        (other / "f.txt").write_text("theirs\n")
    _commit(other, "other side")
    if linked:
        git(main, "worktree", "remove", "--force", str(other))
    else:
        git(main, "checkout", "-q", "-")
    if kind == "ours-deleted":
        (wt / "f.txt").unlink()
    else:
        (wt / "f.txt").write_text("ours\n")
    _commit(wt, "our side")
    return wt


def _merge(wt: Path, how: str) -> None:
    args = ["merge", "--squash", "other"] if how == "squash" else ["merge", "other"]
    proc = git(wt, *args, check=False)
    assert proc.returncode != 0, "fixture: the merge was meant to conflict"
    assert _unmerged(wt), "fixture: the index must have unmerged entries"


CASES = [(linked, how, kind)
         for linked in (False, True)
         for how in ("squash", "merge")
         for kind in ("edit", "theirs-deleted", "ours-deleted")]
IDS = [f"{'linked' if l else 'main'}-{h}-{k}" for l, h, k in CASES]


# --------------------------------------------------------------------------
# BA-R1


@pytest.mark.parametrize("linked,how,kind", CASES, ids=IDS)
def test_ba_r1_refuses_an_unmerged_index_and_leaves_it_untouched(
        tmp_path, linked, how, kind):
    wt = _setup(tmp_path, linked, kind)
    _merge(wt, how)
    (wt / "extra.txt").write_text("unrelated agent work\n")
    head, count = _head(wt), commit_count(wt)
    staged, tree = git(wt, "ls-files", "-s").stdout, files_under(wt)
    unmerged = _unmerged(wt)

    result = gitops.commit_all(wt, "agent: work in progress (x)")

    assert not result.ok, f"an unmerged index was committed: {result.out!r}"
    said = f"{result.err}\n{result.out}".lower()
    assert "unmerged" in said, f"the error must say why: {said!r}"
    assert _head(wt) == head and commit_count(wt) == count, "a commit was created"
    assert _unmerged(wt) == unmerged != "", "the unmerged state was changed"
    assert git(wt, "ls-files", "-s").stdout == staged, "the index was changed"
    assert files_under(wt) == tree, "the files were changed"
    if kind == "edit":
        assert MARKER in (wt / "f.txt").read_text(), "fixture: conflict markers"


def test_ba_r1_a_repeated_call_is_refused_again(tmp_path):
    wt = _setup(tmp_path, True, "edit")
    _merge(wt, "merge")
    head = _head(wt)
    for _ in range(2):
        assert not gitops.commit_all(wt, "wip").ok
    assert _head(wt) == head and _unmerged(wt)


def test_ba_r1_a_conflict_in_one_file_blocks_the_whole_commit(tmp_path):
    """Other files, staged or not, must not be committed around the conflict."""
    wt = _setup(tmp_path, False, "edit")
    (wt / "keep.txt").write_text("edited after base\n")
    _merge(wt, "merge")
    head = _head(wt)
    result = gitops.commit_all(wt, "wip")
    assert not result.ok and _head(wt) == head
    assert "keep.txt" not in git(wt, "diff", "--cached", "--name-only").stdout


@pytest.mark.parametrize("how", ["squash", "merge"])
def test_ba_r1_once_resolved_the_commit_goes_through(tmp_path, how):
    wt = _setup(tmp_path, True, "edit")
    _merge(wt, how)
    assert not gitops.commit_all(wt, "wip").ok
    (wt / "f.txt").write_text("resolved\n")
    git(wt, "add", "f.txt")
    assert _unmerged(wt) == ""

    result = gitops.commit_all(wt, "wip")

    assert result.ok, result
    assert git(wt, "show", "HEAD:f.txt").stdout == "resolved\n"
    assert git(wt, "status", "--porcelain").stdout.strip() == ""


# --------------------------------------------------------------------------
# BA-R2 — in-progress state without conflicts is committed


def _clean_other(tmp_path: Path, linked: bool) -> Path:
    main = make_repo(tmp_path / "main")
    (main / "f.txt").write_text("base\n")
    _commit(main, "base")
    git(main, "checkout", "-q", "-b", "other")
    (main / "new.txt").write_text("from other\n")
    _commit(main, "other adds a file")
    git(main, "checkout", "-q", "-")
    if linked:
        wt = tmp_path / "wt"
        git(main, "worktree", "add", "-q", str(wt), "-b", "agents/checker/abc123")
    else:
        wt = main
    (wt / "mine.txt").write_text("mine\n")
    _commit(wt, "our side")
    return wt


@pytest.mark.parametrize("linked", [False, True], ids=["main", "linked"])
def test_ba_r2_a_clean_squash_is_committed(tmp_path, linked):
    wt = _clean_other(tmp_path, linked)
    git(wt, "merge", "--squash", "other")
    assert _unmerged(wt) == ""
    assert _git_path(wt, "SQUASH_MSG").exists(), "fixture: a squash is in progress"
    count = commit_count(wt)

    result = gitops.commit_all(wt, "agent: work in progress (x)")

    assert result.ok, result
    assert commit_count(wt) == count + 1
    assert git(wt, "show", "HEAD:new.txt").stdout == "from other\n"
    assert git(wt, "status", "--porcelain").stdout.strip() == ""


@pytest.mark.parametrize("linked", [False, True], ids=["main", "linked"])
def test_ba_r2_a_clean_merge_awaiting_its_commit_is_committed(tmp_path, linked):
    wt = _clean_other(tmp_path, linked)
    git(wt, "merge", "--no-commit", "--no-ff", "other")
    assert _git_path(wt, "MERGE_HEAD").exists(), "fixture: a merge is in progress"
    assert _unmerged(wt) == ""

    result = gitops.commit_all(wt, "wip")

    assert result.ok, result
    parents = git(wt, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, "the merge in progress becomes a merge commit"
    assert not _git_path(wt, "MERGE_HEAD").exists()


@pytest.mark.parametrize("how", ["squash", "merge"])
def test_ba_r2_a_later_commit_is_not_refused_after_the_state_commit(tmp_path, how):
    wt = _clean_other(tmp_path, True)
    git(wt, "merge", *(["--squash"] if how == "squash" else ["--no-commit", "--no-ff"]),
        "other")
    assert gitops.commit_all(wt, "first").ok
    count = commit_count(wt)
    (wt / "later.txt").write_text("later\n")

    second = gitops.commit_all(wt, "second")

    assert second.ok, second
    assert commit_count(wt) == count + 1
    assert git(wt, "show", "HEAD:later.txt").stdout == "later\n"


def test_ba_r2_a_stale_squash_msg_without_conflicts_does_not_block(tmp_path):
    wt = _clean_other(tmp_path, True)
    _git_path(wt, "SQUASH_MSG").write_text("Squashed commit of the following:\n")
    (wt / "work.txt").write_text("work\n")
    count = commit_count(wt)

    result = gitops.commit_all(wt, "wip")

    assert result.ok, result
    assert commit_count(wt) == count + 1


def test_ba_r2_a_clean_tree_is_still_a_clean_no_op(tmp_path):
    wt = _clean_other(tmp_path, True)
    count = commit_count(wt)
    result = gitops.commit_all(wt, "wip")
    assert result.ok and commit_count(wt) == count


# --------------------------------------------------------------------------
# BA-R3 — through the Runner

CONFLICT_SCRIPT = (
    "export GIT_AUTHOR_NAME=a GIT_AUTHOR_EMAIL=a@a GIT_COMMITTER_NAME=a "
    "GIT_COMMITTER_EMAIL=a@a; "
    "echo base > f.txt; git add f.txt; git commit -q -m base; "
    "git checkout -q -b side; echo theirs > f.txt; git commit -q -am side; "
    "git checkout -q -; echo ours > f.txt; git commit -q -am ours; "
    "git merge --squash side >/dev/null 2>&1; "
    "echo BA_R3_ANSWER all finished")

CLEAN_SQUASH_SCRIPT = (
    "export GIT_AUTHOR_NAME=a GIT_AUTHOR_EMAIL=a@a GIT_COMMITTER_NAME=a "
    "GIT_COMMITTER_EMAIL=a@a; "
    "echo base > f.txt; git add f.txt; git commit -q -m base; "
    "git checkout -q -b side; echo side > g.txt; git add g.txt; git commit -q -m side; "
    "git checkout -q -; git merge --squash side >/dev/null 2>&1; "
    "echo BA_R3_ANSWER all finished")


def _events(runner, agent_id):
    return [json.loads(line) for line in
            runner.tree.events_path.read_text().splitlines() if line.strip()
            if json.loads(line).get("agent") == agent_id]


def test_ba_r3_a_refused_wip_commit_is_visible_in_events_and_result(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runner = _runner(project, CONFLICT_SCRIPT)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    wt = Path(node.worktree)
    shown = git(project, "show", f"{node.branch}:f.txt", check=False)
    assert MARKER not in shown.stdout, "conflict markers reached the branch history"
    assert git(project, "log", "--format=%s", node.branch).stdout.count(
        "work in progress") == 0, "a WIP commit was created"

    # BA-R3: some event records the refusal, naming the unmerged entries.
    refusals = [e for e in _events(runner, agent_id)
                if "unmerged" in json.dumps(e).lower()]
    assert refusals, ("no event records the refused WIP commit; events were "
                      f"{[e.get('kind') for e in _events(runner, agent_id)]}")

    result = json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())
    text = result.get("text", "")
    assert "BA_R3_ANSWER" in text, "the agent's answer must be kept"
    low = text.lower().replace("-", " ")
    assert "unmerged" in low, f"the result must say why: {text!r}"
    assert "work in progress" in low and "refused" in low, (
        f"the result must say the WIP commit was refused: {text!r}")


def test_ba_r3_a_clean_squash_left_by_the_agent_is_committed_by_the_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runner = _runner(project, CLEAN_SQUASH_SCRIPT)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    shown = git(project, "show", f"{node.branch}:g.txt", check=False)
    assert shown.returncode == 0 and shown.stdout == "side\n", shown.stderr
    assert not [e for e in _events(runner, agent_id)
                if "unmerged" in json.dumps(e).lower()]
    text = json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())["text"]
    assert "refused" not in text.lower()
