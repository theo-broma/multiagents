"""Uniform authentication across providers.

Every CLI reports "not authenticated" differently and is fixed differently:
``claude auth login``, ``opencode providers login``, and for agy an interactive
session inside its container. Rather than special-casing each one in Python,
each provider ships a small script implementing one contract, and everything
above this line — doctor, the CLI, the MCP tool, and the runner's failure
classifier — treats them identically.

Adding a provider therefore stays what it should be: a block in
``providers.yaml`` plus a script beside it.

**The contract** — ``auth/<provider>.sh <action>``:

``check``
    Non-interactive and fast. Exit ``0`` authenticated, ``10`` not
    authenticated, anything else unknown. One line of human-readable status on
    stdout.

``login``
    May be interactive and may take over the terminal. Print what the user has
    to do before doing it. Exit ``0`` on success.

Both receive the situation through the environment: ``MULTIAGENTS_PROVIDER``,
``MULTIAGENTS_BIN``, ``MULTIAGENTS_EXECUTOR`` (local or docker),
``MULTIAGENTS_CONTAINER``, ``MULTIAGENTS_PRIVATE_HOME``, ``MULTIAGENTS_PROJECT``
and ``MULTIAGENTS_UID`` / ``MULTIAGENTS_GID``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUTHENTICATED = 0
NOT_AUTHENTICATED = 10
CHECK_TIMEOUT = 90


@dataclass
class AuthState:
    provider: str
    status: str                    # authenticated | not_authenticated | unknown | no_script
    detail: str = ""
    script: str = ""
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "authenticated"

    def to_dict(self) -> dict[str, Any]:
        out = {"provider": self.provider, "status": self.status, "authenticated": self.ok}
        if self.detail:
            out["detail"] = self.detail
        if not self.ok and self.fix:
            out["fix"] = self.fix
        return out


def script_dirs(config_dir: Path, project_config: Path | None) -> list[Path]:
    """Project scripts win over global ones, which win over the shipped ones."""
    dirs = [Path(__file__).parent / "defaults" / "auth", config_dir / "auth"]
    if project_config is not None:
        dirs.append(project_config / "auth")
    return dirs


def find_script(name: str, config_dir: Path, project_config: Path | None) -> Path | None:
    for base in reversed(script_dirs(config_dir, project_config)):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def build_env(provider_name: str, provider: Any, executor: Any) -> dict[str, str]:
    env = dict(os.environ)
    binary = getattr(provider, "available", lambda: None)() or getattr(provider, "bin", "")
    env.update({
        "MULTIAGENTS_PROVIDER": provider_name,
        "MULTIAGENTS_BIN": str(binary or ""),
        "MULTIAGENTS_EXECUTOR": getattr(executor, "kind", "local"),
        "MULTIAGENTS_UID": str(os.getuid()),
        "MULTIAGENTS_GID": str(os.getgid()),
    })
    if getattr(executor, "kind", "local") == "docker":
        env["MULTIAGENTS_CONTAINER"] = executor.container
        private = executor.private_state()
        # The path the CLI will see as its home-relative state, and where that
        # actually lives on the host — a login script needs both.
        for container_path, host_path in private.items():
            env["MULTIAGENTS_PRIVATE_HOME"] = str(container_path)
            env["MULTIAGENTS_PRIVATE_BACKING"] = str(host_path)
            break
    return env


def check(provider_name: str, provider: Any, executor: Any,
          config_dir: Path, project_config: Path | None = None) -> AuthState:
    """Run a provider's `check` action. Never raises."""
    spec = (getattr(provider, "auth", None) or {})
    name = spec.get("script") or f"{provider_name}.sh"
    script = find_script(name, config_dir, project_config)
    if script is None:
        return AuthState(provider_name, "no_script",
                         detail=f"no auth script {name!r} found", script=name)

    try:
        result = subprocess.run(
            ["sh", str(script), "check"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT,
            env=build_env(provider_name, provider, executor),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return AuthState(provider_name, "unknown", detail=f"{type(exc).__name__}: {exc}",
                         script=str(script))

    detail = (result.stdout.strip() or result.stderr.strip()).splitlines()
    line = detail[-1][:300] if detail else ""
    fix = f"multiagents auth login {provider_name}"
    if result.returncode == AUTHENTICATED:
        return AuthState(provider_name, "authenticated", line, str(script))
    if result.returncode == NOT_AUTHENTICATED:
        return AuthState(provider_name, "not_authenticated", line, str(script), fix)
    return AuthState(provider_name, "unknown", line or f"exit {result.returncode}",
                     str(script), fix)


def check_all(providers: dict[str, Any], executor_for: Any,
              config_dir: Path, project_config: Path | None = None) -> dict[str, AuthState]:
    return {
        name: check(name, provider, executor_for(name), config_dir, project_config)
        for name, provider in providers.items()
    }


def login_command(provider_name: str, provider: Any, executor: Any,
                  config_dir: Path, project_config: Path | None = None):
    """(argv, env) for the login action, or None if there is no script.

    Returned rather than run, because login may need the terminal and the
    caller should hand it over with execvp rather than capture it.
    """
    spec = (getattr(provider, "auth", None) or {})
    name = spec.get("script") or f"{provider_name}.sh"
    script = find_script(name, config_dir, project_config)
    if script is None:
        return None
    return ["sh", str(script), "login"], build_env(provider_name, provider, executor)


# --------------------------------------------------------------------------
# Recognising an authentication failure in a run's output
# --------------------------------------------------------------------------

_AUTH_MARKERS = (
    "authentication required", "not authenticated", "unauthenticated",
    "please log in", "please login", "run 'agy' to log in", "log in, then retry",
    "providers login", "auth login", "invalid api key", "invalid_api_key",
    "unauthorized", "401", "authentication failed", "credentials not found",
    "no credentials", "token expired", "oauth token",
)

# Phrases that look like auth failures but are not — a tool being denied
# permission inside the agent is a different problem with a different fix.
_NOT_AUTH = ("permission", "auto-denied", "dangerously-skip")


def looks_like_auth_failure(status: str, stderr: str, text: str = "") -> bool:
    blob = f"{status}\n{stderr}\n{text}".lower()
    if not any(marker in blob for marker in _AUTH_MARKERS):
        return False
    # A permission denial mentions neither credentials nor logging in; if the
    # only signal is a permission phrase, this is not an auth problem.
    if any(marker in blob for marker in _NOT_AUTH) and \
       not any(m in blob for m in ("log in", "login", "credential", "token", "unauthorized")):
        return False
    return True
