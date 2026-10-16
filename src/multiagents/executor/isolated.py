"""Adapt the resource lifecycle to Runner's streaming executor seam.

Only host configuration constructs a backend. The reference backend is a
bookkeeping double, injected by tests; it never supplies isolation evidence.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
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

    def __init__(self, paths=None, providers=None, config=None):
        self.paths = paths
        self.providers = providers or {}
        self.backend = None
        self.config = dict(config or {})
        self.creation_key = ""
        self.identity = ""
        self.termination_confirmed = False
        self.backend_name = "docker"
        self.version = "unavailable"
        self.problem = "isolated docker lifecycle is not implemented yet"
        self.record = lambda fields: None
        from ..provision.seeds import SeedRegistry
        self.seeds = SeedRegistry(paths) if paths else None
        target = os.environ.get("MULTIAGENTS_TEST_ISOLATED_BACKEND")
        if not target:
            self._configure(self.config)
        if target:
            try:
                module, factory = target.split(":", 1)
                root = paths.data / "isolated" if paths else Path.cwd() / ".isolated"
                self.backend = getattr(importlib.import_module(module), factory)(root, seeds=self.seeds)
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

    def _configure(self, settings):
        from .lifecycle import DockerBackend
        self.config = dict(settings or {})
        self.backend_name = self.config.get("backend", "docker")
        self.backend = None
        if self.backend_name != "docker":
            self.problem = f"unknown isolated backend: {self.backend_name}"
            return
        docker = self.config.get("docker") or {}
        root = self.paths.data / "isolated" if self.paths else Path.cwd() / ".isolated"
        self.backend = DockerBackend(root, seeds=self.seeds,
            cli=docker.get("cli", "docker"), image=docker.get("image", "multiagents/workspace:latest"),
            resources=self.config.get("resources"))
        self.version, self.problem = self.backend.version, ""

    def restore(self, evidence):
        # These keys come from the host attempt store, never from a pid file.
        if evidence.get("backend") == "docker" and not os.environ.get("MULTIAGENTS_TEST_ISOLATED_BACKEND"):
            self._configure(evidence.get("backend_config", self.config))
        if evidence.get("backend") and evidence["backend"] != self.backend_name:
            self.backend, self.problem = None, "recorded backend is unavailable"
        if evidence.get("backend_version") and evidence["backend_version"] != self.version:
            self.backend, self.problem = None, "recorded backend version is incompatible"
        self.creation_key = evidence.get("creation_key", "")
        self.identity = evidence.get("identity", "")
        self.termination_confirmed = evidence.get("termination_confirmed", False)

    def wrapper_verdict(self, run_id):
        if not self.creation_key and not self.identity and self.termination_confirmed:
            return False
        if self.backend is None or not (self.creation_key or self.identity):
            return None

        async def inspect():
            identity = (ResourceIdentity(self.identity) if self.identity else
                        await outcome(self.backend.find, self.creation_key))
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

    def bind(self, owner, record, *, previous=None, begin=None, attempt=None,
             checkout=None, branch="", authority=None):
        self.record = record
        self.begin = begin or record
        self.previous = dict(previous or {})
        self.creation_key = self.previous.get("creation_key", "")
        self.identity = self.previous.get("identity", "")
        self.termination_confirmed = self.previous.get("termination_confirmed", False)
        self.attempt, self.checkout, self.branch, self.authority = attempt, checkout, branch, authority
        self.spec = LaunchSpec(owner=owner, lifetime="attempt",
                               labels={"multiagents.project": self.paths.slug if self.paths else ""})
        if attempt and attempt.get("writes") is False:
            self.spec.labels["multiagents.repo_readonly"] = "true"

    async def _install_checkout(self, identity):
        """Bridge the reference guest storage to the host's guarded capture path."""
        if not self.attempt or not self.checkout or not self.authority:
            return
        from ..scheduler.sessions import checkout_swap, install_checkout, swap_intent
        from ..provision.git import git_command, git_env
        path = Path(self.checkout)
        home = self.paths.home(self.attempt["alias_home"]) if self.attempt.get("alias_id") else None
        if not self.attempt.get("cleanup"):
            staged = path.with_name(".seed-" + self.attempt["attempt_id"])
            intent = checkout_swap(self.paths, self.attempt)
            if intent.get("state") != "done":
                if intent.get("state") != "swapping" and not intent.get("complete"):
                    source = await self.backend.checkout_path(identity)
                    if not isinstance(source, Path):
                        raise RuntimeError("provisioned checkout is missing")
                    if staged.exists():
                        shutil.rmtree(staged)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source, staged, symlinks=True)
                    subprocess.run(git_command("-C", str(staged), "checkout", "-b", self.branch,
                                               self.attempt["input_commit"]),
                                   env=git_env(), check=True, capture_output=True)
                    swap_intent(self.paths, self.attempt, path, staged, complete=True)
            install_checkout(self.paths, self.authority, path, staged, self.attempt)
        self.backend._attachment_paths.update(value.resolve() for value in (path, home) if value is not None)
        reply = await self.backend.attach_checkout(identity, path, home)
        if not isinstance(reply, ResourceIdentity):
            raise RuntimeError(getattr(reply, "reason", "checkout attachment failed"))

    def _uncertain(self, creation_key, operation, reply):
        return Unknown(f"predecessor {creation_key}: {operation}: "
                       f"{getattr(reply, 'reason', str(reply))}")

    async def predecessor_verdict(self):
        """Probe recorded resource liveness without polling an unknown backend."""
        if not self.creation_key and not self.identity and self.termination_confirmed:
            return False
        if self.backend is None or not (self.creation_key or self.identity):
            return self._uncertain(self.creation_key, "find", "backend or key unavailable")
        identity = (ResourceIdentity(self.identity) if self.identity else
                    await outcome(self.backend.find, self.creation_key))
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
        if self.identity and creation_key == self.creation_key:
            if isinstance(identity, Absent):
                identity = ResourceIdentity(self.identity)
            elif isinstance(identity, ResourceIdentity) and identity.id != self.identity:
                return self._uncertain(creation_key, "find", "recorded identity differs")
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
            if (self.previous.get("attempt_id") == self.spec.owner
                    and not self.previous.get("invocation_id")):
                return Unknown("own interrupted launch requires unlaunched-claim recovery: "
                               + self.previous["creation_key"])
            resolved = await self._resolve_failure(self.previous["creation_key"],
                                                   self.previous.get("invocation_key", ""), None)
            if isinstance(resolved, Unknown):
                return resolved
        launch_id = uuid.uuid4().hex
        creation_key = self.creation_key = "resource-" + launch_id
        self.identity = ""
        invocation_key = "invocation-" + launch_id
        self.termination_confirmed = False
        fields = {"kind": self.kind, "attempt_id": self.spec.owner, "backend": self.backend_name,
                    "backend_version": self.version, "backend_config": self.config, "spec_version": self.spec.spec_version,
                    "creation_key": creation_key, "session_resume": "unsupported"}
        try:
            if self.attempt:
                if self.previous.get("seed") and self.previous.get("attempt_id") == self.spec.owner:
                    self.spec.seed = self.seeds.restore(self.previous["seed"], self.spec.owner)
                elif self.attempt.get("input_commit"):
                    self.spec.seed = self.seeds.build(self.spec.owner, self.attempt["input_commit"])
                else:
                    raise ValueError("seed_required")
                fields["seed"] = self.seeds.record(self.spec.seed, self.spec.owner)
                if self.attempt.get("alias_id") and not self.attempt.get("cleanup"):
                    from ..scheduler.sessions import swap_intent
                    path = Path(self.checkout)
                    swap_intent(self.paths, self.attempt, path,
                                path.with_name(".seed-" + self.attempt["attempt_id"]))
            fields["spec_digest"] = spec_digest(self.spec)
        except Exception as exc:
            from ..provision.seeds import SeedError
            terminal = isinstance(exc, (SeedError, ValueError))
            failure = {"class": "other" if terminal else "launch", "retryable": not terminal,
                       "detail_code": "isolation_unavailable", "excerpt": str(exc)}
            self.begin({**fields, "failure": failure, "termination_confirmed": True})
            raise RuntimeError("isolation_unavailable: " + str(exc)) from exc
        self.begin(fields)
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
            self.identity = identity.id
            self.record({"identity": identity.id, "invocation_key": invocation_key})
            try:
                await self._install_checkout(identity)
            except Exception as exc:
                failure = {"class": "launch", "detail_code": "isolation_unavailable", "excerpt": str(exc)}
                self.record({"failure": failure})
                resolved = await self._resolve_failure(creation_key, invocation_key, run_dir)
                if isinstance(resolved, Unknown):
                    return resolved
                raise RuntimeError("isolation_unavailable: " + str(exc)) from exc
            environment = strip_run_environment(dict(self.adapter_env(argv, env, provider)))
            from ..agentwrap import prompt_stdin
            source = prompt_stdin(environment)
            stdin = None
            if hasattr(source, "close"):
                try:
                    stdin = await self.backend.prepare_input(identity, source)
                finally:
                    source.close()
                if not isinstance(stdin, str):
                    self.record({"failure": {"class": "launch", "detail_code": "isolation_unavailable",
                                              "excerpt": getattr(stdin, "reason", "input preparation failed")}})
                    resolved = await self._resolve_failure(creation_key, invocation_key, run_dir)
                    if isinstance(resolved, Unknown):
                        return resolved
                    raise RuntimeError("isolation_unavailable: input preparation failed")
            for name in ("MULTIAGENTS_RPC_TOKEN", "MULTIAGENTS_RPC_SOCKET"):
                environment.pop(name, None)
            mappings = {str(Path.home()): self.spec.guest_home, str(cwd): self.spec.guest_repo}
            if environment.get("HOME"):
                mappings[environment["HOME"]] = self.spec.guest_home
            if environment.get("TMPDIR"):
                mappings[environment["TMPDIR"]] = self.spec.guest_temp
            for key, value in environment.items():
                for source in sorted(mappings, key=len, reverse=True):
                    if value == source or value.startswith(source.rstrip("/") + "/"):
                        environment[key] = mappings[source] + value[len(source):]
                        break
            for name in ("MULTIAGENTS_USER_HOME", "MULTIAGENTS_ROOT", "MULTIAGENTS_PROJECT"):
                environment.pop(name, None)
            environment.update(HOME=self.spec.guest_home, TMPDIR=self.spec.guest_temp)
            environment.update(XDG_CONFIG_HOME=self.spec.guest_home + "/.config",
                               XDG_DATA_HOME=self.spec.guest_home + "/.local/share",
                               XDG_CACHE_HOME=self.spec.guest_home + "/.cache",
                               XDG_STATE_HOME=self.spec.guest_home + "/.local/state",
                               XDG_RUNTIME_DIR=self.spec.guest_home + "/.runtime")
            invocation = InvocationSpec(argv=list(argv), env=environment, cwd=self.spec.guest_repo,
                                        stdin=stdin, kind="agent")
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

    def _resume_stream(self, stream):
        name = "output.ndjson" if stream == "stdout" else "stderr.log"
        offset = length = 0
        existing = b""
        if self.run_dir:
            directory = gitops._open_beneath(*gitops.beneath(self.run_dir), create=True)
            try:
                try:
                    fd = os.open("checkpoint-" + stream + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    with os.fdopen(fd) as source:
                        record = json.load(source)
                    if record.get("invocation") == self.invocation:
                        offset, length = record["offset"], record["length"]
                except (OSError, ValueError, TypeError):
                    pass
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, "r+b") as target:
                    target.truncate(length)
                    existing = target.read()
            finally:
                os.close(directory)
        return offset, length, existing

    def _append_stream(self, stream, data, offset, length):
        if not self.run_dir:
            return
        name = "output.ndjson" if stream == "stdout" else "stderr.log"
        # A detached supervisor may survive the scheduler restart. Restore
        # its last committed length too, before appending the next chunk.
        fd = gitops._open_file_beneath(*gitops.beneath(self.run_dir), name,
                                      os.O_WRONLY, create=True)
        try:
            os.ftruncate(fd, length - len(data))
        finally:
            os.close(fd)
        _write_run_file(self.run_dir, name, data, append=True)
        # The append reaches disk before its atomic checkpoint. Recovery
        # truncates any uncheckpointed append before replaying backend bytes.
        directory = gitops._open_beneath(*gitops.beneath(self.run_dir), create=True)
        temporary = ".checkpoint-" + uuid.uuid4().hex
        try:
            output = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                os.fsync(output)
            finally:
                os.close(output)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            with os.fdopen(fd, "w") as target:
                json.dump({"invocation": self.invocation, "offset": offset, "length": length}, target)
                target.flush()
                os.fsync(target.fileno())
            os.rename(temporary, "checkpoint-" + stream + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)

    async def _stream(self, stream):
        offset, length, existing = self._resume_stream(stream)
        # Replay checkpointed bytes to rebuild the provider parser, without
        # appending them again. Runner's follow checkpoint suppresses effects.
        for line in existing.splitlines(keepends=True):
            yield line
        pending = b""
        while True:
            state = await self._inspect()
            chunk = await outcome(self.backend.output, self.identity, self.invocation, stream, offset + len(pending))
            if isinstance(chunk, bytes) and chunk:
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    data = line + b"\n"
                    offset += len(data)
                    length += len(data)
                    self._append_stream(stream, data, offset, length)
                    yield data
                continue
            if isinstance(chunk, Unknown) or isinstance(state, Unknown):
                await asyncio.sleep(0.05)
                continue
            if isinstance(state, (Exited, Absent)):
                if pending:
                    offset += len(pending)
                    length += len(pending)
                    self._append_stream(stream, pending, offset, length)
                    yield pending
                return
            await asyncio.sleep(0.05)

    async def lines(self):
        async for data in self._stream("stdout"):
            self.offset += len(data)
            yield data.decode(errors="replace").rstrip("\r\n")

    async def drain_stderr(self):
        async for data in self._stream("stderr"):
            self._stderr = (self._stderr + data)[-65536:]

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
                if isinstance(state, Absent):
                    self.record({"failure": {"class": "launch", "retryable": True,
                        "excerpt": "isolated resource is absent: lost launch " + self.identity.id}})
                self.record({"termination_confirmed": True, "exit_code": getattr(state, "code", None),
                             "oom": bool(getattr(state, "oom", False)),
                             "finished_at": getattr(state, "finished_at", None)})
                code = state.code if isinstance(state, Exited) else None
                if self.run_dir and code is not None:
                    _write_run_file(self.run_dir, "exit_status", str(code).encode())
                await outcome(self.backend.remove, self.identity, True)
                return code
            await asyncio.sleep(0.05)

    async def stop(self, grace=3.0):
        state = await outcome(self.backend.stop, self.identity, grace)
        if not isinstance(state, (Exited, Absent)):
            return state
        return await self.wait()
