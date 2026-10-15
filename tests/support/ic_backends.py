"""Backends the tests inject (IC-C5): the reference backend, and a double
around it that fails on demand. Imported by name inside a scheduler process
through `ic_support.BACKEND_ENV`; never selectable from project.yaml.

The plan file (path in `IC_TEST_PLAN`, re-read on every call) says what to do:

    {"probe": ["daemon down"],          # probe() returns these problems
     "provision": "unsupported:resources" | "invalid_spec" | "raise" | "raise_after",
     "start": "raise" | "raise_after",  # run() raises (an operational failure)
                                        # `raise_after`: the operation SUCCEEDS,
                                        # then the exception arrives (K7)
     "inspect": "unknown",              # inspect/inspect_invocation/stop answer
                                        # unknown(reason) (K6)
     "times": 1,                        # fail only the first N matching calls
     "version": "ref-2"}                # the version string it reports

Operational failures are plain exceptions on purpose: IC-C4 says an
unexpected exception is `unknown(reason)` to the caller, and IC-C3 sends that
down the `launch` class.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ic_support import lifecycle


def make_reference(root: Path, seeds=None):
    if seeds is not None:
        return lifecycle().ReferenceBackend(Path(root), seeds=seeds)
    return lifecycle().ReferenceBackend(Path(root))              # G1


def _plan() -> dict:
    try:
        return json.loads(Path(os.environ["IC_TEST_PLAN"]).read_text())
    except (KeyError, OSError, ValueError):
        return {}


def _due(step: str) -> bool:
    """True when this call must fail; counts down `times` through a file."""
    plan = _plan()
    if not plan.get(step):
        return False
    times = plan.get("times")
    if times is None:
        return True
    counter = Path(os.environ["IC_TEST_PLAN"] + f".{step}.count")
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    return seen < times


class Double:
    def __init__(self, inner):
        self._inner = inner

    @property
    def version(self) -> str:
        return _plan().get("version") or self._inner.version

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def probe(self):
        problems = _plan().get("probe")
        return list(problems) if problems else await self._inner.probe()

    async def provision(self, spec, key):
        if _due("provision"):
            mode = _plan()["provision"]
            if mode == "raise_after":
                await self._inner.provision(spec, key)
                raise RuntimeError("ic-double: provision failed after creating the resource")
            if mode == "raise":
                raise RuntimeError("ic-double: provision failed operationally")
            lc = lifecycle()
            if mode == "invalid_spec":
                return lc.InvalidSpec("ic-double: spec refused")
            return lc.Unsupported(mode.split(":", 1)[1])
        return await self._inner.provision(spec, key)

    async def run(self, identity, invocation, key):
        if _due("start"):
            if _plan()["start"] == "raise_after":
                await self._inner.run(identity, invocation, key)
                raise RuntimeError("ic-double: run failed after starting the invocation")
            raise RuntimeError("ic-double: start failed operationally")
        return await self._inner.run(identity, invocation, key)

    async def inspect(self, identity):
        if _plan().get("inspect") == "unknown":
            return lifecycle().Unknown("ic-double: cannot tell")
        return await self._inner.inspect(identity)

    async def inspect_invocation(self, identity, invocation_id):
        if _plan().get("inspect") == "unknown":
            return lifecycle().Unknown("ic-double: cannot tell")
        return await self._inner.inspect_invocation(identity, invocation_id)

    async def stop(self, identity, grace):
        result = await self._inner.stop(identity, grace)
        if _plan().get("inspect") == "unknown":
            return lifecycle().Unknown("ic-double: cannot confirm termination")
        return result


def make(root, seeds=None):
    root = Path(os.environ.get("IC_BACKEND_ROOT") or root)
    root.mkdir(parents=True, exist_ok=True)
    return Double(make_reference(root, seeds=seeds))


def boom(root, seeds=None):
    """A factory that fails (K3: a failing factory refuses isolated execution)."""
    raise RuntimeError("ic-backends: factory failed")
