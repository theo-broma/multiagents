"""H13: auto gc/maintenance is off for commits made on an agent's behalf."""

from __future__ import annotations

import subprocess
from pathlib import Path

from multiagents import gitops
from multiagents.executor.base import build_env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_multiagents_commit_disables_auto_maintenance(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f.txt").write_text("x")
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    result = gitops.commit_all(tmp_path, "checkpoint")
    assert result.ok, result.err
    commits = [c for c in seen if "commit" in c]
    assert commits
    for cmd in commits:
        assert "gc.auto=0" in cmd and "maintenance.auto=false" in cmd


def test_agent_env_disables_auto_maintenance():
    env = build_env(passthrough=[], blocked=[], home=None, identity={})
    params = env["GIT_CONFIG_PARAMETERS"]
    assert "'gc.auto'='0'" in params
    assert "'maintenance.auto'='false'" in params
    assert "'commit.gpgsign'='false'" in params
