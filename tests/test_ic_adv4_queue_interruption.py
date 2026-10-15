"""IC-C11/P4/P5: interrupt the on-disk graph walk during capture."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from multiagents import gitops
from multiagents.paths import ProjectPaths
from multiagents.scheduler import import_validation
from multiagents.scheduler.results import Results

from test_ic_adv4_import_edges import repos, config, git, snapshot, commit  # noqa: F401


class Authority:
    def __init__(self, attempt, branch):
        self.attempt, self.branch = attempt, branch

    def get(self, run_id):
        return {"seeded": False, "worktree": str(self.attempt), "branch": self.branch}

    @contextmanager
    def pinned_worktree(self, path):
        yield path


def test_ic_c11_p5_midwalk_interruption_cleans_queue_and_git_process(repos, monkeypatch):
    host, attempt, _, parent = repos
    tree = git(attempt, "rev-parse", "HEAD^{tree}")
    tip = parent
    for i in range(80):
        tip = commit(attempt, tree, tip, str(i))
    branch = git(attempt, "symbolic-ref", "--short", "HEAD")
    git(attempt, "update-ref", "refs/heads/" + branch, tip)
    paths = ProjectPaths(host)
    paths.ensure()
    paths.scheduler.mkdir(parents=True, exist_ok=True)
    result = Results(paths, config())
    remaining = import_validation.ImportValidation.remaining
    popen = import_validation.subprocess.Popen
    queue_dirs, processes = [], []
    checked = 0
    connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal checked
            answer = super().execute(sql, parameters)
            if sql.startswith("UPDATE objects SET checked=1"):
                checked += 1
            return answer

    def connected(*args, **kwargs):
        return connect(*args, factory=TrackingConnection, **kwargs)

    def interrupt(validation):
        queues = list(paths.scheduler.rglob("objects.sqlite3"))
        if queues and checked >= 10:
            queue_dirs.extend(q.parent for q in queues)
            # A small graph must not consume an unbounded disk queue. This
            # generous allowance includes SQLite's page and journal overhead.
            assert all(0 < q.stat().st_size < 1_000_000 for q in queues)
            raise InterruptedError("capture interrupted during graph validation")
        return remaining(validation)

    def started(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    before = snapshot(host)
    with monkeypatch.context() as patch:
        patch.setattr(import_validation.ImportValidation, "remaining", interrupt)
        patch.setattr(import_validation.subprocess, "Popen", started)
        patch.setattr(import_validation.sqlite3, "connect", connected)
        with pytest.raises(gitops.GitError, match="capture interrupted"):
            result.capture(SimpleNamespace(id="run"), {
                "input_commit": parent, "launch_evidence": {"executor": {"kind": "isolated"}}},
                Authority(attempt, branch))
    assert queue_dirs, "interruption missed the on-disk work queue"
    assert all(not p.exists() for p in queue_dirs), "capture left its queue and object copies behind"
    assert all(p.poll() is not None for p in processes), "validation left a Git subprocess alive"
    assert snapshot(host) == before
