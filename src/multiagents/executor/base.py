"""Executor interface plus the environment preparation both backends share.

An executor's only job is to start a command somewhere and hand back a line
stream. Everything above it — supervision, parsing, the tree, git — is identical
whether the process runs on this machine or inside a container, which is what
makes ``executor.kind: docker`` a one-line switch later.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .. import gitops, procs
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
        """Yield stdout lines as they arrive.

        A line longer than the reader's limit raises rather than truncating,
        and an unhandled raise here ends the run. The limit is generous (see
        STREAM_LIMIT) but it is still a limit, so the over-long line is
        salvaged instead: read what is buffered, hand it on marked as
        truncated, and carry on. Losing the tail of one event is a far smaller
        loss than losing the agent.
        """
        assert self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                # ValueError is what StreamReader.readline raises when the
                # separator is past the limit; the partial data stays in its
                # buffer, so drain it rather than leaving it to desynchronise
                # every line that follows.
                salvaged = bytes(stdout._buffer)          # noqa: SLF001
                stdout._buffer.clear()                    # noqa: SLF001
                self._stderr.append(f"[stream] over-long line dropped: {exc}")
                del self._stderr[:-200]
                if salvaged:
                    yield salvaged.decode("utf-8", errors="replace").rstrip("\n")
                continue
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


# --------------------------------------------------------------------------
# SV-R1/R2: an agent started under the launch wrapper (`multiagents.agentwrap`)
# writes to files, and this is the handle that reads them. It holds no pipe,
# so the agent does not care whether the server that started it is alive —
# and a server that did NOT start it can build one of these from the files
# alone, which is what adoption is (SV-R6).
# --------------------------------------------------------------------------

FOLLOW_POLL_SECONDS = 0.2
# How often a handle's `probe` may be asked: it can cost a `docker exec`.
PROBE_SECONDS = 5.0
# Where the wrapper records the agent's process group: `agent.pid` locally,
# `container.pid` under docker, where `kill_detached` has always read it.
AGENT_PID_FILES = ("agent.pid", "container.pid")
# The value `exit_status` holds when the wrapper ended the run at its deadline.
TIMEOUT_STATUS = "timeout"


def running(pid: int | None, start: str = "") -> bool:
    """`procs.alive`, except that a zombie is dead.

    A wrapper whose parent server died is reparented, and whatever inherits it
    may not reap it at once. It has already written everything it ever will.
    """
    if not procs.alive(pid, start):
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except (OSError, IndexError):
        return True
    return state not in ("Z", "X")


def read_exit_status(run_dir: Path) -> str | None:
    """The wrapper's verdict, or None while there is none (SV-R2)."""
    try:
        return (run_dir / "exit_status").read_text().strip() or None
    except OSError:
        return None


def _pid_namespace() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def agent_group(run_dir: Path) -> int | None:
    """The agent's process group, if it is provably still that agent's.

    The wrapper writes the group leader's pid; a pid alone is a number, so it
    is only trusted while the leader is alive, or while the group survives its
    leader — Linux does not hand out a pid still in use as a group id, so a
    live group with no leader is still the one the wrapper made.

    The wrapper records its pid namespace beside the pid, and a pid from any
    other namespace — a container's, read from the host across the bind
    mount — is refused outright: there it names an unrelated process.
    """
    own = _pid_namespace()
    for name in AGENT_PID_FILES:
        try:
            token, namespace = (run_dir / name).read_text().split()[:2]
        except (OSError, ValueError):
            continue
        if not token.isdigit() or own is None or namespace != own:
            continue
        pgid = int(token)
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return None
        try:
            fields = Path(f"/proc/{pgid}/stat").read_text().rpartition(")")[2].split()
        except OSError:
            return pgid                    # leader gone, group still alive
        # Field 5 is the process group: a live process holding this pid in a
        # different group is a recycled number, not our agent.
        return pgid if len(fields) > 2 and fields[2] == str(pgid) else None
    return None


def _signal(pid: int | None, start: str, pgid: int | None, sig: int) -> None:
    if pgid:
        with contextlib.suppress(OSError):
            os.killpg(pgid, sig)
    if pid and running(pid, start):
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


def stop_wrapped(run_dir: Path, pid: int | None, start: str = "",
                 grace: float = 3.0) -> bool:
    """End a wrapped agent from anywhere — no handle, no server (SV-R10).

    TERM to the wrapper (which forwards it) and to the agent's group; KILL to
    the group after `grace`, then to the wrapper if it has not recorded the
    exit and gone. The wrapper is killed last because it is what writes
    `exit_status`. Blocking: at most `grace` plus two seconds. Returns whether
    there was anything to stop.
    """
    pgid = agent_group(run_dir)
    if not pgid and not running(pid, start):
        return False
    _signal(pid, start, pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not running(pid, start) and not agent_group(run_dir):
            return True
        time.sleep(0.1)
    pgid = agent_group(run_dir)
    if pgid:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while running(pid, start) and time.monotonic() < deadline:
        time.sleep(0.1)
    _signal(pid, start, None, signal.SIGKILL)
    return True


@dataclass
class FollowHandle:
    """A wrapped agent, read from `runs/<id>/output.ndjson` (SV-R1).

    `offset` is the byte position just past the last line handed out, which is
    what a server persists so the next one can resume the file there (SV-R7).
    The stream ends once the wrapper has written `exit_status`, or its process
    is gone, AND the file has been read to its end — in that order, so a line
    written just before the exit is never lost.
    """

    pid: int
    run_dir: Path
    offset: int = 0
    pid_start: str = ""
    _proc: Any = None
    timed_out: bool = False
    stopper: Any = None               # executor-specific stop, if any
    # Executor-specific liveness, asked only once `pid` is gone, for a pid
    # that is not the wrapper: a host's `docker exec` client can die while
    # the wrapper it started carries on inside the container.
    probe: Any = None
    _probed_at: float = float("-inf")
    _probed: bool = True

    def _alive(self, ask: bool = True) -> bool:
        """Blocking when `ask` and a probe is due — call it off the loop."""
        if self._proc is not None:
            self._proc.poll()             # reap it, if it was ours to reap
        if running(self.pid, self.pid_start):
            return True
        if self.probe is None:
            return False
        if ask and time.monotonic() - self._probed_at >= PROBE_SECONDS:
            self._probed_at = time.monotonic()
            self._probed = bool(self.probe())
        return self._probed

    def _ended(self) -> bool:
        return read_exit_status(self.run_dir) is not None or not self._alive()

    def _read(self, fh, path: Path):
        """One poll, off the loop: whether the run has ended — decided BEFORE
        the read, so a line written just ahead of the exit is still read —
        then the next chunk of output."""
        ended = self._ended()
        if fh is None and path.exists():
            fh = path.open("rb")
            fh.seek(self.offset)
        chunk = fh.read(1 << 20) if fh is not None else b""
        return ended, fh, chunk

    async def lines(self) -> AsyncIterator[str]:
        path = self.run_dir / "output.ndjson"
        fh = None
        buf = b""
        try:
            while True:
                ended, fh, chunk = await asyncio.to_thread(self._read, fh, path)
                if chunk:
                    buf += chunk
                    while True:
                        cut = buf.find(b"\n")
                        if cut < 0:
                            break
                        line, buf = buf[:cut], buf[cut + 1:]
                        self.offset += cut + 1
                        yield line.decode("utf-8", errors="replace").rstrip("\r")
                    continue
                if ended:
                    if buf:
                        # The last line, unterminated: it is all there will be.
                        self.offset += len(buf)
                        text, buf = buf.decode("utf-8", errors="replace"), b""
                        yield text
                    return
                await asyncio.sleep(FOLLOW_POLL_SECONDS)
        finally:
            if fh is not None:
                fh.close()

    async def drain_stderr(self) -> None:
        """Nothing to drain: stderr goes to a file, read when asked for."""

    @property
    def stderr_tail(self) -> str:
        try:
            raw = (self.run_dir / "stderr.log").read_bytes()[-64 * 1024:]
        except OSError:
            return ""
        lines = [x for x in raw.decode("utf-8", errors="replace").splitlines() if x]
        return "\n".join(lines[-40:])

    async def wait(self) -> int | None:
        """The exit code, from `exit_status`. None if the process is gone
        without one — killed by the kernel, or its wrapper killed — which is
        the caller's to judge from the stream (SV-R6 decided)."""
        while True:
            status = await asyncio.to_thread(read_exit_status, self.run_dir)
            if status is None and not await asyncio.to_thread(self._alive):
                await asyncio.sleep(FOLLOW_POLL_SECONDS)
                status = await asyncio.to_thread(read_exit_status, self.run_dir)
                if status is None:
                    return None
            if status is not None:
                if status == TIMEOUT_STATUS:
                    self.timed_out = True
                    return 124
                try:
                    return int(status)
                except ValueError:
                    return None
            await asyncio.sleep(FOLLOW_POLL_SECONDS)

    @property
    def returncode(self) -> int | None:
        # The last probe's answer, not a fresh one: this is read on the loop.
        if read_exit_status(self.run_dir) is None and self._alive(ask=False):
            return None
        return 0

    async def stop(self, grace: float = 3.0) -> None:
        if self.stopper is not None:
            await asyncio.to_thread(self.stopper, grace)
        else:
            await asyncio.to_thread(stop_wrapped, self.run_dir, self.pid,
                                    self.pid_start, grace)
        await self._settle()

    async def _settle(self) -> None:
        # Give the wrapper a moment to record the exit it just saw.
        for _ in range(20):
            if await asyncio.to_thread(self._ended):
                return
            await asyncio.sleep(0.1)


def wrapper_argv(python: str, run_dir: Path, deadline: float, pid_file: Path,
                 argv: list[str], inline: bool = False) -> list[str]:
    """`argv`, run under the launch wrapper. `inline` passes the wrapper's
    source rather than its path, for an interpreter (a container's) that may
    not see this package."""
    from .. import agentwrap
    source = Path(agentwrap.__file__)
    head = [python, "-c", source.read_text()] if inline else [python, str(source)]
    return [*head, str(run_dir), f"{deadline:.3f}", str(pid_file), "--", *argv]


class Executor(ABC):
    """Starts a command and returns a :class:`Handle`."""

    kind = "base"

    @abstractmethod
    async def start(self, argv: list[str], cwd: Path, env: dict[str, str], *,
                    run_dir: Path | None = None, deadline: float = 0) -> Handle:
        """Start `argv`. Given `run_dir`, under the launch wrapper, returning
        a `FollowHandle` (SV-R1); without one, on pipes, as before."""
        ...

    def preflight(self) -> list[str]:
        """Problems that would make every run fail. Empty means ready."""
        return []

    def host_path(self, path: Path) -> Path:
        """Where `path`, as an agent run by this executor sees it, lives on
        the host. The same path for an executor that runs on the host."""
        return path

    def container_home(self) -> Path:
        """HOME as an agent run by this executor sees it: what a `~` in a
        provider's declarations means. On the host, the user's own — an agent
        given a private HOME reaches the provider's state in it only through
        links to exactly these paths (`prepare_home`)."""
        return Path.home()

    def git(self, agent_id: str) -> gitops.Git:
        """Where agent `agent_id`'s own commits run, and their hooks (SG-R3):
        the end-of-run commit, the CI-R5 fix-loop commits and the merge gate's
        revert. On the host for an executor that runs agents there."""
        return gitops.HOST


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
        # `KEY=value` SETS the variable; a bare `KEY` forwards this process's.
        # Forwarding alone could not express "the toolchain cache is at this
        # path inside the container", because that value does not exist in the
        # host environment to be forwarded — a mounted package cache the agent
        # cannot be told the location of is a mounted cache it does not use,
        # and every agent then re-downloads the world into its private HOME.
        key, sep, literal = key.partition("=")
        key = key.strip()
        if not key or key in blocked:
            continue                     # an explicit block always wins
        value = literal if sep else os.environ.get(key)
        if value is not None:
            env[key] = value

    if home is not None:
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_DATA_HOME"] = str(home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(home / ".cache")

    _add_git_config_override(env, "commit.gpgsign", "false")
    env.update(identity)
    return env


def _add_git_config_override(env: dict[str, str], key: str, value: str) -> None:
    """Append one `GIT_CONFIG_PARAMETERS` entry that wins over anything else
    the agent's environment already carries (CI-R6 — commit-identity.md).

    `GIT_CONFIG_PARAMETERS` outranks the `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`
    mechanism regardless of which one a passed-through user setting used, and
    within `GIT_CONFIG_PARAMETERS` itself the last entry for a given key wins
    — so appending here, after whatever passthrough already put in `env`,
    always overrides it without touching any config file. Never written to
    `os.environ`: this only ever mutates the child's own env dict.
    """
    entry = f"'{key}'='{value}'"
    existing = env.get("GIT_CONFIG_PARAMETERS")
    env["GIT_CONFIG_PARAMETERS"] = f"{existing} {entry}" if existing else entry


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


def private_file(home: Path, relative: str) -> Path:
    """``home / relative``, made safe to write without reaching the real home.

    A per-agent HOME links whole directories of the user's CLI state into
    place, so writing a file beneath one of them writes the user's own copy.
    Every linked directory on the way down is therefore replaced by a real
    directory holding one link per entry of what it pointed at: the CLI still
    finds everything else it had, and the file itself is this agent's alone.
    Only the link is removed, never anything it pointed at.
    """
    target = home / relative
    cursor = home
    for part in Path(relative).parent.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            source = Path(os.readlink(cursor))
            if not source.is_absolute():
                source = cursor.parent / source
            cursor.unlink()
            cursor.mkdir()
            if source.is_dir():
                for entry in source.iterdir():
                    with contextlib.suppress(OSError):
                        (cursor / entry.name).symlink_to(
                            entry, target_is_directory=entry.is_dir())
        else:
            cursor.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    return target
