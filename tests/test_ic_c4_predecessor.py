"""K14: predecessor uncertainty is an outcome, never permission to launch."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import ic_support as ic
from multiagents.executor.isolated import IsolatedExecutor

START_BOUND = 2  # no backend latency; includes the protocol's 0.5 s stop grace
PREVIOUS_KEY = "resource-predecessor"


class PredecessorBackend:
    """Protocol double: only provision creates an observable new resource."""
    version = "predecessor-test-1"

    def __init__(self, operation, final="exited"):
        self.operation = operation
        self.final = final
        self.resources = {PREVIOUS_KEY: ic.lifecycle().ResourceIdentity("old")}

    async def find(self, key):
        if self.operation == "find":
            return ic.lifecycle().Unknown("temporarily unavailable")
        return self.resources.get(key, ic.lifecycle().Absent())

    async def inspect_invocation(self, identity, key):
        return ic.lifecycle().Absent()

    async def stop(self, identity, grace):
        if self.operation == "stop":
            return ic.lifecycle().Unknown("temporarily unavailable")
        return ic.lifecycle().Exited(0, False, 1)

    async def inspect(self, identity):
        if self.operation == "inspect":
            return ic.lifecycle().Unknown("temporarily unavailable")
        if self.final == "absent":
            return ic.lifecycle().Absent()
        return ic.lifecycle().Exited(0, False, 1)

    async def remove(self, identity, keep_artifacts):
        if self.operation == "remove":
            return ic.lifecycle().Unknown("temporarily unavailable")
        self.resources.pop(PREVIOUS_KEY, None)
        return ic.lifecycle().Absent()

    async def provision(self, spec, key):
        identity = ic.lifecycle().ResourceIdentity(key)
        self.resources[key] = identity
        return identity

    async def run(self, identity, invocation, key):
        return key


def bound_executor(backend, records):
    ex = IsolatedExecutor()
    ex.backend = backend
    ex.bind("next-attempt", records.append,
            previous={"creation_key": PREVIOUS_KEY, "invocation_key": "old-invocation"})
    return ex


@pytest.mark.parametrize("operation", ["find", "stop", "inspect", "remove"])
def test_ic_c4_k14_predecessor_unknown_surfaces_context_without_new_resource(operation):
    backend = PredecessorBackend(operation)
    records = []
    ex = bound_executor(backend, records)
    try:
        reply = ic.run(ex.start(["unused"], "/work/repo", {}), timeout=START_BOUND)
    except asyncio.TimeoutError:
        pytest.fail(f"start looped on predecessor {operation} Unknown")
    assert isinstance(reply, ic.lifecycle().Unknown), reply
    assert PREVIOUS_KEY in reply.reason
    assert operation in reply.reason
    assert set(backend.resources) == {PREVIOUS_KEY}
    assert not any(r.get("creation_key") not in (None, PREVIOUS_KEY) for r in records)


@pytest.mark.parametrize("operation", ["find", "stop", "inspect", "remove"])
@pytest.mark.parametrize("final", ["exited", "absent"])
def test_ic_c4_k14_later_activation_resolves_recorded_predecessor_then_launches(operation, final):
    backend = PredecessorBackend(operation, final)
    records = []
    ex = bound_executor(backend, records)
    try:
        reply = ic.run(ex.start(["unused"], "/work/repo", {}), timeout=START_BOUND)
    except asyncio.TimeoutError:
        pytest.fail(f"start looped on predecessor {operation} Unknown")
    assert isinstance(reply, ic.lifecycle().Unknown), reply
    backend.operation = None  # later activation: backend can confirm termination
    # A fresh adapter models recovery from the durable predecessor evidence.
    recovered = bound_executor(backend, records)
    reply = ic.run(recovered.start(["unused"], "/work/repo", {}), timeout=START_BOUND)
    assert not isinstance(reply, ic.lifecycle().Unknown), reply
    assert PREVIOUS_KEY not in backend.resources
    keys = [r["creation_key"] for r in records if r.get("creation_key")]
    assert keys and keys[-1] in backend.resources


class SchedulerPredecessorBackend:
    """K3 double; uncertainty starts at predecessor lookup, after steer stops."""
    version = "predecessor-scheduler-1"

    def __init__(self, root, seeds=None):
        import os
        backend_root = Path(os.environ["IC_BACKEND_ROOT"])
        if seeds is not None:
            self.inner = ic.lifecycle().ReferenceBackend(backend_root, seeds=seeds)
        else:
            self.inner = ic.lifecycle().ReferenceBackend(backend_root)
        self.resolving = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def plan(self):
        import json
        import os
        return json.loads(Path(os.environ[ic.PLAN_ENV]).read_text())

    async def find(self, key):
        self.resolving = True
        if self.plan().get("operation") == "find":
            return ic.lifecycle().Unknown("predecessor unavailable")
        return await self.inner.find(key)

    async def stop(self, identity, grace):
        if self.resolving and self.plan().get("operation") == "stop":
            return ic.lifecycle().Unknown("predecessor unavailable")
        return await self.inner.stop(identity, grace)

    async def inspect(self, identity):
        if self.resolving and self.plan().get("operation") == "inspect":
            return ic.lifecycle().Unknown("predecessor unavailable")
        return await self.inner.inspect(identity)

    async def remove(self, identity, keep_artifacts):
        if self.resolving and self.plan().get("operation") == "remove":
            return ic.lifecycle().Unknown("predecessor unavailable")
        return await self.inner.remove(identity, keep_artifacts)


def make_scheduler_predecessor(root, seeds=None):
    return SchedulerPredecessorBackend(root, seeds=seeds)


@pytest.mark.parametrize("operation", ["find", "stop", "inspect", "remove"])
def test_ic_c4_k14_scheduler_holds_predecessor_uncertainty_and_compute_slot(tmp_path, monkeypatch, operation):
    import os
    s = ic.IcSched(tmp_path, monkeypatch)
    monkeypatch.setenv(ic.BACKEND_ENV, "test_ic_c4_predecessor:make_scheduler_predecessor")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([
        str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH", "")]))
    s.p.project["limits"] = {"max_depth": 7, "max_concurrent": 1}
    s.p.write()
    s._reset_server()
    try:
        s.fake.script(steps=[{"cost": 0, "sleep": 20}])
        s.start()
        first = s.create()["id"]
        run = s.wait_runs(first, 1)[0]
        evidence = s.wait(lambda: (e := s.evidence(run["attempt_id"]).get("executor", {}))
                          and e.get("invocation_id") and e, what="first launch evidence")
        previous = evidence["creation_key"]
        s.plan({"operation": operation})
        result = s.rpc("steer_run", {"run_id": run["run_id"], "message": "continue"})
        assert result.get("ok") is True, result
        held = s.wait(lambda: (v := s.get(first)) and v["state"] == "held" and v,
                      what="Unknown predecessor liveness hold")
        assert previous in ic.text_of(held.get("hold"))
        assert operation in ic.text_of(held.get("hold"))
        assert not ic.causes(held), "Unknown predecessor is not a failed attempt"
        second = s.create()["id"]
        s.wait(lambda: "max_concurrent" in ic.text_of(s.blocked(second)),
               what="retained compute reservation")
        assert s.runs(second) == []
        assert len(s.runs(first)) == 1
        assert s.evidence(run["attempt_id"])["executor"]["creation_key"] == previous
        assert s.fake.spawns() == 1
    finally:
        s.plan({})
        s.close()
