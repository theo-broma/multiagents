"""Adversary: M7 stopped conversation resource must refuse subsequent run().

IC-C4: "on an exited attempt resource, exited... On a stopped resource, run()
must be refused."
In ReferenceBackend.run (lifecycle.py:397):
`if resource["removed"] or resource.get("stopped_at") is not None or ...`
If the stopped_at check is omitted (mutant M7), a conversation resource that
previously executed an invocation and was subsequently stopped does not refuse
a later run().
"""
from __future__ import annotations

import asyncio

from multiagents.executor import lifecycle as lc


def test_ic_adv2_stopped_conversation_resource_refuses_run(tmp_path):
    backend = lc.ReferenceBackend(tmp_path / "backend")

    async def scenario():
        spec = lc.LaunchSpec(owner="conv_1", lifetime="conversation", labels={"multiagents.project": "p"})
        identity = await backend.provision(spec, "conv-res")
        assert isinstance(identity, lc.ResourceIdentity)

        # First invocation on the conversation resource
        inv1 = lc.InvocationSpec(argv=["echo", "1"], env={}, cwd="/work/repo", kind="turn")
        assert await backend.run(identity, inv1, "turn-1") == "turn-1"

        # Wait for turn 1 to exit
        while not isinstance(await backend.inspect_invocation(identity, "turn-1"), lc.Exited):
            await asyncio.sleep(0.05)

        # Stop the resource
        stop_state = await backend.stop(identity, 0.1)
        assert isinstance(stop_state, lc.Exited)

        # Now try to run turn 2 on the stopped conversation resource
        inv2 = lc.InvocationSpec(argv=["echo", "2"], env={}, cwd="/work/repo", kind="turn")
        reply = await backend.run(identity, inv2, "turn-2")

        # Must return Exited (refused because resource was stopped)
        assert isinstance(reply, lc.Exited), (
            f"M7 defect: Stopped conversation resource accepted run(): {reply}"
        )

    asyncio.run(scenario())
