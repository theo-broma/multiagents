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
``usage``    non-interactive. Prints the human-readable lines the monitor shows
             for this provider, receiving the parsed budget as
             ``MULTIAGENTS_BUDGET`` so it formats rather than re-fetches.
             Optional: quotas have genuinely different shapes — two rolling
             windows and a credit pool here, three windows there, nothing at
             all somewhere else — and a provider that has something particular
             to say should say it rather than be flattened into one bar.
``identity`` non-interactive. Prints only {"identity": "...", "kind":
             "email|account|org"}, or exits 64 when unknown. Credential files
             may be read internally, but tokens, keys and their fragments must
             never appear in stdout or stderr. The monitor caches the scalar
             in memory and reveals it only on an authenticated request.
``prepare``  idempotently register the MCP server for this CLI. (Phase 4)
``launch``   exec the CLI interactively as an orchestrator. (Phase 4)

Captured actions (``check``, ``budget``) are run and their output read. Handed-
over actions (``login``, ``launch``) return an argv and environment for the
caller to ``execvpe``, because they need the terminal.

Scripts resolve project-first, then global, then the shipped copies, so a
project can override one provider's behaviour without touching the machine.

A script need not be shell. The file named by ``script:`` in ``providers.yaml``
is run by :func:`script_argv`, which sends a ``.sh`` to ``sh`` and lets anything
else run itself — so a provider whose quota lives behind a JSON API can be
``myprovider.py``, or a compiled binary, without a line of Python in this
package. That is what keeps the promise in ``providers.yaml``'s header honest:
the three shipped scripts reach for inline ``python3 -c`` heredocs precisely
because shell was the only language the contract accepted.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .safepoint import strip_key
from .providers import Provider, expand_env_value, resolved_profile

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
    env.update({
        "MULTIAGENTS_PROVIDER": provider_name,
        "MULTIAGENTS_EXECUTOR": getattr(executor, "kind", "local"),
        "MULTIAGENTS_UID": str(os.getuid()),
        "MULTIAGENTS_GID": str(os.getgid()),
    })
    if getattr(executor, "kind", "local") == "docker":
        env["MULTIAGENTS_CONTAINER"] = executor.container
        # THIS provider's private home, not whichever one the executor happened
        # to list first. With one provider using it the difference never showed;
        # with two, a script would have been handed the other one's directory.
        try:
            private = executor.private_state(provider_name) or {}
        except TypeError:                     # an executor from before the filter
            private = executor.private_state() or {}
        for container_path, host_path in private.items():
            env["MULTIAGENTS_PRIVATE_HOME"] = str(container_path)
            env["MULTIAGENTS_PRIVATE_BACKING"] = str(host_path)
            break
        # Where the REAL credential lives: a host-only profile the container
        # cannot see. BACKING is what agents read; VAULT is what can mint it.
        vault = getattr(executor, "vault_state", lambda _n="": {})(provider_name)
        if vault:
            env["MULTIAGENTS_PRIVATE_VAULT"] = str(next(iter(vault.values())))
        if getattr(executor, "auth_proxy_enabled", lambda: False)():
            env["MULTIAGENTS_AUTH_PROXY"] = "1"
    resolver = provider
    if not hasattr(resolver, "resolve_bin"):
        resolver = Provider.from_dict(provider_name, {
            "bin": getattr(provider, "bin", provider_name),
            "bin_search": getattr(provider, "bin_search", []),
        })
    resolved = resolver.resolve_bin(env=env)
    # H7 PS-R2a: invocation uses the launcher path, symlinks not resolved.
    # PS-R2b: set before the provider's own `env:` so a provider value wins.
    env["MULTIAGENTS_BIN"] = str(resolved.launcher) if resolved.launcher is not None else ""
    env["MULTIAGENTS_BIN_ERROR"] = "" if resolved.launcher is not None else resolver.bin_error(resolved)
    # The instance's own environment, expanded. This is what separates two
    # accounts on one CLI, so it is applied to EVERY action: a `check` that
    # inspects profile A while `launch` runs as profile B would report on an
    # account nobody is using.
    #
    # PS-R2: a provider that borrows its credentials runs with the OWNER's
    # environment overlaid with its own — `credential_env`, attached at config
    # load. Providers without the key keep their own `env`, exactly as before.
    env_map = getattr(provider, "credential_env", None) or \
        (getattr(provider, "env", None) or {})
    for key, value in env_map.items():
        env[key] = expand_env_value(value)
    # The account profile is resolved by the same helper the budget reader
    # uses, so the CLI and the reader can never be pointed at different
    # directories — including a relative value, which the helper makes
    # absolute rather than leaving it to each process's cwd.
    profile_field = getattr(provider, "budget_profile_env", "") or ""
    profile = resolved_profile(provider)
    if profile_field and profile:
        env[profile_field] = profile
    # A host auth request belongs to one action, never to this process's
    # later budget reads. Only an explicit caller may select that profile.
    env.pop("MULTIAGENTS_PROFILE", None)
    env.update(extra or {})
    # The executor object is authoritative. An ambient or provider variable,
    # or an action overlay, cannot turn a container read into a host read.
    # Missing executor information remains unknown rather than implying local.
    env["MULTIAGENTS_EXECUTOR"] = getattr(executor, "kind", "") or ""
    if env["MULTIAGENTS_EXECUTOR"] == "docker":
        # Checks and login select the same signed-routing account as launches.
        # Pins from config outrank ambient, provider and action variables.
        env.pop("MULTIAGENTS_CONTAINER_ACCOUNT", None)
        pin = (getattr(provider, "container_account", "") or
               getattr(getattr(provider, "auth_owner", None), "container_account", ""))
        if pin:
            env["MULTIAGENTS_CONTAINER_ACCOUNT"] = pin
        pins = getattr(executor, "account_pins", lambda: {})()
        env["MULTIAGENTS_RESERVED_ACCOUNTS"] = json.dumps(sorted(set(pins.values())))
    # A rejected docker layout is status, not an exception from a read-only
    # environment lookup. Action boundaries below refuse before running code.
    env.pop("MULTIAGENTS_CONFIG_ERROR", None)
    problems = getattr(executor, "configuration_problems", lambda: [])()
    if problems:
        for key in ("MULTIAGENTS_PRIVATE_HOME", "MULTIAGENTS_PRIVATE_BACKING",
                    "MULTIAGENTS_PRIVATE_VAULT"):
            env.pop(key, None)
        env["MULTIAGENTS_CONFIG_ERROR"] = "; ".join(problems)
    # Only docker login's exec argv may mark an action as already inside the
    # container. Ambient, provider and caller values cannot bypass that hop.
    env.pop("MULTIAGENTS_CONTAINER_ACTION", None)
    # CW-R2b: a provider action never gets the safe-point key, whatever this
    # process, the provider's env or the caller says. The driver adds it to
    # its own CLI's launch environment itself, after this.
    return strip_key(env)


def resolve(provider_name: str, provider: Any, config_dir: Path,
            project_config: Path | None = None) -> Path | None:
    name = getattr(provider, "script_name", None) or f"{provider_name}.sh"
    return find_script(name, config_dir, project_config)


def resolve_adapter(provider: Any, config_dir: Path,
                    project_config: Path | None = None) -> Path | None:
    """The absolute path of a provider's `adapter:` (CX-C1), found where its
    action script would be; None when it has none or it is not installed."""
    name = getattr(provider, "adapter", "")
    if not name:
        return None
    found = find_script(name, config_dir, project_config)
    return found.absolute() if found is not None else None


def script_argv(script: Path) -> list[str]:
    """The command that runs this script, minus the action.

    A ``.sh`` runs under ``sh`` whatever its mode. Every shipped provider is
    one and every existing install has one, so that path is byte-identical to
    before this function existed — including the case of a hand-written
    override nobody remembered to ``chmod +x``.

    Anything else runs ITSELF and the kernel decides how: a shebang for
    ``myprovider.py``, an ELF header for somebody's compiled one. Deliberately
    not a shebang sniff — reading the first two bytes would send a native
    binary to ``sh``, which fails as a syntax error in a language nobody wrote.
    """
    if script.suffix == ".sh":
        return ["sh", str(script)]
    return [str(script)]


def why_it_would_not_run(script: Path, exc: OSError) -> str:
    """Turn an exec failure into the sentence that names the fix.

    Both realistic slips are ordinary and neither says so for itself: `Exec
    format error` is what a missing shebang looks like, and `Permission denied`
    is what a forgotten chmod looks like.
    """
    if exc.errno == errno.ENOEXEC:
        return (f"{script} is executable but the system cannot tell how to run "
                f"it — a script needs a shebang line, e.g. #!/usr/bin/env python3")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (f"{script} is not executable — a provider script that is not a "
                f"shell script runs itself, so it needs: chmod +x {script}")
    return f"{type(exc).__name__}: {exc}"


def run_action(provider_name: str, provider: Any, executor: Any, action: str,
               config_dir: Path, project_config: Path | None = None,
               timeout: int = CAPTURE_TIMEOUT,
               extra_env: dict[str, str] | None = None,
               cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a captured action. Returns ``(returncode, stdout, stderr)``.

    Never raises: a missing script, a timeout or an OS error all come back as a
    non-zero code with the reason in stderr, because every caller of this is
    reporting status rather than doing work. Output that is not UTF-8 is
    decoded with replacement rather than raising: a CLI error that echoes a
    binary path is still an answer.

    The script runs in its own process group, and a timeout kills the GROUP.
    A shell script's real work is usually a child — a `compact` that runs the
    CLI to do it — and killing only the shell left that child working on the
    session after the caller had reported the attempt over and moved on.
    stdin is closed: a captured action is non-interactive, and one run beside
    an attached CLI must not read the keys meant for it.
    """
    script = resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return 127, "", f"no script for provider {provider_name!r}"
    env = build_env(provider_name, provider, executor, extra_env)
    if env.get("MULTIAGENTS_CONFIG_ERROR"):
        return 64, "", env["MULTIAGENTS_CONFIG_ERROR"]
    try:
        child = subprocess.Popen(
            [*script_argv(script), action],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            env=env,
            cwd=cwd,
        )
    except OSError as exc:
        return 124, "", why_it_would_not_run(script, exc)
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(child)
        return 124, "", f"{type(exc).__name__}: {exc}"
    except BaseException:
        # P0-R8f.11: interrupted while waiting — Ctrl-C, a signal handler's
        # exception — the action must not go on working after its caller has
        # stopped listening. Kill and reap the group, then let it propagate.
        _kill_group(child)
        raise
    return child.returncode, _text(out), _text(err)


def _text(data: bytes | None) -> str:
    """Bytes as `text=True` would have given them, minus the ways it raises."""
    text = (data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _kill_group(child: subprocess.Popen) -> None:
    """Kill a timed-out action and everything it started, and reap it."""
    # Only while still running: once `child.returncode` is set the pid has
    # been reaped and the OS is free to hand it to an unrelated process, so a
    # `killpg` after that point could kill a stranger's group instead of ours.
    if child.returncode is None:
        with contextlib.suppress(OSError):
            os.killpg(child.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        child.kill()
    # Bounded: a grandchild that left the group can still hold the pipes open.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        child.communicate(timeout=5)
    for stream in (child.stdout, child.stderr):
        with contextlib.suppress(OSError):
            if stream is not None:
                stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)


def exec_action(provider_name: str, provider: Any, executor: Any, action: str,
                config_dir: Path, project_config: Path | None = None,
                extra_env: dict[str, str] | None = None):
    """``(argv, env)`` for an action that needs the terminal, or ``None``."""
    script = resolve(provider_name, provider, config_dir, project_config)
    if script is None:
        return None
    env = build_env(provider_name, provider, executor, extra_env)
    if env.get("MULTIAGENTS_CONFIG_ERROR"):
        return ([sys.executable, "-c",
                 "import sys; print(sys.argv[1], file=sys.stderr); sys.exit(64)",
                 env["MULTIAGENTS_CONFIG_ERROR"]], env)
    return ([*script_argv(script), action], env)
