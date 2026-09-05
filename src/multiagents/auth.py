"""Uniform authentication across providers.

Every CLI reports "not authenticated" differently and is fixed differently:
``claude auth login``, ``opencode providers login``, and for agy an interactive
session inside its container. Rather than special-casing each one in Python,
each provider ships a small script implementing one contract, and everything
above this line — doctor, the CLI, the MCP tool, and the runner's failure
classifier — treats them identically.

Adding a provider therefore stays what it should be: a block in
``providers.yaml`` plus a script beside it.

This module owns only the auth-specific parts — the ``check`` and ``login``
actions, the exit-code mapping, and recognising an auth failure in an agent's
output. The script contract itself, shared with budget and orchestration, lives
in :mod:`multiagents.scripts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import scripts as _scripts

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


# Plumbing lives in scripts.py; this module keeps only what is auth-specific.
script_dirs = _scripts.script_dirs
find_script = _scripts.find_script
build_env = _scripts.build_env


def check(provider_name: str, provider: Any, executor: Any,
          config_dir: Path, project_config: Path | None = None) -> AuthState:
    """Run a provider's `check` action. Never raises."""
    script = _scripts.resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return AuthState(provider_name, "no_script",
                         detail=f"no script for provider {provider_name!r}",
                         script=getattr(provider, "script_name", ""))

    code, out, err = _scripts.run_action(
        provider_name, provider, executor, "check", config_dir, project_config,
        timeout=CHECK_TIMEOUT,
    )
    detail = (out.strip() or err.strip()).splitlines()
    line = detail[-1][:300] if detail else ""
    fix = f"multiagents auth login {provider_name}"
    if code == AUTHENTICATED:
        return AuthState(provider_name, "authenticated", line, str(script))
    if code == NOT_AUTHENTICATED:
        return AuthState(provider_name, "not_authenticated", line, str(script), fix)
    return AuthState(provider_name, "unknown", line or f"exit {code}", str(script), fix)


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
    caller should hand it over with execvpe rather than capture it.
    """
    return _scripts.exec_action(provider_name, provider, executor, "login",
                                config_dir, project_config)


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


def looks_like_auth_failure(status: str, stderr: str) -> bool:
    """Did the CLI itself report an authentication failure?

    Reads only the run's own failure channels, never the agent's output — an
    agent writing *about* authentication must not be recorded as unauthenticated.
    """
    blob = f"{status}\n{stderr}".lower()
    if not any(marker in blob for marker in _AUTH_MARKERS):
        return False
    # A permission denial mentions neither credentials nor logging in; if the
    # only signal is a permission phrase, this is not an auth problem.
    if any(marker in blob for marker in _NOT_AUTH) and \
       not any(m in blob for m in ("log in", "login", "credential", "token", "unauthorized")):
        return False
    return True
