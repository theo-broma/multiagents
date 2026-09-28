"""Codex provider contract: the dedicated profile and auth actions (CX-C8, CX-C9).

Black box: the adapter runs as an executable with an explicit environment,
and the fake native CLI records the CODEX_HOME it was handed. See
`support/codex_harness.py`.

Profile rule, as amended (CX-C8 revised, CX-C9 amended):
- agent runs: docker → `$MULTIAGENTS_PRIVATE_HOME` (the private backing, as
  mounted); refused with a `codex:` line when that is missing, never
  `$HOME/.codex` (CX-C8 revised again, "Live results");
  otherwise `$MULTIAGENTS_CODEX_PROFILE`, else `~/.multiagents/profiles/codex`;
- `check` and `login`: the private backing (`$MULTIAGENTS_PRIVATE_BACKING`)
  when the executor is docker and `MULTIAGENTS_PROFILE` is not `host`, the
  host profile otherwise;
- an ambient CODEX_HOME is ignored and overwritten.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                              # noqa: E402
from multiagents.auth import looks_like_auth_failure                # noqa: E402
from multiagents.providers import RESULT                            # noqa: E402

HAPPY = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]

# What Codex says when a stored ChatGPT token cannot be refreshed. The wording
# is Codex's own (codex-rs auth); see the NEED_INFO in the tester's result.
REFRESH_EXPIRED = ("Your access token could not be refreshed because your refresh "
                   "token has expired. Please log out and sign in again.")


@pytest.fixture
def fake(tmp_path):
    return h.FakeCodex(tmp_path)


def run_agent(tmp_path, fake, env):
    prov = h.provider()
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    argv = prov.build_command(prompt="p", model="m", workdir=str(workdir), permission="sandbox")
    return prov, h.invoke(argv[1:], env, cwd=workdir)


def default_profile(tmp_path) -> Path:
    return tmp_path / "home" / ".multiagents" / "profiles" / "codex"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


# ------------------------------------------------------------------ CX-C8 --

@pytest.mark.parametrize("action", ["check", "run"])
def test_cx_c8_default_profile_is_created_0700_and_used(tmp_path, fake, action):
    fake.set(events=HAPPY, status="logged_in")
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=None)
    env.pop("MULTIAGENTS_CODEX_PROFILE", None)
    if action == "check":
        result = h.invoke(["check"], env)
    else:
        _, result = run_agent(tmp_path, fake, env)
    assert result.returncode == 0, result.stderr
    profile = default_profile(tmp_path)
    assert profile.is_dir()
    assert mode(profile) == 0o700
    homes = {c["codex_home"] for c in fake.calls()}
    assert homes == {str(profile)}


def test_cx_c8_codex_profile_variable_overrides_the_default(tmp_path, fake):
    fake.set(events=HAPPY)
    second = tmp_path / "accounts" / "second"
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(second))
    _, result = run_agent(tmp_path, fake, env)
    assert result.returncode == 0, result.stderr
    assert second.is_dir() and mode(second) == 0o700
    assert {c["codex_home"] for c in fake.calls()} == {str(second)}
    assert not default_profile(tmp_path).exists()


@pytest.mark.parametrize("action", ["check", "run"])
def test_cx_c8_ambient_codex_home_is_ignored(tmp_path, fake, action):
    fake.set(events=HAPPY, status="logged_in")
    ambient = tmp_path / "ambient-codex-home"
    env = h.base_env(tmp_path, fake, CODEX_HOME=str(ambient))
    if action == "check":
        result = h.invoke(["check"], env)
    else:
        _, result = run_agent(tmp_path, fake, env)
    assert result.returncode == 0, result.stderr
    assert {c["codex_home"] for c in fake.calls()} == {str(tmp_path / "profile")}
    assert not ambient.exists()


def test_cx_c8_docker_run_uses_the_private_home(tmp_path, fake):
    # CX-C8 revised again: an agent's HOME is its own per-agent home, not the
    # user's, so the backing is found at MULTIAGENTS_PRIVATE_HOME, never at
    # $HOME/.codex. HOME, the private home and the backing are all distinct.
    fake.set(events=HAPPY)
    private_home = tmp_path / "private-home" / ".codex"
    private_home.mkdir(parents=True)
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                     MULTIAGENTS_PRIVATE_HOME=str(private_home),
                     MULTIAGENTS_PRIVATE_BACKING=str(tmp_path / "backing"),
                     CODEX_HOME=str(tmp_path / "ambient"))
    assert env["HOME"] != str(private_home.parent)
    _, result = run_agent(tmp_path, fake, env)
    assert result.returncode == 0, result.stderr
    assert {c["codex_home"] for c in fake.calls()} == {str(private_home)}


def test_cx_c8_docker_run_without_private_home_is_refused(tmp_path, fake):
    # CX-C8 revised again: missing MULTIAGENTS_PRIVATE_HOME under docker is a
    # refusal with a `codex:` line, and never a fallback to $HOME/.codex, even
    # when that directory exists and looks logged in.
    fake.set(events=HAPPY)
    own = tmp_path / "home" / ".codex"
    own.mkdir(parents=True)
    (own / "auth.json").write_text('{"tokens": "' + h.SECRET + '"}')
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                     MULTIAGENTS_PRIVATE_BACKING=str(tmp_path / "backing"))
    assert "MULTIAGENTS_PRIVATE_HOME" not in env
    _, result = run_agent(tmp_path, fake, env)
    assert result.returncode != 0
    assert any(line.startswith("codex:") for line in result.stderr.splitlines()), result.stderr
    assert "Traceback" not in result.stderr
    assert fake.exec_calls() == []
    assert all(c["codex_home"] != str(own) for c in fake.calls())


def test_cx_c8_local_actions_never_touch_the_users_own_codex_dir(tmp_path, fake):
    fake.set(events=HAPPY, status="logged_in")
    own = tmp_path / "home" / ".codex"
    (own / "sessions" / "2026" / "09" / "28").mkdir(parents=True)
    (own / "auth.json").write_text('{"tokens": "' + h.SECRET + '"}')
    (own / "models_cache.json").write_text('{"models": []}')
    h.write_rollout(own, "mine", [h.token_count_line(
        time.time() - 10, h.window(50.0, 300, 4102444800),
        h.window(50.0, 10080, 4102444800))])
    before = snapshot(own)
    env = h.base_env(tmp_path, fake)
    _, run = run_agent(tmp_path, fake, env)
    assert run.returncode == 0, run.stderr
    h.invoke(["check"], env)
    h.invoke(["models"], env)
    h.invoke(["budget"], env)
    assert snapshot(own) == before
    assert all(c["codex_home"] != str(own) for c in fake.calls())


# ------------------------------------------------------------------ CX-C9 --

@pytest.mark.parametrize("status,code", [
    ("logged_in", 0), ("not_logged_in", 10), ("refresh_failed", 10)])
def test_cx_c9_check_exit_codes(tmp_path, fake, status, code):
    fake.set(status=status, refresh_message=REFRESH_EXPIRED)
    result = h.invoke(["check"], h.base_env(tmp_path, fake))
    assert result.returncode == code, (result.stdout, result.stderr)


def test_cx_c9_check_is_an_error_when_the_native_cli_is_missing(tmp_path, fake):
    env = h.base_env(tmp_path, fake, MULTIAGENTS_BIN=str(tmp_path / "nowhere" / "codex"))
    result = h.invoke(["check"], env)
    assert result.returncode == 20
    assert "Traceback" not in result.stderr


def test_cx_c9_check_times_out_as_an_error(tmp_path, fake):
    # 15 s timeout (CX-C9). This test takes about that long by design.
    fake.set(status="hang")
    result = h.invoke(["check"], h.base_env(tmp_path, fake), timeout=40)
    assert result.returncode == 20


@pytest.mark.parametrize("status", ["logged_in", "not_logged_in", "refresh_failed"])
def test_cx_c9_check_never_prints_the_cli_output(tmp_path, fake, status):
    fake.set(status=status, refresh_message=REFRESH_EXPIRED)
    result = h.invoke(["check"], h.base_env(tmp_path, fake))
    assert h.SECRET not in result.stdout + result.stderr
    assert "Logged in using" not in result.stdout + result.stderr
    assert len(result.stdout.strip().splitlines()) <= 1


@pytest.mark.parametrize("action", ["check", "login"])
def test_cx_c9_docker_uses_the_private_backing(tmp_path, fake, action):
    fake.set(status="logged_in")
    backing = tmp_path / "backing"
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                     MULTIAGENTS_PRIVATE_BACKING=str(backing),
                     MULTIAGENTS_PRIVATE_HOME="/home/agent/.codex")
    h.invoke([action], env)
    homes = {c["codex_home"] for c in fake.calls()}
    assert homes == {str(backing)}


@pytest.mark.parametrize("action", ["check", "login"])
def test_cx_c9_docker_with_profile_host_uses_the_host_profile(tmp_path, fake, action):
    fake.set(status="logged_in")
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                     MULTIAGENTS_PRIVATE_BACKING=str(tmp_path / "backing"),
                     MULTIAGENTS_PROFILE="host")
    h.invoke([action], env)
    assert {c["codex_home"] for c in fake.calls()} == {str(tmp_path / "profile")}


@pytest.mark.parametrize("action", ["check", "login"])
def test_cx_c9_local_ignores_a_private_backing(tmp_path, fake, action):
    fake.set(status="logged_in")
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="local",
                     MULTIAGENTS_PRIVATE_BACKING=str(tmp_path / "backing"))
    h.invoke([action], env)
    assert {c["codex_home"] for c in fake.calls()} == {str(tmp_path / "profile")}


def test_cx_c9_login_is_a_device_login_into_the_profile(tmp_path, fake):
    env = h.base_env(tmp_path, fake)
    result = h.invoke(["login"], env)
    assert result.returncode == 0, result.stderr
    logins = [c for c in fake.calls() if "login" in c["argv"] and "status" not in c["argv"]]
    assert len(logins) == 1
    assert "--device-auth" in logins[0]["argv"]
    assert logins[0]["codex_home"] == str(tmp_path / "profile")
    assert (tmp_path / "profile").is_dir()


def test_cx_c9_refresh_failure_in_a_run_reports_unauthenticated(tmp_path, fake):
    fake.set(events=[{"type": "thread.started", "thread_id": "t"},
                     {"type": "turn.started"},
                     {"type": "error", "message": REFRESH_EXPIRED},
                     {"type": "turn.failed", "error": {"message": REFRESH_EXPIRED}}],
             exit=1)
    prov, result = run_agent(tmp_path, fake, h.base_env(tmp_path, fake))
    assert result.returncode != 0
    assert h.AUTH_LINE in result.stderr.splitlines()
    assert looks_like_auth_failure("failed", result.stderr)
    results = [e for e in h.events(prov, result.stdout) if e.kind == RESULT]
    assert results and results[-1].status != "success"


def test_cx_c9_an_ordinary_failure_is_not_reported_as_unauthenticated(tmp_path, fake):
    fake.set(events=[{"type": "turn.failed", "error": {"message": "model overloaded"}}], exit=1)
    _, result = run_agent(tmp_path, fake, h.base_env(tmp_path, fake))
    assert result.returncode != 0
    assert h.AUTH_LINE not in result.stderr
    assert not looks_like_auth_failure("failed", result.stderr)
