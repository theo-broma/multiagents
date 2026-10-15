"""Adversary (nd-750860bb): stderr written just before exit is dropped.

`LifecycleHandle.drain_stderr` stops when `self._state` is exited, but that
state is set by the concurrent `lines()` / `wait()` coroutines, not by a read
the drain made after its own last empty read. Interleaving (a backend whose
`output` suspends, as any real one does):

  drain:  output(stderr, 0) reads nothing, suspends
  guest:  writes "rate limit reached" to stderr and exits
  lines:  inspect -> Exited; sets handle._state
  drain:  resumes with b"", sees Exited, returns

IC-C4: output stays readable after exit until `remove`. The lost tail is the
`stderr_tail` Runner classifies the failure from (`_classify`).
"""
from __future__ import annotations

import asyncio

from multiagents.executor import lifecycle as lc
from multiagents.executor.isolated import LifecycleHandle

TAIL = b"error: rate limit reached, retry later\n"


class SuspendingBackend:
    def __init__(self):
        self.stderr = b""
        self.exited = False
        self.snapshot_taken = asyncio.Event()
        self.exit_observed = asyncio.Event()
        self.first = True

    async def inspect(self, identity):
        if self.exited:
            self.exit_observed.set()
            return lc.Exited(1, False, 0.0)
        return lc.Running()

    async def output(self, identity, invocation, stream, offset):
        if stream == "stdout":
            return b""
        snapshot = self.stderr[offset:]
        if self.first:
            self.first = False
            self.snapshot_taken.set()
            await asyncio.wait_for(self.exit_observed.wait(), 5)
        return snapshot

    async def remove(self, identity, keep_artifacts):
        return lc.Exited(1, False, 0.0)


def test_ic_adv_stderr_written_just_before_exit_reaches_stderr_tail():
    async def scenario():
        backend = SuspendingBackend()
        handle = LifecycleHandle(backend, lc.ResourceIdentity("a" * 32), "inv", None, lambda _f: None)

        async def guest():
            await backend.snapshot_taken.wait()
            backend.stderr += TAIL
            backend.exited = True

        async def consume():
            async for _ in handle.lines():
                pass

        await asyncio.wait_for(asyncio.gather(handle.drain_stderr(), consume(), guest()), 10)
        return handle.stderr_tail

    assert "rate limit reached" in asyncio.run(scenario())
