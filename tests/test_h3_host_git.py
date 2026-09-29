"""HG-R1..R6: host Git must not execute programs selected by agent content.

Each probe is a real Git command against a real repository. A program records
execution by creating a file outside the checkout; absence is the security
oracle, while repository state verifies that the operation actually happened.
"""
from __future__ import annotations

import ast
import argparse
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from multiagents import cli, gitops
from multiagents.config import Config
from multiagents.paths import ProjectPaths
from multiagents.runner import Runner
from multiagents.tree import Node, Tree


@pytest.fixture(autouse=True)
def isolated_git(tmp_path, monkeypatch):
    cfg = tmp_path / "global.gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"GIT_AUTHOR_NAME": "tester", "GIT_COMMITTER_NAME": "tester",
                        "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode:
        raise AssertionError(f"git {args}: {p.stderr}")
    return p


def program(path: Path, sentinel: Path, *, exit_code: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n: > '{sentinel}'\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "tester")
    git(root, "config", "user.email", "t@example.invalid")
    (root / ".gitignore").write_text(".multiagents/\n")
    (root / "base.txt").write_text("base\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def agent_branch(root: Path, tmp_path: Path, *, file: str = "agent.txt") -> Path:
    wt = tmp_path / "worktrees" / "agent"
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    (wt / file).write_text("agent\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "agent")
    return wt


def runner(root: Path, *, hooks: bool = False, content: bool = False,
           style: str = "squash") -> Runner:
    paths = ProjectPaths(root)
    paths.ensure()
    policy = {"git": {"merge": {"style": style, "host_hooks": hooks,
                                "host_content_programs": content}}}
    return Runner(paths, Config(project=policy, providers={}, agents={},
                                models={}, instruction_dirs=[]))


def node(r: Runner, wt: Path) -> Node:
    n = Node(id="ag-one", agent="worker", provider="p", model="m", parent=None,
             depth=1, branch="agents/worker/one", worktree=str(wt),
             status="done", task="work")
    r.tree.add(n)
    return n


def events(paths: ProjectPaths) -> list[dict]:
    if not paths.events_file.exists():
        return []
    return [json.loads(line) for line in paths.events_file.read_text().splitlines()
            if line.strip()]


def stopped_checkpoint(root: Path, wt: Path, monkeypatch, capsys, *,
                       hooks: bool = False) -> tuple[str, list[dict]]:
    """Stop an interrupted node through the public CLI, with no agent process."""
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(argparse.Namespace(path=str(root), force=False, nested=False)) == 0
    paths = ProjectPaths(root)
    config_file = paths.config / "project.yaml"
    with config_file.open("a") as stream:
        stream.write(f"\ngit:\n  merge:\n    host_hooks: {'true' if hooks else 'false'}\n")
    Tree(paths.tree_file, paths.events_file).add(
        Node(id="ag-stop", agent="worker", provider="p", model="m", parent=None,
             depth=1, status="running", pid=0, branch="agents/worker/one",
             worktree=str(wt), task="work"))

    async def stopped(self, agent_id):
        self.tree.set_status(agent_id, "cancelled", "stopped")
        return {"agent_id": agent_id, "status": "cancelled"}

    monkeypatch.setattr(Runner, "stop", stopped)
    capsys.readouterr()
    cli.cmd_stop(argparse.Namespace(path=str(root), keep_containers=True))
    return capsys.readouterr().out, events(paths)


@pytest.mark.parametrize("style", ["squash", "no-ff"])
def test_hg_r2_default_merge_skips_trusted_base_hooks(tmp_path, style):
    root = repo(tmp_path)
    agent_branch(root, tmp_path)
    sentinel = tmp_path / "base-hook-ran"
    program(root / ".git" / "hooks" / ("pre-commit" if style == "squash" else "pre-merge-commit"), sentinel)
    status, detail = gitops.merge(root, "agents/worker/one", "merge", style=style)
    assert status == "merged", detail
    assert (root / "agent.txt").read_text() == "agent\n"
    assert not sentinel.exists(), "the host ran a base hook by default"


def test_hg_r2_default_merge_skips_trusted_hookspath(tmp_path):
    root = repo(tmp_path)
    agent_branch(root, tmp_path)
    sentinel = tmp_path / "hookspath-ran"
    hooks = tmp_path / "trusted-hooks"
    program(hooks / "pre-commit", sentinel)
    git(root, "config", "core.hooksPath", str(hooks))
    status, detail = gitops.merge(root, "agents/worker/one", "merge")
    assert status == "merged", detail
    assert not sentinel.exists()


@pytest.mark.parametrize("hooks", [False, True])
def test_hg_r2_stop_bookkeeping_commit_skips_hooks(tmp_path, monkeypatch, capsys, hooks):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "checkpoint-hook-ran"
    program(root / ".git" / "hooks" / "pre-commit", sentinel)
    (wt / "pending.txt").write_text("pending\n")
    output, _ = stopped_checkpoint(root, wt, monkeypatch, capsys, hooks=hooks)
    assert "committed" in output
    assert not sentinel.exists()
    assert git(root, "show", "agents/worker/one:pending.txt").stdout == "pending\n"


def test_hg_r2_worktree_add_skips_post_checkout_hook(tmp_path):
    root = repo(tmp_path)
    sentinel = tmp_path / "checkout-hook-ran"
    program(root / ".git" / "hooks" / "post-checkout", sentinel)
    wt = tmp_path / "worktrees" / "new"
    gitops.create_worktree(root, wt, "agents/worker/new", base="main", unique=False)
    assert wt.is_dir()
    assert not sentinel.exists()


def test_hg_r2_merge_agent_skip_notice_once_per_process(tmp_path):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "hook-ran"
    program(root / ".git" / "hooks" / "pre-commit", sentinel)
    r = runner(root)
    n = node(r, wt)
    result = r.merge_agent(n.id)
    assert result["result"] == "merged", result
    assert not sentinel.exists()
    second_wt = tmp_path / "worktrees" / "second"
    gitops.create_worktree(root, second_wt, "agents/worker/two", base="main", unique=False)
    (second_wt / "second.txt").write_text("second\n")
    git(second_wt, "add", "-A")
    git(second_wt, "commit", "-q", "-m", "second")
    sentinel.unlink(missing_ok=True)  # fixture's agent commit runs outside host bookkeeping
    second = Node(id="ag-two", agent="worker", provider="p", model="m",
                  parent=None, depth=1, branch="agents/worker/two",
                  worktree=str(second_wt), status="done", task="work two")
    r.tree.add(second)
    result2 = r.merge_agent(second.id)
    assert result2["result"] == "merged", result2
    assert not sentinel.exists()
    notice = "git.merge.host_hooks"
    lines = [line for response in (result, result2)
             for line in response.get("detail", "").splitlines() if notice in line]
    assert len(lines) == 1, (result, result2)
    notices = [event for event in events(r.paths) if notice in json.dumps(event)]
    assert len(notices) == 1, notices


def test_hg_r2_host_hooks_opt_in_runs_only_for_base_merge(tmp_path):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "opt-in-ran"
    program(root / ".git" / "hooks" / "pre-commit", sentinel)
    r = runner(root, hooks=True)
    result = r.merge_agent(node(r, wt).id)
    assert result["result"] == "merged", result
    assert sentinel.exists(), "explicit host_hooks did not run the base hook"

    # A parent worktree is an agent checkout. The opt-in must not reach it.
    sentinel.unlink()
    parent = tmp_path / "worktrees" / "parent"
    gitops.create_worktree(root, parent, "agents/parent/one", base="main", unique=False)
    child = tmp_path / "worktrees" / "child"
    gitops.create_worktree(root, child, "agents/worker/child", base="main", unique=False)
    (child / "child.txt").write_text("child\n")
    git(child, "add", "-A")
    git(child, "commit", "-q", "-m", "child")
    sentinel.unlink(missing_ok=True)  # agent commit is outside the opt-in boundary
    child_node = Node(id="ag-child", agent="worker", provider="p", model="m",
                      parent=None, depth=1, branch="agents/worker/child",
                      worktree=str(child), status="done", task="child")
    r.tree.add(child_node)
    result2 = r.merge_agent(child_node.id, into=str(parent))
    assert result2["result"] == "merged", result2
    assert (parent / "child.txt").is_file()
    assert not sentinel.exists(), "host_hooks opt-in reached a parent worktree"


def test_hg_r1_agent_worktree_config_cannot_select_host_hook(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "agent-config-hook-ran"
    hookdir = tmp_path / "malicious-hooks"
    program(hookdir / "pre-commit", sentinel)
    git(root, "config", "extensions.worktreeConfig", "true")
    git(wt, "config", "--worktree", "core.hooksPath", str(hookdir))
    (wt / "pending.txt").write_text("pending\n")
    output, _ = stopped_checkpoint(root, wt, monkeypatch, capsys)
    assert "committed" in output
    assert not sentinel.exists(), "host commit honoured agent-written config.worktree"
    assert git(root, "show", "agents/worker/one:pending.txt").stdout == "pending\n"


def test_hg_r1_agent_git_file_cannot_redirect_host_commit(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "redirected-hook-ran"
    fake = tmp_path / "fake-gitdir"
    fake.mkdir()
    genuine = (root / ".git" / "worktrees" / wt.name)
    (fake / "HEAD").write_bytes((genuine / "HEAD").read_bytes())
    (fake / "commondir").write_text(str(root / ".git") + "\n")
    (fake / "index").write_bytes((genuine / "index").read_bytes())
    program(fake / "hooks" / "pre-commit", sentinel)
    git(root, "config", "extensions.worktreeConfig", "true")
    (fake / "config.worktree").write_text(f"[core]\n\thooksPath = {fake / 'hooks'}\n")
    (wt / ".git").write_text(f"gitdir: {fake}\n")
    (wt / "pending.txt").write_text("pending\n")
    output, _ = stopped_checkpoint(root, wt, monkeypatch, capsys)
    assert "committed" in output
    assert not sentinel.exists()
    assert git(root, "show", "agents/worker/one:pending.txt").stdout == "pending\n"


def test_hg_r3_trusted_fsmonitor_does_not_run_during_merge(tmp_path):
    root = repo(tmp_path)
    agent_branch(root, tmp_path)
    sentinel = tmp_path / "fsmonitor-ran"
    monitor = tmp_path / "fsmonitor"
    program(monitor, sentinel)
    git(root, "config", "core.fsmonitor", str(monitor))
    status, detail = gitops.merge(root, "agents/worker/one", "merge")
    assert status == "merged", detail
    assert not sentinel.exists()


def test_hg_r4_stop_refuses_a_selected_clean_filter(tmp_path, monkeypatch, capsys):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    sentinel = tmp_path / "clean-ran"
    script = tmp_path / "clean"
    program(script, sentinel)
    git(root, "config", "filter.probe.clean", str(script))
    (wt / ".gitattributes").write_text("payload.bin filter=probe\n")
    (wt / "payload.bin").write_bytes(b"unconverted payload")
    before = git(root, "rev-parse", "agents/worker/one").stdout.strip()
    output, emitted = stopped_checkpoint(root, wt, monkeypatch, capsys)
    assert not sentinel.exists(), "host ran the selected clean filter"
    assert "committed" not in output
    assert "payload.bin" in output
    assert any("payload.bin" in json.dumps(event) for event in emitted), emitted
    assert git(root, "rev-parse", "agents/worker/one").stdout.strip() == before
    assert (wt / "payload.bin").read_bytes() == b"unconverted payload"


def driver_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("shared.txt merge=probe\n")
    (root / "shared.txt").write_text("common\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "shared")
    wt = tmp_path / "worktrees" / "agent"
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    (wt / "shared.txt").write_text("agent change\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "agent")
    (root / "shared.txt").write_text("base change\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base change")
    sentinel = tmp_path / "driver-ran"
    driver = tmp_path / "driver"
    driver.write_text(f"#!/bin/sh\n: > '{sentinel}'\ncp \"$2\" \"$1\"\n")
    driver.chmod(0o755)
    git(root, "config", "merge.probe.driver", f"{driver} %O %A %B")
    return root, wt, sentinel


def test_hg_r4_merge_does_not_run_selected_trusted_driver(tmp_path):
    root, _, sentinel = driver_project(tmp_path)
    status, detail = gitops.merge(root, "agents/worker/one", "merge", style="no-ff")
    assert status == "conflict", (status, detail)
    assert not sentinel.exists(), "host ran a content merge driver by default"
    assert (root / "shared.txt").read_text() == "base change\n"


def test_hg_r4_content_program_opt_in_reaches_base_merge_only(tmp_path):
    root, wt, sentinel = driver_project(tmp_path)
    r = runner(root, content=True, style="no-ff")
    result = r.merge_agent(node(r, wt).id)
    assert result["result"] == "merged", result
    assert sentinel.exists(), "host_content_programs opt-in did not enable the trusted driver"

    # Repeat on a separate repository, targeting a parent's agent checkout.
    other = tmp_path / "parent-case"
    other.mkdir()
    root2, child_wt, sentinel2 = driver_project(other)
    parent_wt = other / "worktrees" / "parent"
    gitops.create_worktree(root2, parent_wt, "agents/parent/one", base="main", unique=False)
    r2 = runner(root2, content=True, style="no-ff")
    result2 = r2.merge_agent(node(r2, child_wt).id, into=str(parent_wt))
    assert result2["result"] == "conflict", result2
    assert not sentinel2.exists(), "content-program opt-in reached a parent worktree"
    assert (parent_wt / "shared.txt").read_text() == "base change\n"


def test_hg_r4_recovery_worktree_add_reports_unconverted_filtered_path(tmp_path, capsys):
    root = repo(tmp_path)
    (root / ".gitattributes").write_text("payload.bin filter=probe\n")
    (root / "payload.bin").write_bytes(b"stored payload")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "filtered path")
    wt = tmp_path / "worktrees" / "agent"
    gitops.create_worktree(root, wt, "agents/worker/one", base="main", unique=False)
    git(root, "worktree", "remove", "--force", str(wt))
    sentinel = tmp_path / "smudge-ran"
    smudge = tmp_path / "smudge"
    smudge.write_text(f"#!/bin/sh\n: > '{sentinel}'\ncat\n")
    smudge.chmod(0o755)
    git(root, "config", "filter.probe.smudge", str(smudge))
    capsys.readouterr()
    gitops.attach_worktree(root, wt, "agents/worker/one")
    notice = capsys.readouterr()
    assert (wt / "payload.bin").read_bytes() == b"stored payload"
    assert not sentinel.exists(), "recovery checkout ran a smudge filter on the host"
    said = notice.out + notice.err
    assert "payload.bin" in said and "unconverted" in said.lower(), said


def test_hg_r5_push_uses_trusted_local_remote_despite_worktree_config(tmp_path):
    root = repo(tmp_path)
    wt = agent_branch(root, tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(root, "config", "extensions.worktreeConfig", "true")
    sentinel = tmp_path / "ssh-ran"
    ssh = tmp_path / "ssh"
    program(ssh, sentinel)
    git(wt, "config", "--worktree", "core.sshCommand", str(ssh))
    git(wt, "config", "--worktree", "remote.evil.url", "ssh://host.invalid/never")
    paths = ProjectPaths(root)
    paths.ensure()
    config = Config(project={"git": {"remote": str(remote),
                                       "push_agent_branches": True}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    r = Runner(paths, config)
    n = node(r, wt)
    result = r.push_branch(n.id)
    assert result["pushed"], result
    assert git(remote, "rev-parse", "refs/heads/agents/worker/one").stdout.strip()
    assert not sentinel.exists()


def test_hg_r6_git_call_inventory_has_no_unreviewed_entry():
    source = Path(gitops.__file__).read_text()
    module = ast.parse(source)
    # Every direct git invocation needs an audit entry. This allowlist is
    # intentionally in the test so a newly added call cannot pass unnoticed.
    audited = {
        "is_repo", "init_repo", "initial_commit", "_read", "repo_root", "has_commits",
        "current_branch", "branch_exists", "create_worktree", "attach_worktree",
        "worktree_branch", "move_aside", "_registered_worktree", "remove_worktree",
        "prune_worktrees", "delete_branch", "resolve_commit", "short_sha",
        "holds_unmerged_commits", "untracked_in_the_way", "reset_keep",
        "_base_hooks", "_undo_merge", "_merge", "push",
    }
    callers = {f.name for f in module.body if isinstance(f, ast.FunctionDef)
               and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "run" for n in ast.walk(f))}
    assert callers <= audited, f"new direct Git call needs H3 audit: {callers - audited}"


def test_hg_r6_documented_call_families_are_present():
    spec = Path(__file__).resolve().parents[1] / "context/specs/h3-host-git-execution.md"
    text = spec.read_text()
    for family in ("merge", "commit_all", "worktree", "push", "reset"):
        assert family in text
