"""Root CLIs keep their transcripts on the host, even in Docker projects."""

import json
import re
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from multiagents import driver, server, watchdog
from multiagents.paths import ProjectPaths
from multiagents.providers import Provider
from multiagents.transcripts import session_context


class FakeDockerExecutor:
    kind = "docker"

    def __init__(self, store):
        self.store = store
        self.reads = []

    def container_home(self):
        return Path("/container/home")

    def host_path(self, path):
        self.reads.append(path)
        return self.store / path.relative_to("/container/home")


def write(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def request(tokens, text="ok"):
    return {"type": "assistant", "message": {
        "content": text, "usage": {"input_tokens": tokens,
        "cache_read_input_tokens": 20, "cache_creation_input_tokens": 3}}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths(root)
    paths.ensure()
    (paths.config / "project.yaml").write_text("executor:\n  kind: docker\n")
    provider = Provider.from_dict("txp", {"bin": "true", "transcript": {
        "dir": "~/.claude/projects/{slug}", "glob": "*.jsonl",
        "usage_path": "message.usage", "context_fields": ["input_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens"],
        "limit_markers": [{"match": "session limit", "detail": "limited"}]}})
    executor = FakeDockerExecutor(tmp_path / "container-store")
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(root.resolve()))
    host = home / ".claude" / "projects" / slug / "sid.jsonl"
    container = executor.store / ".claude" / "projects" / slug / "sid.jsonl"
    write(host, {"type": "user", "message": {"content": "do the work"}},
          request(1200, "session limit"))
    write(container, request(8))
    spec = SimpleNamespace(provider="txp")
    config = SimpleNamespace(project={"executor": {"kind": "docker"}}, limits={
        "restart_attempts": 0, "context_wind_down_tokens": 1000})
    monkeypatch.setattr(driver, "_launched_spec", lambda *a: spec)
    return SimpleNamespace(paths=paths, provider=provider, executor=executor,
                           host=host, container=container, spec=spec, config=config)


def test_root_budget_context_reads_host_transcript(project, monkeypatch):
    p = project
    run = SimpleNamespace(
        paths=p.paths, config=p.config, providers={"txp": p.provider},
        session=lambda: "sid", self_depth=lambda: 0,
        executor=lambda *a: p.executor,
        tree=SimpleNamespace(read=lambda: {}, rollup_usage=lambda: {},
                             usage_by_model=lambda: {}),
        provider_slots=lambda: {}, spend_status=lambda: {})
    monkeypatch.setattr(server, "runner", lambda: run)
    monkeypatch.setattr(server.budget_mod, "read_all", lambda *a: {})
    monkeypatch.setattr(server, "billed_rows", lambda *a: [])
    assert server.budget_status()["context"] == {
        "known": True, "tokens": 1223, "wind_down_at": 1000,
        "compact_at": server._limit(run, "compact_at_tokens")}
    assert p.executor.reads == []


def test_depth_one_session_still_reads_docker_transcript(project, monkeypatch):
    p = project
    # This is the automatic executor lookup used for agent session reads.
    monkeypatch.setattr("multiagents.executor.executor_at", lambda *a: p.executor)
    assert session_context(p.provider, p.paths.root, "sid") == 31
    assert p.executor.reads
    run = SimpleNamespace(session=lambda: "sid", self_depth=lambda: 1)
    assert server._context_reading(run) is None


@pytest.mark.parametrize("limited", [False, True])
def test_driver_root_checks_read_host_file(project, monkeypatch, limited):
    p = project
    if not limited:
        # A container transcript contains no human turn; the host one does.
        write(p.host, {"type": "user", "message": {"content": "do the work"}},
              request(1200))
    monkeypatch.setattr("multiagents.executor.executor_at", lambda *a: p.executor)
    monkeypatch.setattr(driver, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(watchdog, "write_status", lambda *a: None)
    monkeypatch.setattr(driver, "_limit_stop", lambda *a, **k: 3)
    handed_over = []
    monkeypatch.setattr(driver, "_supervise", lambda *a, **k: handed_over.append(True) or 0)
    monkeypatch.setattr(driver.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(driver, "_AttachedCompaction", lambda *a: SimpleNamespace(
        launched=lambda: None, detached=lambda: None, attached=lambda *a: None,
        requested=None, release=lambda: None, due=lambda: False,
        limit_pending=lambda: None))

    def attached(*args, stalled, **kwargs):
        if limited:
            assert stalled() is False  # first poll warns
            assert stalled() is True   # second poll stops the root CLI
            stalled.stopping()
        return -signal.SIGHUP

    monkeypatch.setattr(driver, "_run_attached", attached)
    result = driver._run_supervised(p.paths, p.config, "orchestrator", p.spec,
                                    p.provider, p.executor, {}, [], {})
    assert result == (3 if limited else 0)
    assert handed_over == ([] if limited else [True])
    assert p.executor.reads == []
