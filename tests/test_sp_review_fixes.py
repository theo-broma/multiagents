"""Regression tests for the fixes to review ag-39521a of session persistence.

Contract: context/specs/session-persistence.md (SP-R1, SP-R2, SP-R4).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents import executor as executor_pkg
from multiagents import gitops, watchdog


def _provider(directory: str, name: str = "p") -> SimpleNamespace:
    return SimpleNamespace(name=name, transcript={"dir": directory, "glob": "*.jsonl"})


# --------------------------------------------------------------- SP-R1 --

@pytest.mark.parametrize("directory", [
    "~/.cli/projects/{slug}/../../../etc",
    "~/.cli/projects/{slug}/..",
    "/var/{slug}/x/../../..",
])
def test_dotdot_after_the_placeholder_is_refused(directory, capsys):
    provider = _provider(directory, name=f"dotdot-{abs(hash(directory))}")
    assert watchdog.transcript_prefix(provider) is None
    assert watchdog.transcript_sources(provider, Path("/tmp"), object()) == []
    assert "'..'" in capsys.readouterr().err


def test_tilde_is_the_executors_home_not_this_processes(tmp_path):
    agent_home = tmp_path / "agent-home"
    executor = SimpleNamespace(container_home=lambda: agent_home)
    provider = _provider("~/.cli/projects/{slug}")
    [(directory, glob)] = watchdog.transcript_sources(provider, tmp_path, executor)
    assert directory.parent == agent_home / ".cli" / "projects"
    assert watchdog.transcript_prefix(provider, agent_home) == agent_home / ".cli" / "projects"


def test_a_home_prefix_is_judged_against_the_executors_home(tmp_path):
    agent_home = tmp_path / "agent-home"
    provider = _provider("~/{slug}", name="home-prefix")
    assert watchdog.transcript_prefix(provider, agent_home) is None


def test_executors_agree_on_home():
    from multiagents.executor import DockerExecutor, LocalExecutor

    assert LocalExecutor().container_home() == Path.home()
    assert DockerExecutor({}).container_home() == Path.home()


# --------------------------------------------------------------- SP-R2 --

def test_newest_transcript_survives_a_file_vanishing_mid_scan(tmp_path, monkeypatch):
    provider = _provider(str(tmp_path / "{slug}"))
    [(directory, _)] = watchdog.transcript_sources(provider, tmp_path, object())
    directory.mkdir(parents=True)
    kept = directory / "kept.jsonl"
    kept.write_text("{}\n")
    (directory / "gone.jsonl").write_text("{}\n")

    real = Path.stat

    def stat(self, *args, **kwargs):
        # is_file() still sees it; the stat taken for the mtime does not.
        if self.name == "gone.jsonl" and not kwargs and not args:
            raise FileNotFoundError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "is_file", lambda self: real(self).st_mode & 0o170000 == 0o100000)
    assert watchdog.newest_transcript(provider, tmp_path, object()) == kept


@pytest.fixture
def fresh_at(monkeypatch):
    monkeypatch.setattr(executor_pkg, "_at", type(executor_pkg._at)())
    monkeypatch.setattr(executor_pkg, "_unreadable", set())
    return executor_pkg


def test_executor_at_reports_a_broken_config_once_and_reads_nothing(tmp_path, fresh_at,
                                                                    monkeypatch, capsys):
    import multiagents.config as config

    def broken(*a, **k):
        raise ValueError("bad executor kind")

    monkeypatch.setattr(fresh_at, "_project_of", lambda cwd: tmp_path)
    monkeypatch.setattr(config, "load", broken)
    monkeypatch.setattr(fresh_at, "_RESTAT", 0.0)
    edits = iter(range(100))                           # the config keeps changing
    monkeypatch.setattr(fresh_at, "_config_stamp", lambda root: (next(edits),))
    for _ in range(3):
        assert fresh_at.executor_at(tmp_path, "p") is None
    assert capsys.readouterr().err.count("cannot read the configuration") == 1


def test_executor_at_does_not_swallow_a_bug(tmp_path, fresh_at, monkeypatch):
    import multiagents.config as config

    def bug(*a, **k):
        raise AttributeError("a programming error")

    monkeypatch.setattr(fresh_at, "_project_of", lambda cwd: tmp_path)
    monkeypatch.setattr(config, "load", bug)
    with pytest.raises(AttributeError):
        fresh_at.executor_at(tmp_path, "p")


def test_executor_at_restats_at_most_every_few_seconds(tmp_path, fresh_at, monkeypatch):
    import multiagents.config as config

    calls = []
    monkeypatch.setattr(fresh_at, "_project_of", lambda cwd: tmp_path)
    monkeypatch.setattr(fresh_at, "_config_stamp", lambda root: calls.append(root) or ())
    monkeypatch.setattr(config, "load", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    for _ in range(50):
        fresh_at.executor_at(tmp_path, "p")
    assert len(calls) == 1


def test_executor_at_cache_is_bounded(tmp_path, fresh_at, monkeypatch):
    import multiagents.config as config

    monkeypatch.setattr(fresh_at, "_project_of", lambda cwd: tmp_path)
    monkeypatch.setattr(fresh_at, "_config_stamp", lambda root: ())
    monkeypatch.setattr(config, "load", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    for n in range(fresh_at._AT_LIMIT * 3):
        fresh_at.executor_at(tmp_path / str(n), "p")
    assert len(fresh_at._at) == fresh_at._AT_LIMIT


# --------------------------------------------------------------- SP-R4 --

def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


def test_a_worktree_git_cannot_move_is_left_where_it_is(tmp_path, repo):
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "b", str(tree))
    _git(repo, "worktree", "lock", str(tree))          # makes `worktree move` fail
    before = _git(repo, "worktree", "list", "--porcelain")

    with pytest.raises(gitops.GitError, match="worktree move"):
        gitops.move_aside(repo, tree)

    assert (tree / ".git").exists()
    assert _git(repo, "worktree", "list", "--porcelain") == before
    assert not list(tmp_path.glob("wt.aside*"))


def test_a_worktree_is_moved_with_its_registration(tmp_path, repo):
    tree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "b", str(tree))
    moved = gitops.move_aside(repo, tree)
    assert not tree.exists() and (moved / ".git").exists()
    assert str(moved.resolve()) in _git(repo, "worktree", "list", "--porcelain")


def test_moving_aside_never_lands_on_an_existing_name(tmp_path, repo):
    stale = tmp_path / "wt"
    stale.mkdir()
    (stale / "only-copy").write_text("keep me")
    squatter = tmp_path / "wt.aside"
    squatter.mkdir()                                     # empty: rename would replace it

    moved = gitops.move_aside(repo, stale)

    assert squatter.is_dir() and not any(squatter.iterdir())
    assert (moved / "only-copy").read_text() == "keep me"
    assert not moved.is_relative_to(squatter)
    assert not stale.exists()
