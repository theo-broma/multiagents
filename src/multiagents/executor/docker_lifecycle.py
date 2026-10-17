"""One Docker container per attempt, with Docker as its supervisor."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from .lifecycle import (Absent, AlreadyInvoked, Created, Exited, IdentityConflict,
                        InvalidSpec, InvocationSpec, LaunchSpec, ReferenceBackend,
                        ResourceIdentity, Running, Unknown, Unsupported, _read,
                        _write, spec_digest)
from ..provision.seeds import SeedError


_ID = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED = ("owner", "lifetime", "creation_key", "spec_digest")
_INPUT = "/multiagents-input"
# The input mount is host-controlled and read-only. Frame bytes before Docker's
# JSON logger can replace invalid UTF-8; only the host decodes the durable logs.
from .log_entrypoint import ENTRYPOINT as _ENTRYPOINT


class DockerBackend(ReferenceBackend):
    """Docker CLI adapter; construction performs no I/O.

    Control records and input snapshots are never writable from the guest.
    Create records real mount-source paths; Docker's start sets up the mounts
    and OCI task. Those paths may be replaced with host-controlled links only
    after creation, preserving the recorded source and container identity.
    """

    name = "docker"
    version = "docker-1"

    def __init__(self, root: Path, *, seeds=None, cli="docker",
                 image="multiagents/workspace:latest", resources=None):
        super().__init__(root, seeds=seeds)
        self.cli = cli
        self.image = image
        self.resources = dict(resources or {})
        self._attachment_paths: set[Path] = set()
        self.network_registry = None
        self._log_reads = {}

    @property
    def _control(self):
        return self.root / ".docker-lifecycle"

    @property
    def _storage(self):
        return self.root / ".docker-guests"

    @contextlib.asynccontextmanager
    async def _lock(self):
        self._control.mkdir(parents=True, mode=0o700, exist_ok=True)
        with (self._control / "lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def _cli(self, *args):
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli, *args, stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise
            return proc.returncode, stdout, stderr
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError) as exc:
            return -1, b"", str(exc).encode()

    def _failure(self, result):
        code, out, err = result
        return Unknown(err.decode(errors="replace").strip() or
                       out.decode(errors="replace").strip() or
                       f"docker command failed ({code})")

    @staticmethod
    def _missing(result):
        return result[0] != 0 and any(text in result[2].lower() for text in
                                    (b"no such object:", b"no such container:"))

    def _host_path(self, value):
        if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
            return False
        try:
            path = Path(value).resolve()
            if not path.is_relative_to(self.root):
                return False
            for reserved in (self._control, self._storage,
                             self.root / ".lifecycle", self.root / ".guests"):
                if path.is_relative_to(reserved) or reserved.is_relative_to(path):
                    return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def _resources(self, overrides):
        # Config validates the same values for project.yaml. Public callers
        # still receive a typed refusal rather than an invalid Docker flag.
        from ..config import validate_isolated_resources
        defaults = {}
        for values in (self.resources, overrides):
            if not isinstance(values, dict):
                return InvalidSpec("resources must be a mapping")
            for key, value in values.items():
                if key not in ("memory", "pids", "cpu_shares", "cpus"):
                    return Unsupported("resources")
                if value is not None:
                    defaults[key] = value
        try:
            return validate_isolated_resources(defaults)
        except ValueError as exc:
            return InvalidSpec(str(exc))

    def _valid_spec(self, spec):
        if not isinstance(spec, LaunchSpec):
            return InvalidSpec("a LaunchSpec is required")
        # Preserve structural validation's precedence over unsupported fields.
        refusal = super()._valid_spec(dataclasses.replace(
            spec, resources={}, credential_handle=None,
            session_state=None, network_handle=None))
        if refusal is not None:
            return refusal
        if spec.network_handle is not None:
            if self.network_registry is None:
                return InvalidSpec("network_handle is outside the host authority")
            network = self.network_registry.resolve(spec.network_handle, spec.owner)
            if not isinstance(network, str):
                return network
            if os.getuid() == 0:
                return InvalidSpec("gateway isolation requires a non-root host UID")
        if any("multiagents." + key in spec.labels for key in _GENERATED):
            return InvalidSpec("generated multiagents labels are reserved")
        for value in spec.writable_paths + spec.readonly_paths:
            if not Path(value).exists():
                return InvalidSpec(f"mount source does not exist: {value}")
            if "docker.sock" in str(value) or "," in value or ":" in value:
                return InvalidSpec(f"invalid mount source: {value}")
        if set(spec.writable_paths) & set(spec.readonly_paths):
            return InvalidSpec("mount path cannot be both writable and read-only")
        for guest in (spec.guest_repo, spec.guest_home, spec.guest_temp):
            if "," in guest or ":" in guest or (Path(guest).is_relative_to(_INPUT)
                                                  or Path(_INPUT).is_relative_to(guest)):
                return InvalidSpec("guest path overlaps the host-controlled input mount")
        for writable in spec.writable_paths:
            for readonly in spec.readonly_paths:
                if (Path(writable).resolve().is_relative_to(Path(readonly).resolve()) or
                        Path(readonly).resolve().is_relative_to(Path(writable).resolve())):
                    return InvalidSpec("writable and read-only mounts must not overlap")
        for name in ("credential_handle", "session_state"):
            if getattr(spec, name):
                return Unsupported(name)
        if spec.lifetime == "conversation":
            return Unsupported("lifetime")
        resources = self._resources(spec.resources)
        return resources if not isinstance(resources, dict) else None

    async def probe(self):
        problems = []
        info = await self._cli("info", "--format", "{{json .}}")
        if info[0]:
            problems.append("docker daemon unavailable: " + self._failure(info).reason)
        else:
            try:
                value = json.loads(info[1])
                security = " ".join(value.get("SecurityOptions", [])).lower()
                if "userns" in security or "rootless" in security:
                    problems.append("docker user namespace mode not supported")
                if value.get("OSType", "linux") != "linux":
                    problems.append("docker Linux containers are required")
            except (TypeError, ValueError, AttributeError):
                problems.append("docker daemon returned invalid information")
        image = await self._cli("image", "inspect", self.image)
        if image[0]:
            problems.append(f"docker image unavailable: {self.image}: {self._failure(image).reason}")
        return problems

    def _path_for_key(self, key):
        return self._control / hashlib.sha256(key.encode()).hexdigest()

    def _local(self, identity):
        if not isinstance(identity, ResourceIdentity) or not _ID.fullmatch(identity.id):
            return None
        for filename in self._control.glob("*/resource.json"):
            if _read(filename).get("id") == identity.id:
                return filename.parent
        return None

    async def metadata(self, identity):
        """Docker's authoritative inspect object, including orphan metadata."""
        if not isinstance(identity, ResourceIdentity) or not _ID.fullmatch(identity.id):
            return Absent()
        result = await self._cli("inspect", identity.id)
        if self._missing(result):
            return Absent()
        if result[0]:
            return self._failure(result)
        try:
            value = json.loads(result[1])
            if (not isinstance(value, list) or len(value) != 1 or
                    value[0].get("Id") != identity.id or not isinstance(value[0].get("State"), dict)):
                raise ValueError("invalid inspect result")
            return value[0]
        except (ValueError, TypeError, AttributeError):
            return Unknown("docker inspect returned invalid data")

    def _state_of(self, metadata):
        if not isinstance(metadata, dict):
            return metadata
        state = metadata["State"]
        status = state.get("Status")
        if status == "created":
            return Created()
        if status == "running" and state.get("Running") is True:
            return Running()
        if status == "exited" and state.get("Running") is False:
            try:
                code = state["ExitCode"]
                oom = state["OOMKilled"]
                if type(code) is not int or type(oom) is not bool:
                    raise ValueError("invalid exit evidence")
                stamp = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00")).timestamp()
                return Exited(code, oom, stamp)
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                return Unknown("docker inspect returned invalid exit evidence")
        return Unknown(f"docker container state is uncertain: {status}")

    async def inspect(self, identity):
        return self._state_of(await self.metadata(identity))

    async def _list(self, label):
        result = await self._cli("ps", "-a", "-q", "--no-trunc", "--filter", "label=" + label)
        if result[0]:
            return self._failure(result)
        ids = result[1].decode(errors="replace").split()
        if any(not _ID.fullmatch(value) for value in ids):
            return Unknown("docker ps returned invalid container identities")
        return [ResourceIdentity(value) for value in ids]

    async def find(self, key):
        if not isinstance(key, str) or not key:
            return InvalidSpec("a non-empty creation key is required")
        result = await self._list("multiagents.creation_key=" + key)
        if not isinstance(result, list):
            return result
        if len(result) > 1:
            return Unknown("multiple containers have the same creation key")
        return result[0] if result else Absent()

    async def provision(self, spec, key):
        if not isinstance(key, str) or not key:
            return InvalidSpec("a non-empty creation key is required")
        refusal = self._valid_spec(spec)
        if refusal is not None:
            return refusal
        if spec.seed:
            if self.seeds is None:
                return InvalidSpec("seed_unavailable: no registry")
            try:
                self.seeds.record(spec.seed, spec.owner)
            except SeedError as exc:
                return InvalidSpec(str(exc))
        problems = await self.probe()
        if problems:
            return Unknown("; ".join(problems))
        digest = spec_digest(spec)
        async with self._lock():
            found = await self.find(key)
            if isinstance(found, ResourceIdentity):
                metadata = await self.metadata(found)
                if not isinstance(metadata, dict):
                    return metadata
                labels = metadata.get("Config", {}).get("Labels", {})
                if labels.get("multiagents.spec_digest") != digest:
                    return IdentityConflict()
                path = self._path_for_key(key)
                if (path / "resource.json").exists():
                    record = _read(path / "resource.json")
                    record["id"] = found.id
                    _write(path / "resource.json", record)
                return found
            if not isinstance(found, Absent):
                return found
            path = self._path_for_key(key)
            path.mkdir(mode=0o700, exist_ok=True)
            storage = self._storage / path.name
            storage.mkdir(parents=True, mode=0o700, exist_ok=True)
            for name in ("home", "tmp"):
                (storage / name).mkdir(mode=0o700, exist_ok=True)
            repo = storage / "repo"
            if not repo.exists():
                if spec.seed:
                    try:
                        with self.seeds.acquire(spec.seed, spec.owner) as bundle:
                            seed = self.seeds._authorised(spec.seed, spec.owner)
                            self._clone(bundle, repo, seed["checkout_commit"])
                        repo.chmod(0o700)
                    except SeedError as exc:
                        return InvalidSpec(str(exc))
                else:
                    repo.mkdir(mode=0o700)
            inputs = path / "input"
            inputs.mkdir(mode=0o700, exist_ok=True)
            record = {"key": key, "digest": digest, "spec": dataclasses.asdict(spec),
                      "repo_path": str(repo), "home_path": str(storage / "home")}
            _write(path / "resource.json", record)
            name = "multiagents-" + hashlib.sha256(key.encode()).hexdigest()
            network = (self.network_registry.resolve(spec.network_handle, spec.owner)
                       if spec.network_handle is not None else "none")
            if not isinstance(network, str):
                return network
            args = ["create", "--name", name, "--pull", "never", "--restart", "no",
                    "--network", network, "--user", "0:0" if spec.network_handle else f"{os.getuid()}:{os.getgid()}",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
                    "--log-driver", "json-file"]
            if spec.network_handle:
                args += ["--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "SETPCAP",
                         "--dns", "127.0.0.1"]
            labels = dict(spec.labels)
            labels.update({"multiagents.owner": spec.owner, "multiagents.lifetime": spec.lifetime,
                           "multiagents.creation_key": key, "multiagents.spec_digest": digest})
            for label, value in labels.items():
                args += ["--label", label + "=" + value]
            for name, guest in (("repo", spec.guest_repo), ("home", spec.guest_home),
                                ("tmp", spec.guest_temp)):
                readonly = ",readonly" if name == "repo" and spec.labels.get("multiagents.repo_readonly") == "true" else ""
                args += ["--mount", f"type=bind,source={storage / name},target={guest}{readonly}"]
            if spec.transport_handle:
                source = spec.transport_handle["directory"]
                args += ["--mount", f"type=bind,source={source},target=/run/multiagents/rpc,readonly"]
            for source in spec.writable_paths:
                args += ["--mount", f"type=bind,source={source},target={source}"]
            for source in spec.readonly_paths:
                args += ["--mount", f"type=bind,source={source},target={source},readonly"]
            args += ["--mount", f"type=bind,source={inputs},target={_INPUT},readonly"]
            resources = self._resources(spec.resources)
            for field, flag in (("memory", "--memory"), ("pids", "--pids-limit"),
                                ("cpu_shares", "--cpu-shares"), ("cpus", "--cpus")):
                if resources[field] is not None:
                    args += [flag, str(resources[field])]
            args += ["--entrypoint", "python3", self.image, "-c", _ENTRYPOINT]
            result = await self._cli(*args)
            if result[0] or not _ID.fullmatch(result[1].decode(errors="replace").strip()):
                found = await self.find(key)
                if not isinstance(found, ResourceIdentity):
                    return found if isinstance(found, Unknown) else self._failure(result)
            else:
                found = ResourceIdentity(result[1].decode().strip())
            record["id"] = found.id
            _write(path / "resource.json", record)
            return found

    async def checkout_path(self, identity):
        state = await self.inspect(identity)
        if isinstance(state, (Absent, Unknown)):
            return state
        path = self._local(identity)
        return Path(_read(path / "resource.json")["repo_path"]) if path else Unknown("host resource record missing")

    def _attachment(self, value, record):
        if not isinstance(value, (str, Path)) or "\0" in str(value) or not Path(value).is_absolute():
            return False
        try:
            path = Path(value).resolve()
            return path.is_dir() and (self._host_path(str(path)) or
                    path in {Path(p).resolve() for p in self._attachment_paths} or
                    str(path) in (record["repo_path"], record["home_path"]))
        except (OSError, RuntimeError, ValueError):
            return False

    async def attach_checkout(self, identity, checkout, home=None):
        async with self._lock():
            state = await self.inspect(identity)
            if isinstance(state, (Absent, Unknown)):
                return state
            if isinstance(state, Running):
                return InvalidSpec("cannot attach a live checkout")
            path = self._local(identity)
            if path is None:
                return Unknown("host resource record missing")
            record = _read(path / "resource.json")
            if not self._attachment(checkout, record) or (home is not None and not self._attachment(home, record)):
                return InvalidSpec("checkout or HOME is outside the host-issued authority")
            updates = {"repo": Path(checkout).resolve()}
            if home is not None:
                updates["home"] = Path(home).resolve()
            for name, target in updates.items():
                # Docker canonicalises symlinks at create time. The source
                # was a real directory then, so the daemon retains this exact
                # pathname and resolves the attachment when it mounts at start.
                link = self._storage / path.name / name
                if link.resolve() == target:
                    continue
                temporary = link.with_name(name + "." + uuid.uuid4().hex)
                temporary.symlink_to(target, target_is_directory=True)
                if link.is_dir() and not link.is_symlink():
                    link.rename(link.with_name("unattached-" + name))
                temporary.replace(link)
                record[name + "_path"] = str(target)
            _write(path / "resource.json", record)
            return identity

    async def prepare_input(self, identity, source):
        state = await self.inspect(identity)
        if isinstance(state, (Absent, Unknown)):
            return state
        path = self._local(identity)
        if path is None:
            return Unknown("host resource record missing")
        target = path / "input" / ("stdin-" + uuid.uuid4().hex)
        try:
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, output)
            return str(target)
        except (TypeError, AttributeError, ValueError) as exc:
            target.unlink(missing_ok=True)
            return InvalidSpec(str(exc))
        except OSError as exc:
            target.unlink(missing_ok=True)
            return Unknown(str(exc))

    async def run(self, identity, invocation, key):
        if not isinstance(key, str) or not key:
            return InvalidSpec("a non-empty invocation key is required")
        async with self._lock():
            state = await self.inspect(identity)
            if isinstance(state, (Absent, Unknown)):
                return state
            path = self._local(identity)
            if path is None:
                return Unknown("host resource record missing")
            # Input snapshots are resource-specific host authority, not a
            # general permission to name arbitrary files below the root.
            stdin = getattr(invocation, "stdin", None)
            if stdin is not None:
                try:
                    source = Path(stdin).resolve()
                    if not source.is_file() or not source.is_relative_to(path / "input"):
                        return InvalidSpec("stdin is not an input snapshot for this resource")
                except (TypeError, OSError, ValueError, RuntimeError):
                    return InvalidSpec("invalid stdin input snapshot")
            refusal = self._valid_invocation(dataclasses.replace(invocation, stdin=None)
                                             if isinstance(invocation, InvocationSpec) else invocation)
            if refusal is not None:
                return refusal
            record = _read(path / "resource.json")
            spec = record["spec"]
            if not any(invocation.cwd == p or invocation.cwd.startswith(p.rstrip("/") + "/")
                       for p in [spec["guest_repo"], spec["guest_home"], spec["guest_temp"],
                                 *spec["writable_paths"], *spec["readonly_paths"]]):
                return InvalidSpec("cwd is not an authorised guest path")
            digest = spec_digest(invocation)
            previous = record.get("invocation")
            if previous:
                if previous["key"] != key:
                    return state if isinstance(state, Exited) else AlreadyInvoked()
                if previous["digest"] != digest:
                    return IdentityConflict()
                if not isinstance(state, Created):
                    return key
            elif not isinstance(state, Created):
                return state if isinstance(state, Exited) else AlreadyInvoked()
            env = dict(invocation.env)
            env.update(HOME=spec["guest_home"], TMPDIR=spec["guest_temp"])
            payload = {"argv": invocation.argv, "env": env, "cwd": invocation.cwd,
                       "stdin": _INPUT + "/" + Path(stdin).name if stdin else None,
                       "output_prefix": "multiagents-output-" + uuid.uuid4().hex + ":"}
            if spec.get("network_handle"):
                from ..scheduler.gateways import redaction_values
                payload.update(redaction_secrets=redaction_values(env), uid=os.getuid(), gid=os.getgid())
            _write(path / "input" / "invocation.json", payload)
            record["invocation"] = {"key": key, "digest": digest,
                                    "output_prefix": payload["output_prefix"]}
            _write(path / "resource.json", record)
            result = await self._cli("start", identity.id)
            after = await self.inspect(identity)
            if isinstance(after, (Running, Exited)):
                return key
            return after if isinstance(after, (Absent, Unknown)) else self._failure(result)

    async def inspect_invocation(self, identity, invocation_id):
        state = await self.inspect(identity)
        if isinstance(state, (Absent, Unknown)):
            return state
        if isinstance(state, Created):
            # The durable input is an invocation intent. Docker's created
            # state proves it never started, even if that intent was written.
            return Absent()
        path = self._local(identity)
        if path is None:
            return Absent()
        invocation = _read(path / "resource.json").get("invocation", {})
        return state if invocation.get("key") == invocation_id else Absent()

    async def output(self, identity, invocation_id, stream, offset):
        if stream not in ("stdout", "stderr") or type(offset) is not int or offset < 0:
            return InvalidSpec("invalid output stream or offset")
        path = self._local(identity)
        invocation = _read(path / "resource.json").get("invocation", {}) if path else {}
        if invocation.get("key") != invocation_id:
            return Absent()
        # Logs itself reports absence and daemon uncertainty. An extra inspect
        # adds no authority here and delays draining both streams after exit.
        # Docker returns both streams together. Concurrent readers can share
        # the command without retaining a stale log snapshot between polls.
        read = self._log_reads.get(identity.id)
        if read is None or read.done():
            read = asyncio.create_task(self._cli("logs", identity.id))
            self._log_reads[identity.id] = read
        result = await asyncio.shield(read)
        if self._missing(result):
            return Absent()
        if result[0]:
            return self._failure(result)
        data = result[1 if stream == "stdout" else 2]
        prefix = invocation.get("output_prefix", "").encode("ascii")
        # Older resources and entrypoint startup diagnostics have raw logs.
        if prefix and data.startswith(prefix):
            try:
                if not data.endswith(b"\n"):
                    raise ValueError("incomplete output frame")
                chunks = []
                for line in data.split(b"\n")[:-1]:
                    if not line.startswith(prefix):
                        raise ValueError("invalid output frame")
                    chunks.append(base64.b64decode(line[len(prefix):], validate=True))
                data = b"".join(chunks)
            except ValueError as exc:
                return Unknown(str(exc))
        return data[offset:]

    async def stop(self, identity, grace):
        state = await self.inspect(identity)
        if isinstance(state, (Absent, Exited, Unknown)):
            return state
        if isinstance(state, Created):
            return Exited(None)
        # Docker stop sends TERM and then KILL after the requested grace.
        await self._cli("stop", "--time", str(max(0, math.ceil(grace))), identity.id)
        state = await self.inspect(identity)
        if isinstance(state, (Exited, Absent, Unknown)):
            return state
        await self._cli("kill", identity.id)
        state = await self.inspect(identity)
        if isinstance(state, Created):
            # A container never started has no live processes; Docker does not
            # transition Created on stop. Retention must still preserve it.
            return Exited(None)
        return state if isinstance(state, (Exited, Absent, Unknown)) else Unknown("container termination could not be confirmed")

    async def remove(self, identity, keep_artifacts):
        state = await self.stop(identity, 0.1)
        if isinstance(state, Unknown):
            return state
        if keep_artifacts:
            return state
        if not isinstance(state, Absent):
            result = await self._cli("rm", identity.id)
            after = await self.inspect(identity)
            if not isinstance(after, Absent):
                return after if isinstance(after, Unknown) else self._failure(result)
        path = self._local(identity)
        if path is not None:
            storage = self._storage / path.name
            if storage.exists():
                shutil.rmtree(storage)
            shutil.rmtree(path)
        return Absent()

    async def list_owned(self, project):
        return await self._list("multiagents.project=" + project)
