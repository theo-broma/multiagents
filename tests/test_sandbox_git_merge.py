"""SG-R5: a host-side merge runs the base's hooks, never the branch's.

context/specs/sandbox-git.md, SG-R5 plus its Decisions entry "SG-R5, hook
directory as a whole". When `core.hooksPath` points inside the working tree,
`gitops.merge` still runs the user's commit hooks on the merge commit, but
from the whole hooks directory as it is at the base's HEAD before the merge:
helpers a hook calls next to itself come from the base too, and a hook that
exists only on the branch being merged is not run.

Black box: every hook appends its own distinct line to a log file outside the
repository, and the log is the oracle. Nothing here depends on how the base's
hooks are obtained.

Which hooks git fires depends on the merge style. A squash merge ends with a
`git commit` (pre-commit, commit-msg). A `--no-ff` merge is a `git merge`
commit, which fires pre-merge-commit and commit-msg but never pre-commit. So
the base carries a `pre-merge-commit` that execs the `pre-commit` next to it,
as git's own sample does. The base's pre-commit entry is therefore expected
under both styles, and a branch-modified pre-commit would show under both.

The `control_*` tests run plain git on the same fixture, and show that the
branch's hooks do fire when nothing intervenes. Without them the SG-R5
assertions could pass vacuously.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from multiagents import gitops


STYLES = ("squash", "no-ff")

BASE_PRE_COMMIT = "base:pre-commit"
BASE_HELPER = "base:helper"
BRANCH_PRE_COMMIT = "branch:pre-commit"
BRANCH_HELPER = "branch:helper"
BRANCH_COMMIT_MSG = "branch:commit-msg"
BRANCH_ENTRIES = (BRANCH_PRE_COMMIT, BRANCH_HELPER, BRANCH_COMMIT_MSG)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def pre_commit(log: Path, tag: str, exit_code: int = 0) -> str:
    return (
        "#!/bin/sh\n"
        f"echo '{tag}' >> '{log}'\n"
        '"$(dirname "$0")/helper"\n'
        f"exit {exit_code}\n"
    )


def helper(log: Path, tag: str) -> str:
    return f"#!/bin/sh\necho '{tag}' >> '{log}'\n"


def log_lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    """No global or system config: a developer's own hooksPath or template
    must not decide what runs."""
    cfg = tmp_path / "empty-global-gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)


def build(tmp_path: Path, *, absolute: bool = False, base_exit: int = 0,
          branch_deletes_pre_commit: bool = False) -> dict:
    """A repo on `main` with in-tree hooks at `.hooks`, and a branch `agent`
    that rewrites them. Hook setup is committed without running any hook."""
    log = tmp_path / "hooks.log"
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "tester")
    git(repo, "config", "user.email", "tester@example.invalid")
    hooks = repo / ".hooks"
    git(repo, "config", "core.hooksPath", str(hooks) if absolute else ".hooks")

    (repo / "app.txt").write_text("base\n")
    write_exec(hooks / "pre-commit", pre_commit(log, BASE_PRE_COMMIT, base_exit))
    write_exec(hooks / "helper", helper(log, BASE_HELPER))
    write_exec(hooks / "pre-merge-commit",
               '#!/bin/sh\nexec "$(dirname "$0")/pre-commit"\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")

    git(repo, "checkout", "-q", "-b", "agent")
    (repo / "app.txt").write_text("agent\n")
    if branch_deletes_pre_commit:
        (hooks / "pre-commit").unlink()
    else:
        write_exec(hooks / "pre-commit", pre_commit(log, BRANCH_PRE_COMMIT, 0))
    write_exec(hooks / "helper", helper(log, BRANCH_HELPER))
    write_exec(hooks / "commit-msg", f"#!/bin/sh\necho '{BRANCH_COMMIT_MSG}' >> '{log}'\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "agent rewrites hooks")
    git(repo, "checkout", "-q", "main")

    assert not log.exists(), "fixture setup must not have run any hook"
    return {"repo": repo, "log": log, "base_head": git(repo, "rev-parse", "HEAD").stdout.strip()}


# --------------------------------------------------------------------------
# Controls: plain git runs the branch's hooks on this fixture.
# --------------------------------------------------------------------------

def test_sg_r5_control_plain_squash_merge_and_commit_runs_the_branch_hooks(tmp_path):
    fx = build(tmp_path)
    git(fx["repo"], "merge", "--squash", "agent")
    git(fx["repo"], "commit", "-m", "plain squash")
    lines = log_lines(fx["log"])
    for entry in BRANCH_ENTRIES:
        assert entry in lines, f"control: plain git should have run {entry}; log={lines}"
    assert BASE_PRE_COMMIT not in lines


def test_sg_r5_control_plain_no_ff_merge_runs_the_branch_hooks(tmp_path):
    fx = build(tmp_path)
    git(fx["repo"], "merge", "--no-ff", "-m", "plain no-ff", "agent")
    lines = log_lines(fx["log"])
    for entry in BRANCH_ENTRIES:
        assert entry in lines, f"control: plain git should have run {entry}; log={lines}"
    assert BASE_PRE_COMMIT not in lines


# --------------------------------------------------------------------------
# SG-R5 through gitops.merge.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("absolute", [False, True], ids=["relative-hookspath", "absolute-hookspath"])
@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_merge_runs_the_base_pre_commit_and_its_base_helper(tmp_path, style, absolute):
    fx = build(tmp_path, absolute=absolute)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status == "merged", detail
    lines = log_lines(fx["log"])
    assert BASE_PRE_COMMIT in lines, f"the base's pre-commit must run; log={lines}"
    assert BASE_HELPER in lines, f"the helper next to the base hook must be the base's; log={lines}"


@pytest.mark.parametrize("absolute", [False, True], ids=["relative-hookspath", "absolute-hookspath"])
@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_merge_runs_none_of_the_branch_hooks_or_helpers(tmp_path, style, absolute):
    fx = build(tmp_path, absolute=absolute)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status == "merged", detail
    lines = log_lines(fx["log"])
    assert BRANCH_PRE_COMMIT not in lines, "the branch's modified pre-commit ran on the host"
    assert BRANCH_HELPER not in lines, "the branch's modified helper ran on the host"
    assert BRANCH_COMMIT_MSG not in lines, "a hook that exists only on the branch ran on the host"


@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_merge_still_merges_the_branch_hook_files_into_the_tree(tmp_path, style):
    """Running the base's hooks is not a licence to drop the branch's changes
    to them: the merged commit carries the branch's `.hooks` content."""
    fx = build(tmp_path)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status == "merged", detail
    repo = fx["repo"]
    assert git(repo, "show", "HEAD:.hooks/pre-commit").stdout == \
        git(repo, "show", "agent:.hooks/pre-commit").stdout
    assert git(repo, "show", "HEAD:.hooks/commit-msg").stdout == \
        git(repo, "show", "agent:.hooks/commit-msg").stdout
    assert git(repo, "show", "HEAD:app.txt").stdout == "agent\n"
    assert not git(repo, "status", "--porcelain").stdout.strip()


@pytest.mark.parametrize("absolute", [False, True], ids=["relative-hookspath", "absolute-hookspath"])
@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_a_refusing_base_hook_still_fails_the_merge(tmp_path, style, absolute):
    """The base's pre-commit exits 1; the branch's version exits 0. Running
    the branch's hook would let the merge through; skipping hooks would too."""
    fx = build(tmp_path, absolute=absolute, base_exit=1)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status != "merged", f"a refusing base hook must stop the merge: {detail}"
    assert git(fx["repo"], "rev-parse", "HEAD").stdout.strip() == fx["base_head"]
    lines = log_lines(fx["log"])
    assert BASE_PRE_COMMIT in lines, f"the refusal must come from the base hook; log={lines}"
    for entry in BRANCH_ENTRIES:
        assert entry not in lines


@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_a_hook_the_branch_deletes_still_runs_from_the_base(tmp_path, style):
    """Deleting a hook on the branch must not switch it off for the merge."""
    fx = build(tmp_path, base_exit=1, branch_deletes_pre_commit=True)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status != "merged", f"the base's refusing pre-commit was skipped: {detail}"
    assert git(fx["repo"], "rev-parse", "HEAD").stdout.strip() == fx["base_head"]
    assert BASE_PRE_COMMIT in log_lines(fx["log"])


@pytest.mark.parametrize("style", STYLES)
def test_sg_r5_after_the_merge_the_working_tree_holds_the_merged_hooks(tmp_path, style):
    """The next ordinary commit on the base, after a merge, runs whatever
    `.hooks` then holds (the merged content). SG-R5 governs only the merge
    commit itself, so after the merge the tree's `.hooks` must be exactly
    the merged content, not the base's copy left behind."""
    fx = build(tmp_path)
    status, detail = gitops.merge(fx["repo"], "agent", "merge agent", style=style)
    assert status == "merged", detail
    hooks = fx["repo"] / ".hooks"
    assert (hooks / "commit-msg").exists()
    assert os.access(hooks / "pre-commit", os.X_OK)
    assert BRANCH_PRE_COMMIT in (hooks / "pre-commit").read_text()
    assert BRANCH_HELPER in (hooks / "helper").read_text()
