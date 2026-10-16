"""The slice 3 backend double: `ic_backends.Double` (the reference backend that
fails on demand) plus INJECTED CRASHES (IC-C15's five launch boundaries).
Imported by name inside the scheduler process through K3
(`MULTIAGENTS_TEST_ISOLATED_BACKEND=ic_s3_backends:make`, or `:make_docker` to
wrap the real Docker backend).

Plan file (`IC_TEST_PLAN`, re-read on every call), in addition to `ic_backends`:

    {"crash": "provision_entry" | "provision_done" | "run_entry" | "run_done"
              | "exited_seen" | "remove_entry"}

When the named point is reached the scheduler dies at once, as a host crash
does: `kill_pid` (the scheduler's pid, set by `S3Sched.crash_at`) is sent
SIGKILL, then this process `os._exit(137)`s too (it is the same process when the
launch runs inside the scheduler). The crash is cleared from the plan first, so
the restarted scheduler does not die again.

  provision_entry  before creation (boundary 1: the key is recorded, find = absent)
  provision_done   the resource exists, the identity was never returned (2)
  run_entry        the resource exists and is created, never started (2)
  run_done         the invocation started, its id was never returned (3)
  exited_seen      `inspect` has just seen the resource exited, the caller has not (4)
  remove_entry     the first `remove` call (retention or cleanup) (5)

`{"calls": true}` also journals every operation name to `<plan>.calls`.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import ic_backends
from ic_support import kind_of


def _plan_path() -> Path:
    return Path(os.environ["IC_TEST_PLAN"])


def _crash_if(point: str) -> None:
    try:
        plan = json.loads(_plan_path().read_text())
    except (KeyError, OSError, ValueError):
        return
    if plan.get("crash") == point:
        plan.pop("crash")
        victim = plan.pop("kill_pid", None)
        _plan_path().write_text(json.dumps(plan))
        if victim:                                   # the scheduler, if this is a child of it
            try:
                os.kill(int(victim), signal.SIGKILL)
            except OSError:
                pass
        os._exit(137)


def _journal(op: str) -> None:
    try:
        if ic_backends._plan().get("calls"):
            with open(str(_plan_path()) + ".calls", "a") as f:
                f.write(op + "\n")
    except Exception:
        pass


class Crashy(ic_backends.Double):
    async def provision(self, spec, key):
        _journal("provision")
        with open(str(_plan_path()) + ".creation_keys", "a") as keys:
            keys.write(json.dumps({"owner": spec.owner, "key": key}) + "\n")
        _crash_if("provision_entry")
        got = await super().provision(spec, key)
        _crash_if("provision_done")
        return got

    async def run(self, identity, invocation, key):
        _journal("run")
        _crash_if("run_entry")
        got = await super().run(identity, invocation, key)
        _crash_if("run_done")
        return got

    async def inspect(self, identity):
        _journal("inspect")
        state = await super().inspect(identity)
        if kind_of(state) == "exited":
            _crash_if("exited_seen")
        return state

    async def inspect_invocation(self, identity, invocation_id):
        state = await super().inspect_invocation(identity, invocation_id)
        if kind_of(state) == "exited":
            _crash_if("exited_seen")
        return state

    async def remove(self, identity, keep_artifacts):
        _journal("remove:keep" if keep_artifacts else "remove:discard")
        _crash_if("remove_entry")
        return await self._inner.remove(identity, keep_artifacts)

    async def stop(self, identity, grace):
        _journal("stop")
        return await super().stop(identity, grace)

    async def find(self, key):
        _journal("find")
        return await self._inner.find(key)


def make(root, seeds=None):
    root = Path(os.environ.get("IC_BACKEND_ROOT") or root)
    root.mkdir(parents=True, exist_ok=True)
    return Crashy(ic_backends.make_reference(root, seeds=seeds))


def make_docker(root, seeds=None):
    """The same double around the REAL Docker backend (SG_TEST_DOCKER=1)."""
    import ic_s3
    root = Path(os.environ.get("IC_BACKEND_ROOT") or root)
    root.mkdir(parents=True, exist_ok=True)
    settings = {}
    if os.environ.get("IC_DOCKER_RESOURCES"):             # the configured executor.isolated.resources
        settings["resources"] = json.loads(os.environ["IC_DOCKER_RESOURCES"])
    return Crashy(ic_s3.docker_backend(root, seeds=seeds, **settings))
