"""Adversary: predecessor failure resolution hangs indefinitely on Unknown.

IC-C4: "When termination cannot be confirmed, returns unknown(reason) and the
caller keeps the reservation and the liveness hold."
K6: "If the outcome is unknown, the reservation and the liveness hold are both kept."
K7: "The caller wraps an operational exception into Unknown(reason)."

In IsolatedExecutor._resolve_failure (isolated.py:108-123):
`while True:`
`    identity = await outcome(self.backend.find, creation_key)`
`    ...`
`    await outcome(self.backend.stop, identity, 0.5)`
`    state = await outcome(self.backend.inspect, identity)`
`    if isinstance(state, (Exited, Absent)):`
`        removed = await outcome(self.backend.remove, identity, True)`
`        if isinstance(removed, (Exited, Absent)):`
`            break`
`    await asyncio.sleep(0.1)`

If the predecessor's backend returns Unknown on find, stop, or inspect,
_resolve_failure has no deadline, timeout, or break condition for Unknown.
It loops forever, deadlocking the scheduler runner / worker and never releasing.
Similarly, LifecycleHandle.wait() loops forever when _inspect() returns Unknown.
"""
from __future__ import annotations

import asyncio

import pytest

from multiagents.executor import lifecycle as lc
from multiagents.executor.isolated import IsolatedExecutor


class UnknownBackend:
    version = "unknown-1"

    async def probe(self):
        return []

    async def find(self, key):
        return lc.Unknown("lookup unavailable")

    async def inspect(self, identity):
        return lc.Unknown("inspect unavailable")

    async def stop(self, identity, grace):
        return lc.Unknown("stop unavailable")

    async def remove(self, identity, keep_artifacts):
        return lc.Unknown("remove unavailable")


def test_ic_adv2_predecessor_resolution_does_not_hang_forever_on_unknown():
    executor = IsolatedExecutor()
    executor.backend = UnknownBackend()
    executor.problem = None
    executor.bind("att_test", lambda fields: None, previous={"creation_key": "res-old"})

    async def run_with_timeout():
        # start() invokes _resolve_failure on self.previous['creation_key']
        # When backend returns Unknown, start() must resolve or raise promptly without hanging.
        await executor.start(["echo", "hi"], "/work/repo", {})

    # If it hangs in `while True:`, wait_for will raise asyncio.TimeoutError and fail this test.
    asyncio.run(asyncio.wait_for(run_with_timeout(), timeout=1.0))
