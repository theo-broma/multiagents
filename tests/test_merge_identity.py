"""Commit identity in the merge gate — context/specs/commit-identity.md (CI-R3).

CI-R1 gave `commit_all` a fallback identity for a fresh container HOME with no
git identity anywhere. CI-R3 extends that same fallback to every other
`git commit` multiagents itself makes: `gitops.merge`'s squash (and no-ff)
commit, and `gitops.restore_paths`'s revert-then-commit step of the merge
gate. Black-box, same style as test_commit_identity.py: git's own view
(`git log`, `git config`) is the oracle.
"""

from __future__ import annotations

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


def commit_count(repo: Path, ref: str = "HEAD") -> int:
    return int(git(repo, "rev-list", "--count", ref).stdout.strip())


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
    """Stop git inventing an identity from passwd + hostname (see CI-R1's
    fixture in test_commit_identity.py for why this is needed at all)."""
    git(repo, "config", "user.useConfigOnly", "true")


@pytest.fixture
def bare_repo(tmp_path, no_identity):
    """A repo with a base commit and an agent branch ahead of it, both seeded
    WITHOUT writing any identity to config, then closed to any guess."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "protected.txt").write_text("original\n")
    git(repo, "add", "-A")
    git(repo, *SEED, "commit", "-q", "-m", "initial")
    git(repo, "branch", "agents/worker/abc")
    git(repo, "checkout", "-q", "agents/worker/abc")
    (repo / "work.txt").write_text("agent output\n")
    git(repo, "add", "-A")
    git(repo, *SEED, "commit", "-q", "-m", "agent work")
    git(repo, "checkout", "-q", "-")
    _forbid_host_guess(repo)
    return repo


# --------------------------------------------------------------------------
# Control: the isolation is real


def test_control_a_plain_git_commit_fails_without_identity(bare_repo):
    assert git(bare_repo, "merge", "--squash", "agents/worker/abc",
               check=False).returncode == 0
    plain = git(bare_repo, "commit", "-m", "plain", check=False)
    try:
        assert plain.returncode != 0, (
            "the no-identity setup leaks an identity from somewhere, so the "
            f"CI-R3 tests below would prove nothing: {plain.stdout}{plain.stderr}")
    finally:
        git(bare_repo, "merge", "--abort", check=False)
        git(bare_repo, "reset", "--hard", check=False)


# --------------------------------------------------------------------------
# CI-R3 — gitops.merge


def test_ci_r3_merge_squash_commits_without_a_configured_identity(bare_repo):
    before = commit_count(bare_repo)

    status, detail = gitops.merge(bare_repo, "agents/worker/abc", "worker: did the thing")

    assert status == "merged", detail
    assert commit_count(bare_repo) == before + 1
    assert (bare_repo / "work.txt").read_text() == "agent output\n"


def test_ci_r3_merge_names_the_merging_side_not_the_agent(bare_repo):
    status, detail = gitops.merge(bare_repo, "agents/worker/abc", "worker: did the thing")
    assert status == "merged", detail
    assert author(bare_repo) == ("multiagents", "orchestrator@multiagents.invalid")


def test_ci_r3_merge_no_ff_style_also_commits_without_identity(bare_repo):
    status, detail = gitops.merge(bare_repo, "agents/worker/abc",
                                  "worker: did the thing", style="no-ff")

    assert status == "merged", detail
    parents = git(bare_repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3, "a --no-ff merge must be a real merge commit, not a fast-forward"
    assert (bare_repo / "work.txt").read_text() == "agent output\n"
    assert author(bare_repo) == ("multiagents", "orchestrator@multiagents.invalid")


def test_ci_r3_merge_never_writes_any_git_config(bare_repo, no_identity):
    repo_config = (bare_repo / ".git" / "config").read_bytes()
    global_before = no_identity["global"].read_bytes()

    status, detail = gitops.merge(bare_repo, "agents/worker/abc", "worker: did the thing")

    assert status == "merged", detail
    assert (bare_repo / ".git" / "config").read_bytes() == repo_config, \
        "the repository's config must be untouched"
    assert no_identity["global"].read_bytes() == global_before
    for key in ("user.name", "user.email"):
        assert git(bare_repo, "config", "--get", key, check=False).returncode != 0


def test_ci_r3_merge_uses_a_configured_identity_unchanged(bare_repo):
    git(bare_repo, "config", "user.name", "Repo Person")
    git(bare_repo, "config", "user.email", "repo@example.invalid")

    status, detail = gitops.merge(bare_repo, "agents/worker/abc", "worker: did the thing")

    assert status == "merged", detail
    assert author(bare_repo) == ("Repo Person", "repo@example.invalid")


# --------------------------------------------------------------------------
# CI-R3 — gitops.restore_paths (the merge gate's revert-then-commit step)


def test_ci_r3_restore_paths_commits_without_a_configured_identity(bare_repo):
    base = git(bare_repo, "rev-parse", "HEAD").stdout.strip()
    git(bare_repo, "checkout", "-q", "agents/worker/abc")
    (bare_repo / "protected.txt").write_text("tampered\n")
    git(bare_repo, "add", "-A")
    git(bare_repo, *SEED, "commit", "-q", "-m", "protected edit")
    before = commit_count(bare_repo)

    result = gitops.restore_paths(bare_repo, base, ["protected.txt"], "revert protected file")

    assert result.ok, result
    assert commit_count(bare_repo) == before + 1
    assert (bare_repo / "protected.txt").read_text() == "original\n"
    assert author(bare_repo) == ("multiagents", "orchestrator@multiagents.invalid")


def test_ci_r3_restore_paths_uses_a_configured_identity_unchanged(bare_repo):
    base = git(bare_repo, "rev-parse", "HEAD").stdout.strip()
    git(bare_repo, "checkout", "-q", "agents/worker/abc")
    git(bare_repo, "config", "user.name", "Repo Person")
    git(bare_repo, "config", "user.email", "repo@example.invalid")
    (bare_repo / "protected.txt").write_text("tampered\n")
    git(bare_repo, "add", "-A")
    git(bare_repo, *SEED, "commit", "-q", "-m", "protected edit")

    result = gitops.restore_paths(bare_repo, base, ["protected.txt"], "revert protected file")

    assert result.ok, result
    assert author(bare_repo) == ("Repo Person", "repo@example.invalid")
