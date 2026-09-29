"""H3 coverage: host Git calls that no earlier test pinned (adversary ag-e98308).

Each test drives a public entry point against a real repository and plants a
program that records its own execution by creating a file under `ran/`. The
empty `ran/` directory is the security oracle; repository state (refs, paths,
registrations) confirms the operation itself still happened, so a test cannot
pass merely because the host call failed early.

The mutation each test was checked against is named in its docstring.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from multiagents import cli, gitops
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Node, Tree

from test_h3_host_git import (  # noqa: F401
    agent_branch, git, isolated_git, program, repo, runner,
)


def ran(tmp_path: Path) -> list[str]:
    where = tmp_path / "ran"
    return sorted(p.name for p in where.iterdir()) if where.is_dir() else []


def plant(tmp_path: Path, name: str) -> Path:
    """An executable at `tmp_path/bin/<name>` that marks `ran/<name>`."""
    (tmp_path / "ran").mkdir(exist_ok=True)
    path = tmp_path / "bin" / name
    program(path, tmp_path / "ran" / name)
    return path


def plant_hooks(tmp_path: Path, hooks_dir: Path, *names: str) -> None:
    (tmp_path / "ran").mkdir(exist_ok=True)
    for name in names:
        program(hooks_dir / name, tmp_path / "ran" / name)


def worktree(root: Path, node_id: str, branch: str) -> Path:
    wt = ProjectPaths(root).worktree(node_id)
    gitops.create_worktree(root, wt, branch, base="main", unique=False)
    return wt


def registration(root: Path, wt: Path) -> Path:
    return root / ".git" / "worktrees" / wt.name


def rev(root: Path, ref: str) -> str:
    return git(root, "rev-parse", ref).stdout.strip()


# --------------------------------------------------------------------------
# HG-R4: `process` filters and `textconv` diff drivers are content programs.
# --------------------------------------------------------------------------

def _process_filter_branch(root: Path, tmp_path: Path) -> None:
    """An agent branch whose `.gitattributes` selects a process-only filter."""
    wt = worktree(root, "ag-one", "agents/worker/one")
    (wt / ".gitattributes").write_text("payload.bin filter=probe\n")
    (wt / "payload.bin").write_bytes(b"raw payload\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "agent")
    # Base moves on, so the merge is a real one, not a fast-forward.
    (root / "later.txt").write_text("later\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "later")


@pytest.mark.parametrize("style", ["squash", "no-ff"])
def test_hg_r4_process_only_filter_never_runs_during_merge(tmp_path, style):
    """Kills: dropping `process` from the content programs disabled for host
    calls. The trusted filter has only a `process` command."""
    root = repo(tmp_path)
    _process_filter_branch(root, tmp_path)
    git(root, "config", "filter.probe.process", str(plant(tmp_path, "process-filter")))
    status, detail = gitops.merge(root, "agents/worker/one", "merge", style=style)
    assert status == "merged", detail
    assert (root / "payload.bin").read_bytes() == b"raw payload\n"
    assert ran(tmp_path) == []


def test_hg_r4_process_only_filter_never_runs_during_host_checkout(tmp_path):
    """Kills: dropping `process` from the disabled set (worktree add path)."""
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("payload.bin filter=probe\n")
    (root / "payload.bin").write_bytes(b"raw payload\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "filtered")
    git(root, "config", "filter.probe.process", str(plant(tmp_path, "process-filter")))
    wt = worktree(root, "ag-one", "agents/worker/one")
    assert (wt / "payload.bin").read_bytes() == b"raw payload\n"
    assert ran(tmp_path) == []


@pytest.mark.parametrize("program_key", ["process", "clean", "smudge"])
def test_hg_r4_required_filter_with_disabled_program_neither_aborts_nor_runs(
        tmp_path, program_key):
    """A `filter.<x>.required=true` filter whose program is disabled: the host
    merge must complete (a required filter with no program is otherwise a
    fatal error) and the program must not run.

    Kills: dropping `required` from the overrides (git aborts the checkout).
    NOTE: writing the override as `filter.<x>.required=` (empty) instead of
    `=false` is an equivalent mutant — git parses an empty boolean as false —
    so no black-box test can tell the two apart."""
    root = repo(tmp_path)
    _process_filter_branch(root, tmp_path)
    git(root, "config", f"filter.probe.{program_key}", str(plant(tmp_path, "filter")))
    git(root, "config", "filter.probe.required", "true")
    main_before = rev(root, "main")
    status, detail = gitops.merge(root, "agents/worker/one", "merge", style="no-ff")
    assert status == "merged", detail
    assert rev(root, "main") != main_before
    assert (root / "payload.bin").read_bytes() == b"raw payload\n"
    assert ran(tmp_path) == []


def _textconv_base(root: Path, tmp_path: Path) -> Path:
    """`notes.txt` selects a textconv diff driver in the base tree; returns the
    driver's sentinel-writing program (not yet configured)."""
    (root / ".gitattributes").write_text("notes.txt diff=probe\n")
    (root / "notes.txt").write_text("one\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "notes")
    (tmp_path / "ran").mkdir(exist_ok=True)
    tc = tmp_path / "bin" / "textconv"
    tc.parent.mkdir(parents=True, exist_ok=True)
    tc.write_text(f"#!/bin/sh\n: > '{tmp_path / 'ran' / 'textconv'}'\ncat \"$1\"\n")
    tc.chmod(0o755)
    return tc


def test_hg_r4_textconv_never_runs_during_squash_merge(tmp_path):
    """A squash merge asks git whether anything is staged (`diff --cached
    --quiet`), and git answers that through textconv.

    Kills: dropping `textconv` together with the unconditional
    `diff.external=` override. Dropping `textconv` ALONE is equivalent today:
    git does not run textconv for `diff --quiet` while `diff.external` is set,
    even to empty, and no host call produces patch output."""
    root = repo(tmp_path)
    tc = _textconv_base(root, tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    (wt / "notes.txt").write_text("two\n")
    git(wt, "commit", "-q", "-am", "agent")
    (root / "later.txt").write_text("later\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "later")
    git(root, "config", "diff.probe.textconv", str(tc))
    status, detail = gitops.merge(root, "agents/worker/one", "merge", style="squash")
    assert status == "merged", detail
    assert git(root, "show", "main:notes.txt").stdout == "two\n"
    assert ran(tmp_path) == []


def test_hg_r4_textconv_never_runs_during_host_checkpoint(tmp_path):
    """As above, for the host `commit_all`. A diff driver is not a filter,
    so the checkpoint is not refused."""
    root = repo(tmp_path)
    tc = _textconv_base(root, tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    git(root, "config", "diff.probe.textconv", str(tc))
    (wt / "notes.txt").write_text("two\n")
    result = gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    assert result.ok, result
    assert git(root, "show", "agents/worker/one:notes.txt").stdout == "two\n"
    assert ran(tmp_path) == []


# --------------------------------------------------------------------------
# HG-R2/HG-R3: push, branch -D, worktree remove and worktree move.
# --------------------------------------------------------------------------

def test_hg_r2_push_runs_no_repository_hook(tmp_path):
    """Kills: `push` running outside the host scope (pre-push and
    reference-transaction hooks from `.git/hooks` fire)."""
    root = repo(tmp_path)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    plant_hooks(tmp_path, root / ".git" / "hooks", "pre-push", "reference-transaction")
    result = gitops.push(root, "origin", "main")
    assert result.ok, result
    assert rev(remote, "main") == rev(root, "main")
    assert ran(tmp_path) == []


def test_hg_r2_delete_branch_runs_no_repository_hook(tmp_path):
    """Kills: `delete_branch` running outside the host scope."""
    root = repo(tmp_path)
    git(root, "branch", "agents/worker/gone")
    plant_hooks(tmp_path, root / ".git" / "hooks", "reference-transaction")
    result = gitops.delete_branch(root, "agents/worker/gone", force=True)
    assert result.ok, result
    assert git(root, "rev-parse", "--verify", "-q", "refs/heads/agents/worker/gone",
               check=False).returncode != 0
    assert ran(tmp_path) == []


def test_hg_r2_hg_r3_worktree_remove_runs_no_hook_or_fsmonitor(tmp_path):
    """Kills: `remove_worktree` running outside the host scope. Git checks a
    worktree is clean before removing it, which spawns fsmonitor and the
    post-index-change hook."""
    root = repo(tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    git(root, "config", "core.fsmonitor", str(plant(tmp_path, "fsmonitor")))
    plant_hooks(tmp_path, root / ".git" / "hooks", "post-index-change")
    result = gitops.remove_worktree(root, wt)
    assert result.ok, result
    assert not wt.exists()
    assert ran(tmp_path) == []


def test_hg_r3_worktree_move_runs_no_fsmonitor(tmp_path):
    """Kills: `move_aside` running `worktree move` outside the host scope.
    Git reads the moved worktree's index, which spawns fsmonitor."""
    root = repo(tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    git(root, "config", "core.fsmonitor", str(plant(tmp_path, "fsmonitor")))
    target = gitops.move_aside(root, wt)
    assert not wt.exists() and target.is_dir()
    listed = git(root, "worktree", "list", "--porcelain").stdout
    assert f"worktree {target.resolve()}" in listed
    assert ran(tmp_path) == []


@pytest.mark.parametrize("forgery", ["symlinked-gitdir", "oversized-gitdir"])
def test_hg_r1_move_aside_refuses_an_ambiguous_registration(tmp_path, forgery):
    """Kills: `move_aside` skipping its refusal when the registration cannot be
    matched against the host's reading of it. Git still lists the worktree
    (it follows the link / trims the padding) but the host cannot trust the
    entry, so nothing may move."""
    root = repo(tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    gitdir = registration(root, wt) / "gitdir"
    content = gitdir.read_text()
    if forgery == "symlinked-gitdir":
        elsewhere = tmp_path / "forged-gitdir"
        elsewhere.write_text(content)
        gitdir.unlink()
        gitdir.symlink_to(elsewhere)
    else:
        gitdir.write_text(content.rstrip("\n") + "\n" + " " * 8192 + "\n")
    listed = git(root, "worktree", "list", "--porcelain").stdout
    assert f"worktree {wt.resolve()}" in listed, "precondition: git still sees it"
    with pytest.raises(gitops.GitError, match="host_authority_mismatch"):
        gitops.move_aside(root, wt)
    assert wt.is_dir() and (wt / "base.txt").is_file()
    assert git(root, "worktree", "list", "--porcelain").stdout == listed
    assert not any(p.is_dir() and any(p.iterdir())
                   for p in wt.parent.glob(f"{wt.name}.aside*"))


# --------------------------------------------------------------------------
# HG-R2: host bookkeeping commits never consult hooks; agent commits do.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hook", ["pre-commit", "prepare-commit-msg", "commit-msg",
                                  "post-commit"])
@pytest.mark.parametrize("where", ["dot-git-hooks", "trusted-hookspath"])
def test_hg_r2_host_bookkeeping_commit_runs_no_commit_hook(tmp_path, hook, where):
    """Kills: the host checkpoint (`commit_all` with `root`, no executor)
    running with hooks enabled."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    if where == "dot-git-hooks":
        hooks_dir = root / ".git" / "hooks"
    else:
        hooks_dir = tmp_path / "trusted-hooks"
        git(root, "config", "core.hooksPath", str(hooks_dir))
    plant_hooks(tmp_path, hooks_dir, hook)
    (wt / "pending.txt").write_text("pending\n")
    result = gitops.commit_all(wt, "checkpoint", root=root, branch="agents/worker/one")
    assert result.ok, result
    assert git(root, "show", "agents/worker/one:pending.txt").stdout == "pending\n"
    assert ran(tmp_path) == []


def test_ci_r5_agent_commit_through_its_executor_still_runs_hooks(tmp_path):
    """The other side of HG-R2's boundary: a commit made through the agent's
    executor (here the local one, `gitops.HOST`, with no host root) keeps
    CI-R5's hooks, and a refusing hook is named."""
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    (tmp_path / "ran").mkdir(exist_ok=True)
    program(root / ".git" / "hooks" / "pre-commit", tmp_path / "ran" / "pre-commit",
            exit_code=1)
    before = rev(root, "agents/worker/one")
    (wt / "pending.txt").write_text("pending\n")
    result = gitops.commit_all(wt, "agent commit", git=gitops.HOST)
    assert not result.ok
    assert result.hook == "pre-commit"
    assert ran(tmp_path) == ["pre-commit"]
    assert rev(root, "agents/worker/one") == before


# --------------------------------------------------------------------------
# HG-R8 / H1: merge_agent takes the target branch from trusted state.
# --------------------------------------------------------------------------

def _parent_and_child(tmp_path: Path) -> tuple[Runner, Path, Path, Path]:
    root = repo(tmp_path)
    r = runner(root)
    assert r.authority is not None
    parent = worktree(root, "ag-parent", "agents/parent/one")
    child = worktree(root, "ag-child", "agents/worker/child")
    (child / "child.txt").write_text("child\n")
    git(child, "add", "-A")
    git(child, "commit", "-q", "-m", "child")
    sibling = worktree(root, "ag-sib", "agents/worker/sib")
    for n in (
        Node(id="ag-parent", agent="lead", provider="p", model="m", parent=None,
             depth=1, branch="agents/parent/one", worktree=str(parent),
             status="running", task="lead"),
        Node(id="ag-child", agent="worker", provider="p", model="m", parent="ag-parent",
             depth=2, branch="agents/worker/child", worktree=str(child),
             status="done", task="child work"),
        Node(id="ag-sib", agent="worker", provider="p", model="m", parent="ag-parent",
             depth=2, branch="agents/worker/sib", worktree=str(sibling),
             status="running", task="sibling work"),
    ):
        r.tree.add(n)
        r.authority.add(n)
    return r, root, parent, sibling


def test_merge_agent_into_parent_lands_on_the_recorded_branch(tmp_path):
    """Control for the test below: the honest case merges into the parent's
    recorded branch and nowhere else."""
    r, root, parent, _ = _parent_and_child(tmp_path)
    main_before, sib_before = rev(root, "main"), rev(root, "agents/worker/sib")
    result = r.merge_agent("ag-child", into=str(parent))
    assert result["result"] == "merged", result
    assert git(root, "show", "agents/parent/one:child.txt").stdout == "child\n"
    assert rev(root, "main") == main_before
    assert rev(root, "agents/worker/sib") == sib_before


def test_merge_agent_into_parent_ignores_a_rewritten_worktree_head(tmp_path):
    """Kills: `merge_agent` not looking the target branch up in trusted state.
    The parent agent points its own registration HEAD at a sibling's branch;
    the host must not merge onto the sibling (nor the base)."""
    r, root, parent, _ = _parent_and_child(tmp_path)
    main_before, sib_before = rev(root, "main"), rev(root, "agents/worker/sib")
    parent_before = rev(root, "agents/parent/one")
    (registration(root, parent) / "HEAD").write_text("ref: refs/heads/agents/worker/sib\n")
    result = r.merge_agent("ag-child", into=str(parent))
    assert result["result"] != "merged", result
    assert rev(root, "agents/worker/sib") == sib_before, "merged onto the sibling's branch"
    assert rev(root, "main") == main_before
    assert rev(root, "agents/parent/one") == parent_before


# --------------------------------------------------------------------------
# HG-R9: clean --branches keeps a worktree whose checkpoint failed.
# --------------------------------------------------------------------------

def _stop(root: Path, wt: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(argparse.Namespace(path=str(root), force=False, nested=False)) == 0
    paths = ProjectPaths(root)
    Tree(paths.tree_file, paths.events_file).add(
        Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
             depth=1, status="running", pid=0, branch="agents/worker/one",
             worktree=str(wt), task="work"))

    async def stopped(self, agent_id):
        self.tree.set_status(agent_id, "cancelled", "stopped")
        return {"agent_id": agent_id, "status": "cancelled"}

    monkeypatch.setattr(Runner, "stop", stopped)
    cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))


@pytest.mark.parametrize("failure", ["filtered-path-refused", "index-locked"])
def test_hg_r9_clean_branches_keeps_worktree_whose_checkpoint_failed(
        tmp_path, monkeypatch, capsys, failure):
    """The branch has no commits beyond main, so the agent's only work is the
    uncommitted file the stop checkpoint failed to save. `clean --branches`
    without `--force` must not delete it."""
    root = repo(tmp_path)
    wt = worktree(root, "ag-one", "agents/worker/one")
    if failure == "filtered-path-refused":
        git(root, "config", "filter.probe.clean", "cat")
        (wt / ".gitattributes").write_text("work.txt filter=probe\n")
    else:
        (registration(root, wt) / "index.lock").write_text("")
    (wt / "work.txt").write_text("the only copy\n")
    branch_before = rev(root, "agents/worker/one")
    _stop(root, wt, monkeypatch)
    assert rev(root, "agents/worker/one") == branch_before, \
        "precondition: the checkpoint was expected to fail"
    capsys.readouterr()
    cli.cmd_clean(argparse.Namespace(path=str(root), branches=True, homes=False,
                                     tree=False, force=False))
    assert (wt / "work.txt").is_file(), \
        "clean --branches deleted the worktree of a node whose checkpoint failed"
    assert (wt / "work.txt").read_text() == "the only copy\n"
