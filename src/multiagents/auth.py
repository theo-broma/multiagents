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

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import scripts as _scripts

AUTHENTICATED = 0
NOT_AUTHENTICATED = 10
CHECK_TIMEOUT = 90


# Which stored login a check is asking about. Under the docker executor there
# are two of them and they are different accounts' worth of different: agents
# run inside the container against a container-private profile, and the
# ORCHESTRATOR runs on the host against the user's own — `run` execs the CLI
# locally whatever the executor is. A check that only ever saw one of them
# reported "logged in" for a profile the thing being launched does not use.
#
# "" means whichever one the executor implies, which is what every caller
# wanted before there were two and is still right for agents.
HOST = "host"


@dataclass
class AuthState:
    provider: str
    status: str                    # authenticated | not_authenticated | unknown | no_script
    detail: str = ""
    script: str = ""
    fix: str = ""
    profile: str = ""              # "" | "host"
    # PS-R3: set when this provider borrows its credentials, naming the owner.
    auth_from: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "authenticated"

    def to_dict(self) -> dict[str, Any]:
        out = {"provider": self.provider, "status": self.status, "authenticated": self.ok}
        if self.auth_from:
            out["auth_from"] = self.auth_from
        if self.profile:
            out["profile"] = self.profile
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
          config_dir: Path, project_config: Path | None = None,
          profile: str = "") -> AuthState:
    """Run a provider's `check` action. Never raises.

    `profile` names which stored login to ask about; see HOST above. It is
    passed to the script rather than interpreted here, because which profiles
    a provider even has is the script's business — the same reason `check`
    itself is a script and not a branch in this file.

    PS-R2/R3: a provider that borrows its credentials is checked through the
    OWNER's script and environment — that is where its login actually lives.
    The fix still names this provider unless the load attached the owner
    (`auth_from`), in which case it names the owner: logging in as the
    dependent would fix nothing.
    """
    owner_name = str(getattr(provider, "auth_from", "") or "")
    owner = getattr(provider, "auth_owner", None)
    if owner is not None and owner_name:
        pin = getattr(provider, "container_account", "")
        if pin and getattr(executor, "kind", "") == "docker" and profile != HOST:
            import copy
            provider = copy.copy(owner)
            provider.container_account = pin
        else:
            provider = owner
    script = _scripts.resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return AuthState(provider_name, "no_script",
                         detail=f"no script for provider {provider_name!r}",
                         script=getattr(provider, "script_name", ""),
                         profile=profile)

    code, out, err = _scripts.run_action(
        provider_name, provider, executor, "check", config_dir, project_config,
        timeout=CHECK_TIMEOUT,
        extra_env={"MULTIAGENTS_PROFILE": profile} if profile else None,
    )
    detail = (out.strip() or err.strip()).splitlines()
    line = detail[-1][:300] if detail else ""
    # The host profile is not repaired by the same command: under docker
    # `auth login <p>` signs into the CONTAINER, which is the whole confusion
    # this is here to end, so the fix has to name the other one.
    pinned = (getattr(provider, "container_account", "") and
              getattr(executor, "kind", "") == "docker" and profile != HOST)
    login_name = provider_name if pinned else owner_name or provider_name
    fix = (f"multiagents auth login {login_name}"
           + (" --host" if profile == HOST else ""))
    if code == AUTHENTICATED:
        return AuthState(provider_name, "authenticated", line, str(script),
                         profile=profile, auth_from=owner_name)
    if code == NOT_AUTHENTICATED:
        return AuthState(provider_name, "not_authenticated", line, str(script),
                         fix, profile, owner_name)
    return AuthState(provider_name, "unknown", line or f"exit {code}", str(script),
                     fix, profile, owner_name)


def check_all(providers: dict[str, Any], executor_for: Any,
              config_dir: Path, project_config: Path | None = None) -> dict[str, AuthState]:
    """Check every provider, credential groups sharing one check (PS-R3).

    Each credential OWNER is checked once; every provider that declares
    `auth_from` on it reports the owner's state and names it. A disabled
    owner is still checked — it holds the login its dependents use — and an
    owner missing from the map (a hand-built provider that never went through
    the loader) leaves its dependents unknown rather than checked as if they
    owned a login of their own.
    """
    states: dict[str, AuthState] = {}
    for name, provider in providers.items():
        if getattr(provider, "auth_from", ""):
            continue
        states[name] = check(name, provider, executor_for(name),
                             config_dir, project_config)
    for name, provider in providers.items():
        owner_name = str(getattr(provider, "auth_from", "") or "")
        if not owner_name:
            continue
        executor = executor_for(name)
        if getattr(provider, "container_account", "") and getattr(executor, "kind", "") == "docker":
            states[name] = check(name, provider, executor, config_dir, project_config)
            continue
        owner_state = states.get(owner_name)
        if owner_state is None:
            states[name] = AuthState(
                name, "unknown",
                detail=f"credential owner {owner_name!r} was not checked",
                fix=f"multiagents auth login {owner_name}", auth_from=owner_name)
            continue
        fix = (f"multiagents auth login {owner_name}"
               + (" --host" if owner_state.profile == HOST else ""))
        states[name] = replace(owner_state, provider=name, fix=fix,
                               auth_from=owner_name)
    return states


def login_command(provider_name: str, provider: Any, executor: Any,
                  config_dir: Path, project_config: Path | None = None,
                  profile: str = "", extra_env: dict[str, str] | None = None):
    """(argv, env) for the login action, or None if there is no script.

    Returned rather than run, because login may need the terminal and the
    caller should hand it over with execvpe rather than capture it.
    """
    env = dict(extra_env or {})
    owner = getattr(provider, "auth_owner", None)
    if (owner is not None and getattr(provider, "container_account", "")
            and getattr(executor, "kind", "") == "docker" and profile != HOST):
        import copy
        selected = copy.copy(owner)
        selected.container_account = getattr(provider, "container_account", "")
        provider = selected
    if profile:
        env["MULTIAGENTS_PROFILE"] = profile
    return _scripts.exec_action(provider_name, provider, executor, "login",
                                config_dir, project_config, extra_env=env or None)


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
