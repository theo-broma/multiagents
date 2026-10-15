"""Adapt the resource lifecycle to Runner's streaming executor seam.

Only host configuration constructs a backend. The reference backend is a
bookkeeping double, injected by tests; it never supplies isolation evidence.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import time
import uuid
from pathlib import Path

from .. import gitops
from .base import Executor, strip_run_environment
from .lifecycle import (Absent, Exited, InvalidSpec, InvocationSpec, LaunchSpec,
                        ResourceIdentity, Unknown, Unsupported, spec_digest)


def _write_run_file(run_dir, name, data, *, append=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = gitops._open_file_beneath(*gitops.beneath(run_dir), name, flags,
                                  0o600, create=True, replace=True)
    with os.fdopen(fd, "ab" if append else "wb") as stream:
        stream.write(data)


async def outcome(operation, *args):
    """An operational exception cannot prove that creation did not happen."""
    try:
        return await operation(*args)
    except Exception as exc:
        return Unknown(f"{type(exc).__name__}: {exc}")


class IsolatedExecutor(Executor):
    kind = "isolated"
    uses_guest_environment = True

    def __init__(self, paths=None, providers=None):
        self.paths = paths
        self.providers = providers or {}
        self.backend = None
        self.creation_key = ""
        self.termination_confirmed = False
        self.backend_name = "docker"
        self.version = "unavailable"
        self.problem = "isolated docker lifecycle is not implemented yet"
        self.record = lambda fields: None
        target = os.environ.get("MULTIAGENTS_TEST_ISOLATED_BACKEND")
        if target:
            try:
                module, factory = target.split(":", 1)
                root = paths.data / "isolated" if paths else Path.cwd() / ".isolated"
                self.backend = getattr(importlib.import_module(module), factory)(root)
                self.backend_name = getattr(self.backend, "name", "reference")
                self.version = self.backend.version
                if not isinstance(self.version, str) or not self.version:
                    raise ValueError("backend version must be a nonempty string")
                for operation in ("probe", "provision", "find", "inspect", "run",
                                  "inspect_invocation", "output", "stop", "remove", "list_owned"):
                    if not callable(getattr(self.backend, operation, None)):
                        raise ValueError(f"backend has no {operation} operation")
                self.problem = ""
            except Exception as exc:
                self.problem = f"backend factory failed: {type(exc).__name__}: {exc}"

    def restore(self, evidence):
        # These keys come from the host attempt store, never from a pid file.
        self.creation_key = evidence.get("creation_key", "")
        self.termination_confirmed = evidence.get("termination_confirmed", False)

    def wrapper_verdict(self, run_id):
        if not self.creation_key and self.termination_confirmed:
            return False
        if self.backend is None or not self.creation_key:
            return None

        async def inspect():
            identity = await outcome(self.backend.find, self.creation_key)
            if isinstance(identity, Absent):
                return False
            if not isinstance(identity, ResourceIdentity):
                return None
            state = await outcome(self.backend.inspect, identity)
            if isinstance(state, (Exited, Absent)):
                return False
            return None if isinstance(state, Unknown) else True
        return asyncio.run(inspect())

    async def probe(self):
        if self.problem:
            return [self.problem]
        result = await outcome(self.backend.probe)
        return [result.reason] if isinstance(result, Unknown) else result

    def bind(self, owner, record, *, previous=None, begin=None):
        self.record = record
        self.begin = begin or record
        self.previous = dict(previous or {})
        self.restore(self.previous)
        self.spec = LaunchSpec(owner=owner, lifetime="attempt",
                               labels={"multiagents.project": self.paths.slug if self.paths else ""})

    def _uncertain(self, creation_key, operation, reply):
        return Unknown(f"predecessor {creation_key}: {operation}: "
                       f"{getattr(reply, 'reason', str(reply))}")

    async def predecessor_verdict(self):
        """Probe recorded resource liveness without polling an unknown backend."""
        if not self.creation_key and self.termination_confirmed:
            return False
        if self.backend is None or not self.creation_key:
            return self._uncertain(self.creation_key, "find", "backend or key unavailable")
        identity = await outcome(self.backend.find, self.creation_key)
        if isinstance(identity, Absent):
            return False
        if not isinstance(identity, ResourceIdentity):
            return self._uncertain(self.creation_key, "find", identity)
        state = await outcome(self.backend.inspect, identity)
        if isinstance(state, Unknown):
            return self._uncertain(self.creation_key, "inspect", state)
        return not isinstance(state, (Exited, Absent))

    async def _resolve_failure(self, creation_key, invocation_key, run_dir):
        # Find even when provision raised: it may have created before raising.
        # K14: one resolution pass; Unknown retains the recorded key for recovery.
        identity = await outcome(self.backend.find, creation_key)
        if not isinstance(identity, Absent):
            if not isinstance(identity, ResourceIdentity):
                return self._uncertain(creation_key, "find", identity)
            self.record({"identity": identity.id})
            state = await outcome(self.backend.inspect_invocation, identity, invocation_key)
            if not isinstance(state, (Absent, Unknown)):
                self.record({"invocation_id": invocation_key})
            stopped = await outcome(self.backend.stop, identity, 0.5)
            if isinstance(stopped, Unknown):
                return self._uncertain(creation_key, "stop", stopped)
            state = await outcome(self.backend.inspect, identity)
            if not isinstance(state, (Exited, Absent)):
                return self._uncertain(creation_key, "inspect", state)
            removed = await outcome(self.backend.remove, identity, True)
            if not isinstance(removed, (Exited, Absent)):
                return self._uncertain(creation_key, "remove", removed)
        self.record({"termination_confirmed": True})
        if run_dir is not None:
            _write_run_file(run_dir, "exit_status", b"1")

    async def start(self, argv, cwd, env, *, run_dir=None, deadline=0, provider=""):
        if self.backend is None:
            raise RuntimeError("isolation_unavailable: " + self.problem)
        if self.previous.get("creation_key"):
            resolved = await self._resolve_failure(self.previous["creation_key"],
                                                   self.previous.get("invocation_key", ""), None)
            if isinstance(resolved, Unknown):
                return resolved
        launch_id = uuid.uuid4().hex
        creation_key = self.creation_key = "resource-" + launch_id
        invocation_key = "invocation-" + launch_id
        self.termination_confirmed = False
        self.begin({"kind": self.kind, "backend": self.backend_name,
                    "backend_version": self.version, "spec_version": self.spec.spec_version,
                    "spec_digest": spec_digest(self.spec), "creation_key": creation_key})
        if run_dir is not None:
            directory = gitops._open_beneath(*gitops.beneath(run_dir), create=True)
            try:
                try:
                    os.unlink("exit_status", dir_fd=directory)
                except FileNotFoundError:
                    pass
            finally:
                os.close(directory)
        identity = await outcome(self.backend.provision, self.spec, creation_key)
        failure = None
        if isinstance(identity, (InvalidSpec, Unsupported)):
            reason = ("unsupported:" + identity.field if isinstance(identity, Unsupported)
                      else "invalid_spec: " + identity.reason)
            failure = {"class": "other", "retryable": False, "detail_code": "isolation_unavailable",
                       "excerpt": reason}
        elif not isinstance(identity, ResourceIdentity):
            failure = {"class": "launch", "detail_code": "isolation_unavailable",
                       "excerpt": getattr(identity, "reason", str(identity))}
        else:
            self.record({"identity": identity.id, "invocation_key": invocation_key})
            environment = strip_run_environment(dict(self.adapter_env(argv, env, provider)))
            invocation = InvocationSpec(argv=list(argv), env=environment, cwd=self.spec.guest_repo,
                                        stdin=None, kind="agent")
            reply = await outcome(self.backend.run, identity, invocation, invocation_key)
            if reply == invocation_key:
                self.record({"invocation_id": reply})
                self.previous = {"creation_key": creation_key, "identity": identity.id,
                                 "invocation_key": invocation_key, "invocation_id": reply}
                return LifecycleHandle(self.backend, identity, reply, run_dir,
                    lambda fields: self.record({**fields, "creation_key": creation_key}))
            failure = {"class": "other" if isinstance(reply, (InvalidSpec, Unsupported)) else "launch",
                       "detail_code": "isolation_unavailable", "excerpt": getattr(reply, "reason", str(reply))}
        self.record({"failure": failure})
        resolved = await self._resolve_failure(creation_key, invocation_key, run_dir)
        if isinstance(resolved, Unknown):
            return resolved
        raise RuntimeError("isolation_unavailable: " + failure["excerpt"])


class LifecycleHandle:
    """Replay durable backend output, holding occupancy while state is unknown."""
    pid = None                      # no host pid is authority for a resource
    pid_start = ""
    timed_out = False
    offset = 0

    def __init__(self, backend, identity, invocation, run_dir, record):
        self.backend, self.identity, self.invocation = backend, identity, invocation
        self.run_dir, self.record = run_dir, record
        self.launched_at = time.time()
        self._state = None
        self._stderr = b""

    async def _inspect(self):
        self._state = await outcome(self.backend.inspect, self.identity)
        return self._state

    async def lines(self):
        offset, pending = 0, b""
        while True:
            state = await self._inspect()
            chunk = await outcome(self.backend.output, self.identity, self.invocation, "stdout", offset)
            if isinstance(chunk, bytes) and chunk:
                offset += len(chunk)
                if self.run_dir:
                    _write_run_file(self.run_dir, "output.ndjson", chunk, append=True)
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.offset += len(line) + 1
                    yield line.decode(errors="replace").rstrip("\r")
                continue
            if isinstance(chunk, Unknown):
                await asyncio.sleep(0.05)
                continue
            if isinstance(state, (Exited, Absent)):
                if pending:
                    self.offset += len(pending)
                    yield pending.decode(errors="replace")
                return
            await asyncio.sleep(0.05)

    async def drain_stderr(self):
        offset = 0
        while True:
            state = await self._inspect()
            chunk = await outcome(self.backend.output, self.identity, self.invocation, "stderr", offset)
            if isinstance(chunk, bytes) and chunk:
                offset += len(chunk)
                self._stderr = (self._stderr + chunk)[-65536:]
                if self.run_dir:
                    _write_run_file(self.run_dir, "stderr.log", chunk, append=True)
                continue
            if isinstance(chunk, Unknown):
                await asyncio.sleep(0.05)
                continue
            if isinstance(state, (Exited, Absent)):
                return
            await asyncio.sleep(0.05)

    @property
    def stderr_tail(self):
        return self._stderr.decode(errors="replace")

    @property
    def returncode(self):
        return self._state.code if isinstance(self._state, Exited) else None

    async def wait(self):
        while True:
            state = await self._inspect()
            if isinstance(state, (Exited, Absent)):
                self.record({"termination_confirmed": True})
                code = state.code if isinstance(state, Exited) else None
                if self.run_dir and code is not None:
                    _write_run_file(self.run_dir, "exit_status", str(code).encode())
                await outcome(self.backend.remove, self.identity, True)
                return code
            await asyncio.sleep(0.05)

    async def stop(self, grace=3.0):
        await outcome(self.backend.stop, self.identity, grace)
        await self.wait()
