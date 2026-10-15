"""Adversary (nd-750860bb): the reference backend signalling a process it never started.

IC-C5: "Its `stop` signals only processes it started and recorded; it never
signals an unrelated host process." The record `stop` trusts
(`inv-*/invocation.json`) sits in the parent directory of the invocation's own
HOME, so the invocation can rewrite the pid and kernel start time it names.
`stop` (and `remove`, and therefore every `LifecycleHandle.wait`) then SIGKILLs
whatever process group that pid leads, even after the invocation has exited.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from multiagents.executor import lifecycle as lc

REWRITE = """
import json, os, sys
record = os.path.join(os.environ["HOME"], "..", "invocation.json")
data = json.load(open(record))
data["pid"], data["pid_start"] = int(sys.argv[1]), sys.argv[2]
json.dump(data, open(record, "w"))
"""


def test_ic_adv_stop_never_kills_a_process_named_by_a_record_the_guest_can_rewrite(tmp_path):
    backend = lc.ReferenceBackend(tmp_path / "backend")
    victim = subprocess.Popen(["sleep", "60"], start_new_session=True)     # not the backend's
    try:
        async def scenario():
            identity = await backend.provision(
                lc.LaunchSpec(owner="att_adv", labels={"multiagents.project": "p"}), "k")
            invocation = lc.InvocationSpec(
                argv=[sys.executable, "-c", REWRITE, str(victim.pid), lc._start_time(victim.pid)],
                env={}, cwd="/work/repo")
            assert await backend.run(identity, invocation, "i") == "i"
            deadline = time.monotonic() + 10
            while not isinstance(await backend.inspect(identity), lc.Exited):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            return await backend.stop(identity, 0.2)

        asyncio.run(scenario())
        time.sleep(0.3)
        assert victim.poll() is None, (
            f"stop signalled an unrelated host process (exit {victim.poll()})")
    finally:
        victim.kill()
        victim.wait()
