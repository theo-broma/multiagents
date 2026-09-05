"""Executor interface plus the environment preparation both backends share.

An executor's only job is to start a command somewhere and hand back a line
stream. Everything above it — supervision, parsing, the tree, git — is identical
whether the process runs on this machine or inside a container, which is what
makes ``executor.kind: docker`` a one-line switch later.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from ..redact import register_environment

# Always forwarded: without these, most CLIs cannot even locate a terminal or a
# temporary directory. None of them carry credentials.
BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR", "SHELL", "USER")


@dataclass
class Handle:
    """A running agent process."""

    pid: int
    _proc: asyncio.subprocess.Process
    _stderr: list[str] = field(default_factory=list)

    async def lines(self) -> AsyncIterator[str]:
        """Yield stdout lines as they arrive."""
        assert self._proc.stdout is not None
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                break
            yield raw.decode("utf-8", errors="replace").rstrip("\n")

    async def drain_stderr(self) -> None:
        """Collect stderr in the background so a chatty CLI cannot deadlock on a
        full pipe while we are reading stdout."""
        if self._proc.stderr is None:
            return
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                self._stderr.append(line)
                del self._stderr[:-200]      # keep only the tail

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr[-40:])

    async def wait(self) -> int:
        return await self._proc.wait()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def stop(self, grace: float = 10.0) -> None:
        """Terminate the process and everything it spawned.

        Agent CLIs start children of their own (shells, test runners), so we
        signal the whole process group; killing only the parent orphans them.
        """
        if self._proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(self.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace)
        except (asyncio.TimeoutError, TimeoutError):
            try:
                os.killpg(os.getpgid(self.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                with_suppress = getattr(self._proc, "kill", None)
                if with_suppress:
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass


class Executor(ABC):
    """Starts a command and returns a :class:`Handle`."""

    kind = "base"

    @abstractmethod
    async def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> Handle:
        ...

    def preflight(self) -> list[str]:
        """Problems that would make every run fail. Empty means ready."""
        return []


# --------------------------------------------------------------------------
# Environment preparation — shared, because the same decisions become `-e`
# flags and bind mounts in the docker backend.
# --------------------------------------------------------------------------


def build_env(
    *,
    passthrough: list[str],
    blocked: list[str],
    home: Path | None,
    identity: dict[str, str],
) -> dict[str, str]:
    """Construct a child's environment from a clean base.

    Deny-by-default: the child starts with nothing and receives only what is
    named. The values of blocked variables are registered as redaction literals
    so that if one reaches output by some other route it is still masked before
    anything is written to disk.
    """
    register_environment(blocked)

    env: dict[str, str] = {}
    for key in BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    for key in passthrough:
        if key in blocked:
            continue                     # an explicit block always wins
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    if home is not None:
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(home / ".cache")

    env.update(identity)
    return env


def prepare_home(home: Path, links: list[str], policy: str = "per-agent",
                 agent: str = "agent", copies: list[str] | None = None) -> Path | None:
    """Build a private HOME containing only this provider's own state.

    Each entry in ``links`` is a path relative to the real home which is
    symlinked into the private one. An opencode agent therefore reaches
    opencode's stored credentials and nothing else — it cannot read
    ``~/.claude.json`` or agy's token store, because they are simply not there.

    Entries in ``copies`` are **copied** rather than symlinked. That is for
    credential stores the CLI also writes to: Claude keeps its whole project
    history and its quota cache in the single file ``~/.claude.json``, so
    symlinking it would have every concurrent subagent writing the user's real
    config — and corrupting the very file the budget adapter reads. A copy also
    means the file is never mounted into a container.

    Returns ``None`` for the ``shared`` policy, meaning "use the real HOME".
    """
    if policy != "per-agent":
        return None

    real = Path.home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass

    for relative in links:
        source = real / relative
        if not source.exists():
            continue
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            continue
        try:
            target.symlink_to(source, target_is_directory=source.is_dir())
        except OSError:
            pass

    for relative in (copies or []):
        source = real / relative
        target = home / relative
        if not source.exists() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
                target.chmod(0o600)
        except OSError:
            pass

    for sub in (".config", ".local/share", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    # A private HOME means no ~/.gitconfig, and agents are expected to commit.
    # Without this the first thing every agent does is stop and configure git,
    # which wastes a turn and produces commits attributed to nobody.
    gitconfig = home / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n"
            f"\tname = {agent} (multiagents)\n"
            f"\temail = {agent}@multiagents.local\n"
            "[commit]\n\tgpgsign = false\n"
            "[advice]\n\tdetachedHead = false\n"
        )
    return home
