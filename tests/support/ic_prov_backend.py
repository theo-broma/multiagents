"""The backend double of the slice 2 tests (IC-C9..C13): the reference backend
that fails the N-th provisioning on demand. Injected through
`MULTIAGENTS_TEST_ISOLATED_BACKEND=ic_prov_backend:make` (K3); never selectable
from project.yaml.

The plan file (path in `IC_TEST_PLAN`, re-read on every call):

    {"fail_nth_provision": 4}         # the 4th DISTINCT creation key fails
    {"fail_from_nth_provision": 4}    # that one and every later key fails
    {"mode": "raise" | "raise_after" | "pause_after"} # default "raise": nothing is created;
                                      # "raise_after": the clone is made, then
                                      # the exception arrives (K7)

A retry under a key already seen keeps that key's index, so "the 4th" means
the 4th resource ever asked for, whatever TF does afterwards.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ic_support import lifecycle


def _plan() -> dict:
    try:
        return json.loads(Path(os.environ["IC_TEST_PLAN"]).read_text())
    except (KeyError, OSError, ValueError):
        return {}


def _index(key: str) -> int:
    path = Path(os.environ["IC_TEST_PLAN"] + ".keys")
    keys = json.loads(path.read_text()) if path.exists() else []
    if key not in keys:
        keys.append(key)
        path.write_text(json.dumps(keys))
    return keys.index(key) + 1


class Flaky:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def provision(self, spec, key):
        plan = _plan()
        if plan.get("seed_required"):
            return lifecycle().InvalidSpec("seed_required")
        n = _index(key) if os.environ.get("IC_TEST_PLAN") else 0
        due = (plan.get("fail_nth_provision") == n
               or (plan.get("fail_from_nth_provision") and n >= plan["fail_from_nth_provision"]))
        if not due:
            return await self._inner.provision(spec, key)
        if plan.get("mode") == "pause_after":
            await self._inner.provision(spec, key)
            Path(os.environ["IC_TEST_PLAN"] + ".paused-owner").write_text(
                json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
            Path(os.environ["IC_TEST_PLAN"] + ".staged").write_text(key)
            await asyncio.Future()  # test crashes the attempt owner at this explicit gate
        if plan.get("mode") == "raise_after":
            await self._inner.provision(spec, key)
            raise RuntimeError("ic-prov: provision failed after creating the resource")
        raise RuntimeError("ic-prov: provision failed operationally")


def make(root, *, seeds=None):
    root = Path(os.environ.get("IC_BACKEND_ROOT") or root)
    root.mkdir(parents=True, exist_ok=True)
    return Flaky(lifecycle().ReferenceBackend(root, seeds=seeds))
