"""Q4 regression: `multiagents auth login codex` names the store it writes to.

The store the login lands in depends on the executor (the host profile under
the local executor, the private backing under docker), and an unlabelled
device flow can land credentials where the agents do not read them. Black
box, via `support/codex_harness.py`: HOME is a tmp dir and the native CLI is
the fake, so the expected profile path is known and the CODEX_HOME actually
handed to the CLI is recorded.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import codex_harness as h                                # noqa: E402


def store_line(stdout: str, store: Path, executor: str) -> str:
    """The one line naming `store`; raises if the output does not name it."""
    lines = [l for l in stdout.splitlines() if str(store) in l]
    assert len(lines) == 1, f"no single line naming {store}: {stdout!r}"
    assert executor in lines[0], f"executor {executor} not named: {lines[0]!r}"
    return lines[0]


def test_q4_login_names_the_default_profile_under_home(tmp_path):
    fake = h.FakeCodex(tmp_path)
    expected = tmp_path / "home" / ".multiagents" / "profiles" / "codex"
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=None)
    result = h.invoke(["login"], env)
    assert result.returncode == 0, result.stderr
    store_line(result.stdout, expected, "local")
    # The named store is the one the login actually used.
    logins = [c for c in fake.calls() if "login" in c["argv"] and "status" not in c["argv"]]
    assert len(logins) == 1 and logins[0]["codex_home"] == str(expected)
    assert "--device-auth" in logins[0]["argv"]


def test_q4_login_names_the_profile_override(tmp_path):
    fake = h.FakeCodex(tmp_path)
    second = tmp_path / "accounts" / "second"
    env = h.base_env(tmp_path, fake, MULTIAGENTS_CODEX_PROFILE=str(second))
    result = h.invoke(["login"], env)
    assert result.returncode == 0, result.stderr
    store_line(result.stdout, second, "local")
    assert {c["codex_home"] for c in fake.calls()} == {str(second)}


def test_q4_login_names_the_docker_backing(tmp_path):
    fake = h.FakeCodex(tmp_path)
    backing = tmp_path / "backing"
    env = h.base_env(tmp_path, fake, MULTIAGENTS_EXECUTOR="docker",
                     MULTIAGENTS_PRIVATE_BACKING=str(backing),
                     MULTIAGENTS_PRIVATE_HOME="/home/agent/.codex")
    result = h.invoke(["login"], env)
    assert result.returncode == 0, result.stderr
    store_line(result.stdout, backing, "docker")
    assert {c["codex_home"] for c in fake.calls()} == {str(backing)}
