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
from pathlib import Path

from .base import Executor, Handle


class LocalExecutor(Executor):
    kind = "local"

    async def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> Handle:
        cwd.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *argv,
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
