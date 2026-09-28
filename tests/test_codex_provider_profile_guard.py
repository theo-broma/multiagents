"""Codex provider contract: CX-D3 — refuse a profile that resolves to the
user's own `~/.codex`.

Decision (context/specs/codex-provider.md, "The attack on the adapter"): a
`MULTIAGENTS_CODEX_PROFILE` that resolves to the user's real `~/.codex` is
refused with a `codex:`-prefixed stderr line, never silently used. Black box,
as the other `test_codex_provider*` files: see `support/codex_harness.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402

HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodex(tmp_path)


def run_agent(tmp_path, fake, env):
    prov = h.provider()
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    argv = prov.build_command(prompt="p", model="m", workdir=str(workdir), permission="sandbox")
    return h.invoke(argv[1:], env, cwd=workdir)


@pytest.mark.parametrize("action", ["check", "run"])
def test_cx_d3_a_profile_that_resolves_to_the_users_own_codex_home_is_refused(
        tmp_path, fake, action):
    fake.set(events=HAPPY, status="logged_in")
    own = tmp_path / "home" / ".codex"
    (own / "sessions").mkdir(parents=True)
    (own / "auth.json").write_text('{"tokens": "' + h.SECRET + '"}')
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(own))
    if action == "check":
        result = h.invoke(["check"], env)
    else:
        result = run_agent(tmp_path, fake, env)
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    assert result.returncode != 0
    assert h.SECRET not in result.stdout + result.stderr
    assert fake.calls() == []
    assert any(line.startswith("codex:") for line in result.stderr.splitlines()), result.stderr
