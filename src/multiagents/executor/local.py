"""Local subprocess executor — phase 1.

Isolation here comes from three things: a git worktree (the agent cannot touch
your working tree or another agent's branch), a private HOME (it cannot read
another CLI's stored credentials), and a deny-by-default environment (it has no
``SSH_AUTH_SOCK``, so it cannot authenticate as you anywhere).

What it does *not* give you is process isolation: an agent running with
``--dangerously-skip-permissions`` can still reach anything your user account
can reach. Closing that is what the docker executor is for.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import procs
from .base import Executor, FollowHandle, Handle, turn_deadline, wrapper_argv


# One stdout line can carry a whole file: a CLI reports a tool result as a single
# JSON object, and reading a 60 KB source file makes a 60 KB line. asyncio's
# default StreamReader limit is 64 KiB, and exceeding it raises ValueError from
# readline() and kills the run — which is what stopped the bug-reporter every
# time, its brief being to read this project's own source.
STREAM_LIMIT = 16 * 1024 * 1024


class LocalExecutor(Executor):
    kind = "local"

    def __init__(self, providers: dict[str, Any] | None = None):
        # Only for an adapter run (CX-C1, CX-C16); nothing else here needs them.
        self.providers = providers or {}

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str], *,
                    run_dir: Path | None = None, deadline: float = 0,
                    pid_file: Path | None = None, provider: str = "") -> Handle:
        cwd.mkdir(parents=True, exist_ok=True)
        entry = self.providers.get(provider)
        if entry is not None and not entry.adapter and argv and argv[0] == entry.bin:
            found = entry.resolve_bin(env=env)
            if found.launcher is None:
                raise FileNotFoundError(entry.bin_error(found))
            # H7 PS-R2a: argv[0] is the launcher, not the realpath.
            argv = [str(found.launcher), *argv[1:]]
        env = self.adapter_env(argv, env, provider)
        if run_dir is not None:
            return await self._start_wrapped(argv, cwd, env, run_dir, deadline,
                                             pid_file or run_dir / "agent.pid")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            limit=STREAM_LIMIT,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            # Own process group, so stopping a run also stops the shells and
            # test runners the agent started underneath itself.
            start_new_session=True,
        )
        return Handle(pid=proc.pid, _proc=proc)

    async def _start_wrapped(self, argv: list[str], cwd: Path, env: dict[str, str],
                             run_dir: Path, deadline: float, pid_file: Path) -> FollowHandle:
        """SV-R1: under the launch wrapper, which owns the agent's output.

        The wrapper is the pid recorded for the node, not the agent: it writes
        `exit_status` and only then exits, so "the pid is dead and there is no
        status" can only mean the status will never come — never that it is
        about to. Its own stdout and stderr go nowhere: it has nothing to say
        that it does not put in a file.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "exit_status").unlink(missing_ok=True)
        # RM-R1c: no predecessor's pid file may speak for this run.
        (run_dir / "wrapper.pid").unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
        offset = _turn_start(run_dir / "output.ndjson")
        # A plain Popen, not asyncio's: an asyncio subprocess transport kills
        # its process when it is closed or collected, which is at the latest
        # when this server exits — exactly what must not happen (SV-R3).
        launched_at = time.time()
        proc = subprocess.Popen(
            wrapper_argv(sys.executable, run_dir,
                         turn_deadline(env, deadline, launched_at), pid_file, argv),
            cwd=str(cwd),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # Its own session: the server's end, and the terminal's, are not
            # the agent's (SV-R3).
            start_new_session=True,
        )
        return FollowHandle(pid=proc.pid, run_dir=run_dir, offset=offset,
                            pid_start=procs.start_time(proc.pid), _proc=proc,
                            launched_at=launched_at)


def _turn_start(path: Path) -> int:
    """Where a new turn's output will begin in `path` (SV-R1/R7): its end, on
    a line of its own. A previous turn can end mid-line — killed between a
    write and its newline — and the wrapper appends, so without this the new
    turn's first event would be glued onto that fragment and lost with it."""
    try:
        with path.open("rb+") as fh:
            end = fh.seek(0, 2)
            if end:
                fh.seek(end - 1)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
                    end += 1
            return end
    except FileNotFoundError:
        return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
