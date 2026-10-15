"""K16: recorded attempt identity controls recovery, including failed claims."""
import asyncio

import pytest

from multiagents.executor.isolated import IsolatedExecutor
from multiagents.executor.lifecycle import Absent, ResourceIdentity


class Backend:
    def __init__(self):
        self.provisions = []
        self.invocations = []

    async def find(self, key):
        return Absent()

    async def provision(self, spec, key):
        self.provisions.append(key)
        return ResourceIdentity(key)

    async def run(self, identity, invocation, key):
        self.invocations.append(key)
        return key


@pytest.mark.parametrize("failure", [{"class": "launch", "excerpt": "provision interrupted"},
                                    {"class": "other", "retryable": False}])
def test_k16_own_uninvoked_claim_with_a_failure_record_never_provisions_again(failure):
    backend = Backend()
    executor = IsolatedExecutor()
    executor.backend = backend
    executor.bind("attempt-own", lambda fields: None, previous={
        "attempt_id": "attempt-own", "creation_key": "resource-own",
        "failure": failure,
    })
    asyncio.run(executor.start(["unused"], "/work/repo", {}))
    assert backend.provisions == [], "own recorded attempt was provisioned under a fresh key"
    assert backend.invocations == [], "own abandoned claim ran again"
