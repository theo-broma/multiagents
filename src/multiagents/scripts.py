"""The per-provider script contract.

Every CLI differs in how it reports authentication, how it is repaired, how its
quota is read, and how it is launched as an orchestrator. Rather than a growing
pile of per-CLI branches in Python, each provider ships **one** script
implementing a small set of actions, and everything above this module treats
them identically. Adding a provider stays what it should be: a block in
``providers.yaml`` plus one script beside it.

Actions
-------

``check``    non-interactive. exit 0 authenticated, 10 not, else unknown.
             One line of human-readable status on stdout.
``login``    may be interactive and take the terminal. Prints what the user must
             do *before* doing it.
``budget``   non-interactive. Prints one JSON object describing quota headroom.
             Optional — a provider with no readable quota simply omits it.
``prepare``  idempotently register the MCP server for this CLI. (Phase 4)
``launch``   exec the CLI interactively as an orchestrator. (Phase 4)

Captured actions (``check``, ``budget``) are run and their output read. Handed-
over actions (``login``, ``launch``) return an argv and environment for the
caller to ``execvpe``, because they need the terminal.

Scripts resolve project-first, then global, then the shipped copies, so a
project can override one provider's behaviour without touching the machine.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

# Actions that must never take longer than a moment: they run on the hot path.
CAPTURE_TIMEOUT = 20
LOGIN_TIMEOUT = 900

AUTHENTICATED = 0
NOT_AUTHENTICATED = 10
UNIMPLEMENTED = 64


def script_dirs(config_dir: Path, project_config: Path | None) -> list[Path]:
    """Search path, lowest priority first.

    The legacy ``auth/`` directory is still searched, so an install predating
    the rename keeps working — but it always loses to ``providers/`` in the same
    layer, or a stale script would shadow the current one and silently drop its
    newer actions.
    """
    package = Path(__file__).parent / "defaults"
    dirs = [package / "auth", package / "providers",
            config_dir / "auth", config_dir / "providers"]
    if project_config is not None:
        dirs += [project_config / "auth", project_config / "providers"]
    # Lowest priority first; find_script walks this reversed. Within a layer
    # `providers/` beats `auth/`, or a legacy script left behind by an older
    # install would shadow the current one and silently lose its new actions.
    return dirs


def find_script(name: str, config_dir: Path,
                project_config: Path | None) -> Path | None:
    for base in reversed(script_dirs(config_dir, project_config)):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def build_env(provider_name: str, provider: Any, executor: Any,
              extra: dict[str, str] | None = None) -> dict[str, str]:
    """The situation, handed to the script through the environment."""
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
        for container_path, host_path in (executor.private_state() or {}).items():
            env["MULTIAGENTS_PRIVATE_HOME"] = str(container_path)
            env["MULTIAGENTS_PRIVATE_BACKING"] = str(host_path)
            break
    env.update(extra or {})
    return env


def resolve(provider_name: str, provider: Any, config_dir: Path,
            project_config: Path | None = None) -> Path | None:
    name = getattr(provider, "script_name", None) or f"{provider_name}.sh"
    return find_script(name, config_dir, project_config)


def run_action(provider_name: str, provider: Any, executor: Any, action: str,
               config_dir: Path, project_config: Path | None = None,
               timeout: int = CAPTURE_TIMEOUT,
               extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a captured action. Returns ``(returncode, stdout, stderr)``.

    Never raises: a missing script, a timeout or an OS error all come back as a
    non-zero code with the reason in stderr, because every caller of this is
    reporting status rather than doing work.
    """
    script = resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return 127, "", f"no script for provider {provider_name!r}"
    try:
        result = subprocess.run(
            ["sh", str(script), action],
            capture_output=True, text=True, timeout=timeout,
            env=build_env(provider_name, provider, executor, extra_env),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


def exec_action(provider_name: str, provider: Any, executor: Any, action: str,
                config_dir: Path, project_config: Path | None = None,
                extra_env: dict[str, str] | None = None):
    """``(argv, env)`` for an action that needs the terminal, or ``None``."""
    script = resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return None
    return (["sh", str(script), action],
            build_env(provider_name, provider, executor, extra_env))
