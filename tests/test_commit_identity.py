"""Commit identity — context/specs/commit-identity.md (CI-R1, CI-R2).

Black-box: `gitops.commit_all(worktree, message)` and a real Runner run whose
end-of-run commit goes through it. Git's own view (`git log`, `git config`)
is the oracle; nothing here looks inside the implementation.

Identity isolation. Git finds an identity in: repo config, global config
(HOME/.gitconfig, XDG_CONFIG_HOME/git/config, GIT_CONFIG_GLOBAL), system config
(GIT_CONFIG_SYSTEM, unless GIT_CONFIG_NOSYSTEM), `-c` via GIT_CONFIG_PARAMETERS /
GIT_CONFIG_COUNT, the GIT_AUTHOR_* / GIT_COMMITTER_* / EMAIL variables, and as a
last resort the passwd entry plus hostname. The `no_identity` fixture closes
every one of those, and `test_control_*` proves a plain `git commit` really
fails under it — otherwise every CI-R1 test here would be green for nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from multiagents import gitops


IDENTITY_VARS = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
    "EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)

SEED = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid"]
HOOK_MARKER = "CI_R2_HOOK_REFUSED_THIS_COMMIT"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def author(repo: Path, ref: str = "HEAD") -> tuple[str, str]:
    out = git(repo, "log", "-1", "--format=%an%x00%ae", ref).stdout.strip()
    name, email = out.split("\0")
    return name, email


def committer(repo: Path, ref: str = "HEAD") -> tuple[str, str]:
    out = git(repo, "log", "-1", "--format=%cn%x00%ce", ref).stdout.strip()
    name, email = out.split("\0")
    return name, email


def commit_count(repo: Path, ref: str = "HEAD") -> int:
    return int(git(repo, "rev-list", "--count", ref).stdout.strip())


def make_repo(path: Path) -> Path:
    """A repository with one commit, made WITHOUT writing any identity to config."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, *SEED, "commit", "-q", "--allow-empty", "-m", "seed")
    return path


def files_under(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.fixture
def no_identity(tmp_path, monkeypatch):
    """Close every source git could take an author or committer from."""
    home = tmp_path / "empty-home"
    xdg = tmp_path / "empty-xdg"
    home.mkdir()
    xdg.mkdir()
    global_cfg = tmp_path / "empty-global-gitconfig"
    global_cfg.write_text("")
    for name in IDENTITY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return {"home": home, "xdg": xdg, "global": global_cfg}


def _forbid_host_guess(repo: Path) -> None:
    """Stop git inventing an identity from passwd + hostname.

    `user.useConfigOnly` supplies no identity — it only forbids git's guess —
    so the repository is still in the state CI-R1 describes: nothing names an
    author. Without it the control below passes or fails depending on whether
    the machine's hostname happens to have a domain.
    """
    git(repo, "config", "user.useConfigOnly", "true")


@pytest.fixture
def bare_repo(tmp_path, no_identity):
    repo = make_repo(tmp_path / "repo")
    _forbid_host_guess(repo)
    return repo


# --------------------------------------------------------------------------
# Control: the isolation is real


def test_control_a_plain_git_commit_fails_without_identity(bare_repo):
    (bare_repo / "f.txt").write_text("x")
    git(bare_repo, "add", "-A")
    plain = git(bare_repo, "commit", "-m", "plain", check=False)
    assert plain.returncode != 0, (
        "the no-identity setup leaks an identity from somewhere, so the CI-R1 "
        f"tests below would prove nothing: {plain.stdout}{plain.stderr}")
    assert git(bare_repo, "config", "--get", "user.email", check=False).returncode != 0
    assert git(bare_repo, "config", "--get", "user.name", check=False).returncode != 0


# --------------------------------------------------------------------------
# CI-R1


def test_ci_r1_commit_all_commits_without_a_configured_identity(bare_repo):
    before = commit_count(bare_repo)
    (bare_repo / "work.txt").write_text("agent output\n")

    result = gitops.commit_all(bare_repo, "agent: work in progress")

    assert result.ok, f"commit_all must succeed with no identity: {result}"
    assert commit_count(bare_repo) == before + 1, "the work must reach the branch"
    assert git(bare_repo, "show", "HEAD:work.txt").stdout == "agent output\n"
    assert git(bare_repo, "status", "--porcelain").stdout.strip() == "", \
        "nothing may be left staged or unstaged in the worktree"
    assert git(bare_repo, "log", "-1", "--format=%s").stdout.strip() == \
        "agent: work in progress"


def test_ci_r1_fallback_names_multiagents_when_no_role_is_known(bare_repo):
    """commit_all(worktree, message) is told no role and no agent id, and the
    repository is on its default branch, so the spec's defaults apply."""
    (bare_repo / "work.txt").write_text("x")
    result = gitops.commit_all(bare_repo, "wip")
    assert result.ok, result
    assert author(bare_repo) == ("multiagents", "agent@multiagents.invalid")
    assert committer(bare_repo) == ("multiagents", "agent@multiagents.invalid")


def test_ci_r1_fallback_never_writes_any_git_config(bare_repo, no_identity):
    repo_config = (bare_repo / ".git" / "config").read_bytes()
    global_before = no_identity["global"].read_bytes()
    (bare_repo / "work.txt").write_text("x")

    assert gitops.commit_all(bare_repo, "wip").ok

    assert (bare_repo / ".git" / "config").read_bytes() == repo_config, \
        "the repository's config must be untouched"
    assert no_identity["global"].read_bytes() == global_before
    assert files_under(no_identity["home"]) == {}, "nothing written under HOME"
    assert files_under(no_identity["xdg"]) == {}, "nothing written under XDG_CONFIG_HOME"
    for key in ("user.name", "user.email"):
        assert git(bare_repo, "config", "--get", key, check=False).returncode != 0


def test_ci_r1_fallback_applies_to_that_one_invocation_only(bare_repo):
    (bare_repo / "one.txt").write_text("1")
    assert gitops.commit_all(bare_repo, "wip").ok

    for name in IDENTITY_VARS[:6] + ("EMAIL",):
        assert name not in os.environ, f"{name} leaked into the calling process"
    (bare_repo / "two.txt").write_text("2")
    git(bare_repo, "add", "-A")
    plain = git(bare_repo, "commit", "-m", "plain", check=False)
    assert plain.returncode != 0, \
        "after commit_all, git on its own must still have no identity"


def test_ci_r1_repo_identity_is_used_unchanged(bare_repo):
    git(bare_repo, "config", "user.name", "Repo Person")
    git(bare_repo, "config", "user.email", "repo@example.invalid")
    (bare_repo / "w.txt").write_text("x")

    assert gitops.commit_all(bare_repo, "wip").ok
    assert author(bare_repo) == ("Repo Person", "repo@example.invalid")
    assert committer(bare_repo) == ("Repo Person", "repo@example.invalid")


def test_ci_r1_global_identity_is_used_unchanged(bare_repo, no_identity):
    no_identity["global"].write_text(
        "[user]\n\tname = Global Person\n\temail = global@example.invalid\n")
    (bare_repo / "w.txt").write_text("x")

    assert gitops.commit_all(bare_repo, "wip").ok
    assert author(bare_repo) == ("Global Person", "global@example.invalid")


def test_ci_r1_environment_identity_is_used_unchanged(bare_repo, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Env Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "committer@example.invalid")
    (bare_repo / "w.txt").write_text("x")

    assert gitops.commit_all(bare_repo, "wip").ok
    assert author(bare_repo) == ("Env Author", "author@example.invalid")
    assert committer(bare_repo) == ("Env Committer", "committer@example.invalid")


def test_ci_r1_works_in_a_linked_worktree(tmp_path, no_identity):
    """Agents commit in linked worktrees, not the main checkout."""
    repo = make_repo(tmp_path / "main")
    _forbid_host_guess(repo)
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "agents/worker/abc123")
    (wt / "work.txt").write_text("x")

    result = gitops.commit_all(wt, "wip")
    assert result.ok, result
    assert git(repo, "show", "agents/worker/abc123:work.txt").stdout == "x"
    assert author(repo, "agents/worker/abc123")[1].endswith("@multiagents.invalid")


# --------------------------------------------------------------------------
# CI-R2


def _failing_hook(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho {HOOK_MARKER} >&2\nexit 1\n")
    hook.chmod(0o755)


def _reported_error(repo: Path, message: str) -> str:
    """commit_all's error text, whether it raises or returns a failed result.

    The spec allows either ("raises or returns an error naming the stderr").
    Returning an ok result is the one thing it rules out.
    """
    try:
        result = gitops.commit_all(repo, message)
    except Exception as exc:                        # the raising form
        return str(exc)
    assert not result.ok, f"a failed commit was reported as success: {result}"
    return f"{result.err}\n{result.out}"


def test_ci_r2_a_failing_commit_is_an_error_carrying_git_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    repo = make_repo(tmp_path / "repo")
    _failing_hook(repo)
    before = commit_count(repo)
    (repo / "w.txt").write_text("x")

    error = _reported_error(repo, "wip")

    assert HOOK_MARKER in error, f"git's stderr must reach the caller: {error!r}"
    assert commit_count(repo) == before


def test_ci_r2_a_failing_commit_without_identity_is_still_reported(bare_repo):
    """The fallback identity must not turn into a way of hiding other failures."""
    _failing_hook(bare_repo)
    (bare_repo / "w.txt").write_text("x")
    error = _reported_error(bare_repo, "wip")
    assert HOOK_MARKER in error, error


def test_ci_r2_a_clean_tree_is_not_an_error(bare_repo):
    before = commit_count(bare_repo)
    result = gitops.commit_all(bare_repo, "wip")
    assert result.ok, f"nothing to commit is not a failure: {result}"
    assert commit_count(bare_repo) == before


def test_ci_r2_committing_twice_is_one_commit_then_a_clean_no_op(bare_repo):
    before = commit_count(bare_repo)
    (bare_repo / "w.txt").write_text("x")
    first = gitops.commit_all(bare_repo, "wip")
    second = gitops.commit_all(bare_repo, "wip")
    assert first.ok, first
    assert second.ok, second
    assert commit_count(bare_repo) == before + 1


# --------------------------------------------------------------------------
# Through the runner: the run that owns the commit


def _runner(project: Path, agent_script: str):
    from multiagents.config import AgentSpec, Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner
    paths = ProjectPaths(project)
    paths.ensure()
    make_repo(project)
    config = Config(
        project={}, providers={"p": {"bin": "sh", "spawn": {"args": ["-c", agent_script]}}},
        agents={"worker": AgentSpec("worker", "p", "m")}, models={}, instruction_dirs=[],
    )
    return Runner(paths, config)


def _run_to_end(runner) -> str:
    async def scenario():
        started = await runner.start("worker", "go")
        agent_id = started["agent_id"]
        for _ in range(100):
            node = runner.tree.get(agent_id)
            if node.status not in ("pending", "running"):
                break
            await asyncio.sleep(0.1)
        return agent_id
    return asyncio.run(scenario())


def test_ci_r1_a_run_commits_its_work_as_the_agent_without_identity(tmp_path, no_identity):
    project = tmp_path / "project"
    project.mkdir()
    runner = _runner(project, "echo done > work.txt")
    _forbid_host_guess(project)

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    assert node.branch, "the run must have had a branch"
    shown = git(project, "show", f"{node.branch}:work.txt", check=False)
    assert shown.returncode == 0 and shown.stdout == "done\n", \
        f"the agent's work never reached its branch: {shown.stderr}"
    assert author(project, node.branch) == (
        "multiagents worker", f"{agent_id}@multiagents.invalid")


def test_ci_r2_a_run_whose_commit_fails_reports_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    project = tmp_path / "project"
    project.mkdir()
    runner = _runner(project, "echo done > work.txt; echo all finished")
    _failing_hook(project)            # linked worktrees share the repository's hooks

    agent_id = _run_to_end(runner)

    node = runner.tree.get(agent_id)
    result = json.loads((runner.paths.run_dir(agent_id) / "result.json").read_text())
    reported = f"{result.get('text', '')}\n{node.summary or ''}"
    assert HOOK_MARKER in reported, (
        "the run's result/summary must name the commit failure; got "
        f"text={result.get('text')!r} summary={node.summary!r}")

    events = [json.loads(line) for line in
              runner.tree.events_path.read_text().splitlines() if line.strip()]
    mine = [e for e in events if e.get("agent") == agent_id]
    assert any(HOOK_MARKER in json.dumps(e) for e in mine), \
        f"the node's events must record the commit failure: {[e.get('kind') for e in mine]}"
