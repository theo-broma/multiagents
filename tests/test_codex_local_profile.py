"""Under the local executor an agent's codex run must use the host profile
(the one `auth login codex` / `check` use), not one under the agent's own HOME.

`support/codex_harness.base_env` always sets MULTIAGENTS_CODEX_PROFILE, which
hid this: here the profile variable is absent and HOME is the agent's.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402
from multiagents.executor.base import build_env                     # noqa: E402

HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]


def _agent_env(tmp_path, monkeypatch, fake):
    """The env an agent gets: real HOME in the process, per-agent HOME in the child."""
    real = tmp_path / "realhome"
    real.mkdir()
    agent = tmp_path / "agenthome"
    agent.mkdir()
    monkeypatch.setenv("HOME", str(real))
    monkeypatch.delenv("MULTIAGENTS_CODEX_PROFILE", raising=False)
    env = build_env(passthrough=[], blocked=[], home=agent, identity={})
    env.update({"MULTIAGENTS_EXECUTOR": "local", "MULTIAGENTS_PROVIDER": "codex",
                "MULTIAGENTS_BIN": str(fake.path),
                "PATH": h.base_env(tmp_path, None)["PATH"]})
    assert env["HOME"] == str(agent)
    return env, real, agent


def _run(tmp_path, env):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    argv = h.provider().build_command(prompt="p", model="m", workdir=str(workdir),
                                      permission="sandbox")
    return h.invoke(argv[1:], env, cwd=workdir)


def test_run_uses_host_profile_not_agent_home(tmp_path, monkeypatch):
    fake = h.FakeCodex(tmp_path)
    fake.set(events=HAPPY, status="logged_in")
    env, real, agent = _agent_env(tmp_path, monkeypatch, fake)
    profile = real / ".multiagents" / "profiles" / "codex"
    profile.mkdir(parents=True)
    (profile / "auth.json").write_text("{}")
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stderr[-2000:]
    assert not (agent / ".multiagents").exists()
    assert fake.exec_calls(), "codex was never invoked"
    assert fake.exec_calls()[0]["codex_home"] == str(profile)


def test_missing_auth_fails_fast_naming_path(tmp_path, monkeypatch):
    fake = h.FakeCodex(tmp_path)
    fake.set(events=HAPPY, status="logged_in")
    env, real, agent = _agent_env(tmp_path, monkeypatch, fake)
    result = _run(tmp_path, env)
    profile = real / ".multiagents" / "profiles" / "codex"
    assert result.returncode != 0
    assert str(profile) in result.stderr
    assert fake.calls() == []
    assert not (agent / ".multiagents").exists()


def test_explicit_profile_var_still_wins_and_own_codex_refused(tmp_path, monkeypatch):
    fake = h.FakeCodex(tmp_path)
    fake.set(events=HAPPY, status="logged_in")
    env, real, agent = _agent_env(tmp_path, monkeypatch, fake)
    own = real / ".codex"
    own.mkdir()
    (own / "auth.json").write_text("{}")
    env["MULTIAGENTS_CODEX_PROFILE"] = str(own)
    result = _run(tmp_path, env)
    assert result.returncode != 0
    assert fake.calls() == []
    assert any(l.startswith("codex:") for l in result.stderr.splitlines()), result.stderr
