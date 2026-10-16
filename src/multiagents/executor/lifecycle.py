"""The asynchronous resource lifecycle seam and its bookkeeping-only backend.

ReferenceBackend runs host processes. It is useful for conformance and scheduler
checks, and deliberately makes no claim to contain untrusted commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, BinaryIO, Protocol
import uuid

from ..provision.git import git_command, git_env
from ..provision.seeds import SeedError


@dataclasses.dataclass
class LaunchSpec:
    spec_version: int = 1
    owner: str = ""
    lifetime: str = "attempt"
    labels: dict[str, str] = dataclasses.field(default_factory=dict)
    writable_paths: list[str] = dataclasses.field(default_factory=list)
    readonly_paths: list[str] = dataclasses.field(default_factory=list)
    guest_repo: str = "/work/repo"
    guest_home: str = "/work/home"
    guest_temp: str = "/work/tmp"
    seed: Any = None
    session_state: Any = None
    resources: dict = dataclasses.field(default_factory=dict)
    credential_handle: Any = None
    transport_handle: Any = None


@dataclasses.dataclass
class InvocationSpec:
    argv: list[str]
    env: dict[str, str]
    cwd: str
    stdin: str | None = None
    kind: str = "agent"


@dataclasses.dataclass(frozen=True)
class ResourceIdentity:
    id: str


@dataclasses.dataclass(frozen=True)
class InvalidSpec:
    reason: str


@dataclasses.dataclass(frozen=True)
class Unsupported:
    field: str


@dataclasses.dataclass(frozen=True)
class IdentityConflict:
    pass


@dataclasses.dataclass(frozen=True)
class AlreadyInvoked:
    pass


@dataclasses.dataclass(frozen=True)
class Absent:
    pass


@dataclasses.dataclass(frozen=True)
class Created:
    pass


@dataclasses.dataclass(frozen=True)
class Running:
    pass


@dataclasses.dataclass(frozen=True)
class Exited:
    code: int | None
    oom: bool = False
    finished_at: float | None = None


@dataclasses.dataclass(frozen=True)
class Unknown:
    reason: str


State = Absent | Created | Running | Exited | Unknown


class Backend(Protocol):
    """Resource and invocation operations; unexpected errors belong to callers.

    Implementations return typed refusals for expected conditions. An adapter
    turns unexpected exceptions into Unknown, while propagating cancellation.
    """

    version: str

    async def probe(self) -> list[str]: ...
    async def provision(self, spec: LaunchSpec, key: str) -> ResourceIdentity | InvalidSpec | Unsupported | IdentityConflict: ...
    async def find(self, key: str) -> ResourceIdentity | Absent | Unknown: ...
    async def inspect(self, identity: ResourceIdentity) -> State: ...
    async def run(self, identity: ResourceIdentity, invocation: InvocationSpec, key: str) -> str | State | InvalidSpec | AlreadyInvoked | IdentityConflict: ...
    async def inspect_invocation(self, identity: ResourceIdentity, invocation_id: str) -> State: ...
    async def output(self, identity: ResourceIdentity, invocation_id: str, stream: str, offset: int) -> bytes | Absent | InvalidSpec: ...
    async def stop(self, identity: ResourceIdentity, grace: float) -> State: ...
    async def remove(self, identity: ResourceIdentity, keep_artifacts: bool) -> State: ...
    async def list_owned(self, project: str) -> list[ResourceIdentity]: ...
    async def checkout_path(self, identity: ResourceIdentity) -> Path | Absent | Unknown: ...
    async def attach_checkout(self, identity: ResourceIdentity, checkout: Path, home: Path | None = None) -> ResourceIdentity | Absent | InvalidSpec | Unknown: ...
    async def prepare_input(self, identity: ResourceIdentity, source: BinaryIO) -> str | Absent | InvalidSpec | Unknown: ...


def spec_digest(spec: LaunchSpec | InvocationSpec) -> str:
    """SHA-256 of the schema's canonical UTF-8 JSON representation."""
    value = dataclasses.asdict(spec)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _start_time(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _alive(record: dict) -> bool:
    pid = record.get("pid", 0)
    if not pid or _start_time(pid) != record.get("pid_start"):
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


# The supervisor persists exit evidence even if the client or its event loop
# disappears. It stays in its own session with the command. Caught SIGTERM is
# reset to the default by exec in the child, while the supervisor records exit.
_SUPERVISOR = r'''
import json, os, pathlib, signal, subprocess, sys, time
payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
exit_path = pathlib.Path(sys.argv[2])
signal.signal(signal.SIGTERM, lambda *_: None)
try:
    transport = payload.get('transport_directory')
    if transport and len(os.fsencode(payload['env'].get('MULTIAGENTS_RPC_SOCKET', ''))) > 107:
        # Keep this descriptor in the surviving supervisor, just as a guest
        # mount keeps the endpoint directory reachable across host restarts.
        transport_fd = os.open(transport, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        payload['env']['MULTIAGENTS_RPC_SOCKET'] = '/proc/%d/fd/%d/rpc.sock' % (os.getpid(), transport_fd)
    source = open(payload['stdin'], 'rb') if payload['stdin'] else subprocess.DEVNULL
    with open(payload['stdout'], 'wb') as out, open(payload['stderr'], 'wb') as err:
        child = subprocess.Popen(payload['argv'], cwd=payload['cwd'], env=payload['env'],
                                 stdin=source, stdout=out, stderr=err)
        exit_path.with_name('ready').touch()
        code = child.wait()
except BaseException as exc:
    with open(payload['stderr'], 'ab') as err:
        err.write((str(exc) + '\n').encode())
    code = 127
result = {'code': code, 'oom': False, 'finished_at': time.time()}
tmp = exit_path.with_suffix('.pending')
tmp.write_text(json.dumps(result))
tmp.replace(exit_path)
'''


class ReferenceBackend:
    """Persistent host-process bookkeeping, never an isolation backend.

    Bookkeeping and guest storage have separate trees. Stop uses the supervisor
    identity captured at launch, never PID files beside guest HOME or temp.
    """

    version = "reference-2"
    _processes: dict[tuple[str, int], subprocess.Popen] = {}
    _records: dict[tuple[str, str, str], dict] = {}

    def __init__(self, root: Path, *, seeds=None):
        self.root = Path(root).resolve()
        self.seeds = seeds
        self._attachment_paths: set[Path] = set()
        self._transport_paths: set[Path] = set()

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        control = self.root / ".lifecycle"
        control.mkdir(mode=0o700, exist_ok=True)
        with (control / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield control

    def _resource(self, control: Path, identity: ResourceIdentity) -> Path | None:
        name = getattr(identity, "id", "")
        if not isinstance(name, str) or len(name) != 32 or any(c not in "0123456789abcdef" for c in name):
            return None
        path = control / name
        return path if (path / "resource.json").is_file() else None

    def _guest_resource(self, identity: ResourceIdentity) -> Path:
        return self.root / ".guests" / identity.id

    def _record(self, path: Path, record_path: Path) -> dict:
        """Prefer host memory to mutable files for process ownership."""
        key = (str(self.root), path.name, record_path.parent.name)
        if key not in self._records:
            record = _read(record_path)
            directory = "inv-" + hashlib.sha256(record["key"].encode()).hexdigest()
            if directory != record_path.parent.name or record["directory"] != directory:
                raise ValueError("invalid host invocation record")
            self._records[key] = record
        return self._records[key].copy()

    def _invocation_records(self, path: Path) -> list[dict]:
        for record_path in path.glob("inv-*/invocation.json"):
            self._record(path, record_path)
        return [record.copy() for key, record in list(self._records.items())
                if key[:2] == (str(self.root), path.name)]

    def _valid_spec(self, spec: LaunchSpec) -> InvalidSpec | Unsupported | None:
        if not isinstance(spec, LaunchSpec):
            return InvalidSpec("a LaunchSpec is required")
        if type(spec.spec_version) is not int or spec.spec_version != 1:
            return InvalidSpec("unsupported spec version")
        if spec.lifetime not in ("attempt", "conversation") or not isinstance(spec.owner, str) or not spec.owner:
            return InvalidSpec("owner and a valid lifetime are required")
        if not isinstance(spec.labels, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                    for key, value in spec.labels.items()):
            return InvalidSpec("labels must map strings to strings")
        for name in ("writable_paths", "readonly_paths"):
            if not isinstance(getattr(spec, name), list):
                return InvalidSpec(f"{name} must be a list of host paths")
            for value in getattr(spec, name):
                if not self._host_path(value):
                    return InvalidSpec(f"{name}: host path is outside the host-issued root")
        guests = [spec.guest_repo, spec.guest_home, spec.guest_temp]
        for guest in guests:
            if (not isinstance(guest, str) or "\0" in guest or not guest.startswith("/")
                    or ".." in PurePosixPath(guest).parts or guest == "/"
                    or str(PurePosixPath(guest)) != guest):
                return InvalidSpec("guest paths must be absolute paths below root")
        for index, guest in enumerate(guests):
            for other in guests[index + 1:]:
                if PurePosixPath(guest).is_relative_to(other) or PurePosixPath(other).is_relative_to(guest):
                    return InvalidSpec("guest paths must not overlap")
        if spec.transport_handle is not None:
            handle = spec.transport_handle
            if (not isinstance(handle, dict) or set(handle) != {"directory", "attempt_id"}
                    or not isinstance(handle.get("directory"), str)
                    or not isinstance(handle.get("attempt_id"), str)):
                return InvalidSpec("transport_handle must be a host-issued endpoint")
            try:
                directory = Path(handle["directory"])
                if (not directory.is_absolute() or directory.is_symlink()
                        or directory.resolve() not in self._transport_paths
                        or directory.name != handle["attempt_id"] or not directory.is_dir()):
                    return InvalidSpec("transport_handle is outside the host-issued authority")
            except (OSError, ValueError, RuntimeError):
                return InvalidSpec("invalid transport_handle")
        for name in ("session_state", "resources", "credential_handle"):
            if getattr(spec, name):
                return Unsupported(name)
        try:
            spec_digest(spec)
        except (TypeError, ValueError):
            return InvalidSpec("launch spec must contain JSON values")
        return None

    def _host_path(self, value: Any) -> bool:
        if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
            return False
        try:
            path = Path(value).resolve()
            if not path.is_relative_to(self.root):
                return False
            # Neither a declared path nor one of its ancestors may expose
            # host bookkeeping or another invocation's private storage.
            for reserved in (self.root / ".lifecycle", self.root / ".guests"):
                if path.is_relative_to(reserved) or reserved.is_relative_to(path):
                    return False
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def _valid_invocation(self, invocation: InvocationSpec) -> InvalidSpec | None:
        if not isinstance(invocation, InvocationSpec):
            return InvalidSpec("an InvocationSpec is required")
        if (not isinstance(invocation.argv, list) or not invocation.argv
                or any(not isinstance(arg, str) or "\0" in arg for arg in invocation.argv)
                or not invocation.argv[0]):
            return InvalidSpec("argv must be a non-empty list of command arguments")
        if not isinstance(invocation.env, dict) or any(
                not isinstance(name, str) or not name or "=" in name or "\0" in name
                or not isinstance(value, str) or "\0" in value
                for name, value in invocation.env.items()):
            return InvalidSpec("env must map valid environment names to string values")
        if invocation.kind not in ("agent", "turn", "user_shell"):
            return InvalidSpec("a valid invocation kind is required")
        if (not isinstance(invocation.cwd, str) or "\0" in invocation.cwd
                or not invocation.cwd.startswith("/") or ".." in PurePosixPath(invocation.cwd).parts):
            return InvalidSpec("cwd must be an absolute authorised guest path")
        if invocation.stdin is not None and not self._host_path(invocation.stdin):
            return InvalidSpec("stdin is outside the host-issued root")
        try:
            spec_digest(invocation)
        except (TypeError, ValueError):
            return InvalidSpec("invocation spec must contain JSON values")
        return None

    async def probe(self) -> list[str]:
        return []

    async def provision(self, spec: LaunchSpec, key: str):
        if not isinstance(key, str) or not key:
            return InvalidSpec("a non-empty creation key is required")
        refusal = self._valid_spec(spec)
        if refusal is not None:
            return refusal
        if spec.seed and self.seeds is None:
            return InvalidSpec("seed_unavailable: no registry")
        digest = spec_digest(spec)
        with self._locked() as control:
            identity = None
            for path in control.glob("*/resource.json"):
                record = _read(path)
                if record["key"] == key:
                    if record["digest"] != digest:
                        return IdentityConflict()
                    identity = ResourceIdentity(path.parent.name)
                    # A completed resource, especially a running one, is never
                    # reset by another provisioning request.
                    if (path.parent / "clone.json").is_file() or self._invocation_records(path.parent):
                        return identity
                    break
            if identity is None:
                # Validate authority before creating anything find() can see.
                if spec.seed:
                    try:
                        self.seeds.record(spec.seed, spec.owner)
                    except SeedError as exc:
                        return InvalidSpec(str(exc))
                identity = ResourceIdentity(uuid.uuid4().hex)
            path = control / identity.id
            path.mkdir(mode=0o700, exist_ok=True)
            (self.root / ".guests").mkdir(mode=0o700, exist_ok=True)
            guest = self._guest_resource(identity)
            guest.mkdir(mode=0o700, exist_ok=True)
            _write(path / "resource.json", {"key": key, "digest": digest,
                                           "spec": dataclasses.asdict(spec), "removed": False})
            repo = guest / "repo"
            if repo.is_symlink():
                repo.unlink()
            elif repo.exists():
                shutil.rmtree(repo)
            if spec.seed:
                try:
                    with self.seeds.acquire(spec.seed, spec.owner) as bundle:
                        seed = self.seeds._authorised(spec.seed, spec.owner)
                        self._clone(bundle, repo, seed["checkout_commit"])
                except SeedError as exc:
                    # An authority refusal has no created resource. An I/O
                    # failure, in contrast, keeps its identity for recovery.
                    shutil.rmtree(path)
                    shutil.rmtree(guest)
                    return InvalidSpec(str(exc))
            else:
                repo.mkdir(mode=0o700)
            _write(path / "clone.json", {"digest": digest, "repo": str(repo)})
            return identity

    def _clone(self, bundle: Path, repo: Path, commit: str) -> None:
        def git(*args):
            subprocess.run(git_command(*args), env=git_env(), capture_output=True, check=True)

        git("clone", "--no-checkout", "--no-hardlinks", "--no-local", str(bundle), str(repo))
        # The bundle's sole ref is a host pin, outside refs/heads. Clone's
        # default refspec does not select that namespace.
        git("-C", str(repo), "fetch", "--no-tags", "--no-write-fetch-head", str(bundle), commit)
        git("-C", str(repo), "remote", "remove", "origin")
        git("-C", str(repo), "checkout", "--detach", commit)

    async def checkout_path(self, identity: ResourceIdentity) -> Path | Absent | Unknown:
        """Host adapter: locate the checkout for capture or an alias swap."""
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            record = _read(path / "resource.json")
            return Path(record.get("repo_path", self._guest_resource(identity) / "repo"))

    async def attach_checkout(self, identity: ResourceIdentity, checkout: Path,
                              home: Path | None = None) -> ResourceIdentity | Absent | InvalidSpec | Unknown:
        """Host adapter after a durable swap; never called using guest input."""
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            state = self._state(path)
            if isinstance(state, Unknown):
                return state
            if isinstance(state, Running):
                return InvalidSpec("cannot attach a live checkout")
            associations = {"repo_path": checkout}
            if home is not None:
                associations["home_path"] = home
            resolved = {}
            for name, value in associations.items():
                if not isinstance(value, (str, Path)):
                    return InvalidSpec(f"{name}: unauthorised host path {value}")
                try:
                    target = Path(value).resolve()
                except (OSError, RuntimeError, ValueError):
                    return InvalidSpec(f"{name}: invalid host path {value}")
                if not self._host_path(str(value)) and target not in self._attachment_paths:
                    return InvalidSpec(f"{name}: unauthorised host path {value}")
                if not target.is_dir():
                    return InvalidSpec(f"{name}: directory is missing: {value}")
                resolved[name] = str(target)
            record = _read(path / "resource.json")
            record.update(resolved)
            _write(path / "resource.json", record)
            return identity

    async def prepare_input(self, identity: ResourceIdentity, source: BinaryIO) -> str | Absent | InvalidSpec | Unknown:
        """Host adapter: snapshot a bounded, already-framed prompt for stdin."""
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            directory = self.root / "inputs" / identity.id
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            target = directory / uuid.uuid4().hex
            try:
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return str(target)

    async def find(self, key: str):
        with self._locked() as control:
            for path in control.glob("*/resource.json"):
                if _read(path)["key"] == key:
                    return ResourceIdentity(path.parent.name)
            return Absent()

    def _invocation_state(self, path: Path, record: dict) -> State:
        process_key = (str(self.root), record["pid"])
        proc = self._processes.get(process_key)
        if proc is not None:
            if proc.poll() is not None:
                self._processes.pop(process_key, None)
        exit_path = path / record["directory"] / "exit.json"
        if exit_path.exists():
            return Exited(**_read(exit_path))
        if _alive(record):
            return Running()
        return Unknown("supervisor disappeared without exit evidence")

    def _state(self, path: Path) -> State:
        states = [self._invocation_state(path, record) for record in self._invocation_records(path)]
        for cls in (Running, Unknown):
            for state in states:
                if isinstance(state, cls):
                    return state
        if states:
            return max(states, key=lambda state: state.finished_at or 0)
        resource = _read(path / "resource.json")
        if resource.get("removed") or resource.get("stopped_at") is not None:
            return Exited(None, False, resource.get("stopped_at"))
        return Created()

    async def inspect(self, identity: ResourceIdentity) -> State:
        with self._locked() as control:
            path = self._resource(control, identity)
            return self._state(path) if path else Absent()

    def _invocation(self, path: Path, key: str) -> dict | None:
        record = path / ("inv-" + hashlib.sha256(key.encode()).hexdigest()) / "invocation.json"
        cached = self._records.get((str(self.root), path.name, record.parent.name))
        if cached is not None:
            return cached.copy()
        return self._record(path, record) if record.exists() else None

    def _mapped_path(self, value: str, mappings: dict[str, str]) -> str:
        for source in sorted(mappings, key=len, reverse=True):
            if value == source or value.startswith(source.rstrip("/") + "/"):
                return mappings[source] + value[len(source):]
        return value

    async def run(self, identity: ResourceIdentity, invocation: InvocationSpec, key: str):
        if not isinstance(key, str) or not key:
            return InvalidSpec("a non-empty invocation key is required")
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            refusal = self._valid_invocation(invocation)
            if refusal is not None:
                return refusal
            digest = spec_digest(invocation)
            previous = self._invocation(path, key)
            if previous is not None:
                return key if previous["digest"] == digest else IdentityConflict()
            resource = _read(path / "resource.json")
            spec = resource["spec"]
            state = self._state(path)
            if resource["removed"] or resource.get("stopped_at") is not None or (spec["lifetime"] == "attempt" and isinstance(state, Exited)):
                return state
            if resource.get("stopping"):
                return Unknown("resource termination is in progress")
            if spec["lifetime"] == "attempt" and invocation.kind == "agent":
                for record in self._invocation_records(path):
                    if record["kind"] == "agent":
                        return AlreadyInvoked()
            directory = "inv-" + hashlib.sha256(key.encode()).hexdigest()
            inv_path = path / directory
            guest = self._guest_resource(identity)
            inv_guest = guest / directory
            home = (guest / "home") if spec["lifetime"] == "conversation" else inv_guest / "home"
            mappings = {spec["guest_repo"]: resource.get("repo_path", str(guest / "repo")),
                        spec["guest_home"]: resource.get("home_path", str(home)),
                        spec["guest_temp"]: str(inv_guest / "tmp")}
            if spec.get("transport_handle"):
                mappings["/run/multiagents/rpc"] = spec["transport_handle"]["directory"]
            for value in spec["writable_paths"]:
                mappings[value] = value
            for index, value in enumerate(spec["readonly_paths"]):
                mappings[value] = str(inv_guest / "readonly" / str(index))
            cwd = self._mapped_path(invocation.cwd, mappings)
            if cwd == invocation.cwd and not any(Path(cwd).resolve().is_relative_to(Path(p).resolve()) for p in spec["writable_paths"]):
                return InvalidSpec("cwd is not an authorised guest path")
            inv_path.mkdir(mode=0o700)
            inv_guest.mkdir(mode=0o700)
            for name in ("home", "tmp"):
                (inv_guest / name).mkdir(mode=0o700)
            Path(mappings[spec["guest_home"]]).mkdir(parents=True, mode=0o700, exist_ok=True)
            (guest / "repo").mkdir(exist_ok=True)
            for value in spec["readonly_paths"]:
                target = Path(mappings[value])
                target.parent.mkdir(parents=True, exist_ok=True)
                if Path(value).is_dir():
                    shutil.copytree(value, target)
                    entries = list(target.rglob("*")) + [target]
                else:
                    shutil.copy2(value, target)
                    entries = [target]
                for entry in entries:
                    entry.chmod(entry.stat().st_mode & ~0o222)
            env = {name: self._mapped_path(value, mappings) for name, value in invocation.env.items()}
            env.update(HOME=mappings[spec["guest_home"]], TMPDIR=mappings[spec["guest_temp"]])
            # Injection controls are host-only, including when explicitly supplied.
            for name in ("MULTIAGENTS_TEST_ISOLATED_BACKEND", "IC_TEST_PLAN", "IC_BACKEND_ROOT"):
                env.pop(name, None)
            payload = {"argv": [self._mapped_path(arg, mappings) for arg in invocation.argv],
                       "cwd": cwd, "env": env,
                       "stdin": invocation.stdin, "stdout": str(inv_path / "stdout"),
                       "stderr": str(inv_path / "stderr")}
            if spec.get("transport_handle"):
                payload["transport_directory"] = spec["transport_handle"]["directory"]
            _write(inv_path / "payload.json", payload)
            proc = subprocess.Popen([sys.executable, "-c", _SUPERVISOR,
                                     str(inv_path / "payload.json"), str(inv_path / "exit.json")],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            record = {"key": key, "digest": digest, "directory": directory,
                      "kind": invocation.kind, "pid": proc.pid, "pid_start": _start_time(proc.pid)}
            _write(inv_path / "invocation.json", record)
            self._records[str(self.root), path.name, directory] = record.copy()
            self._processes[str(self.root), proc.pid] = proc
            # Do not expose a start as complete until the supervisor can
            # handle stop. SIGTERM before its handler is installed could
            # otherwise discard exit evidence without stopping the command.
            ready_until = time.monotonic() + 3
            while not (inv_path / "ready").exists() and proc.poll() is None:
                if time.monotonic() >= ready_until:
                    raise RuntimeError("reference supervisor did not become ready")
                time.sleep(0.002)
            return key

    async def inspect_invocation(self, identity: ResourceIdentity, invocation_id: str) -> State:
        with self._locked() as control:
            path = self._resource(control, identity)
            record = self._invocation(path, invocation_id) if path else None
            return self._invocation_state(path, record) if record else Absent()

    async def output(self, identity: ResourceIdentity, invocation_id: str, stream: str, offset: int):
        if stream not in ("stdout", "stderr") or not isinstance(offset, int) or offset < 0:
            return InvalidSpec("invalid output stream or offset")
        with self._locked() as control:
            path = self._resource(control, identity)
            record = self._invocation(path, invocation_id) if path else None
            if record is None:
                return Absent()
            try:
                with (path / record["directory"] / stream).open("rb") as output:
                    output.seek(offset)
                    return output.read()
            except FileNotFoundError:
                return b""

    def _signal(self, path: Path, record: dict, sig: int) -> bool:
        if _alive(record):
            try:
                # Fresh host processes recover records from disk. A matching
                # PID/start time alone does not establish that we launched it:
                # verify our exact supervisor and this invocation's host paths.
                expected = [os.fsencode(sys.executable), b"-c", _SUPERVISOR.encode(),
                            os.fsencode(path / record["directory"] / "payload.json"),
                            os.fsencode(path / record["directory"] / "exit.json")]
                command = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\0")[:-1]
                if command != expected or not _alive(record):
                    return False
                os.killpg(record["pid"], sig)
                return True
            except OSError:
                pass
        return False

    async def stop(self, identity: ResourceIdentity, grace: float) -> State:
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            records = self._invocation_records(path)
            resource = _read(path / "resource.json")
            if not records:
                resource.setdefault("stopped_at", time.time())
                _write(path / "resource.json", resource)
                return self._state(path)
            # Refuse a new turn while stop releases the lock to wait. Otherwise
            # a concurrent run could outlive the invocation snapshot below.
            resource["stopping"] = True
            _write(path / "resource.json", resource)
            for record in records:
                if isinstance(self._invocation_state(path, record), Running):
                    self._signal(path, record, signal.SIGTERM)
        deadline = time.monotonic() + max(0, grace)
        while any(_alive(record) for record in records) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        forced = set()
        for record in records:
            if _alive(record):
                if self._signal(path, record, signal.SIGKILL):
                    forced.add(record["key"])
        deadline = time.monotonic() + 2
        while any(_alive(record) for record in records) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            for record in records:
                if _alive(record):
                    return Unknown("supervisor termination could not be confirmed")
                exit_path = path / record["directory"] / "exit.json"
                if not exit_path.exists() and record["key"] in forced:
                    _write(exit_path, {"code": -signal.SIGKILL, "oom": False, "finished_at": time.time()})
            state = self._state(path)
            if isinstance(state, Exited):
                resource = _read(path / "resource.json")
                resource["stopping"] = False
                resource.setdefault("stopped_at", time.time())
                _write(path / "resource.json", resource)
            return state

    async def remove(self, identity: ResourceIdentity, keep_artifacts: bool) -> State:
        state = await self.stop(identity, 0.1)
        if isinstance(state, (Absent, Unknown)):
            return state
        with self._locked() as control:
            path = self._resource(control, identity)
            if path is None:
                return Absent()
            if keep_artifacts:
                record = _read(path / "resource.json")
                record["removed"] = True
                _write(path / "resource.json", record)
                return self._state(path)
            # Read-only copies belong to us and must be writable for cleanup.
            guest = self._guest_resource(identity)
            if guest.is_symlink():
                guest.unlink()
            elif guest.exists():
                for item in guest.rglob("*"):
                    if not item.is_symlink() and item.is_dir():
                        item.chmod(item.stat().st_mode | 0o700)
                shutil.rmtree(guest)
            shutil.rmtree(path)
            inputs = self.root / "inputs" / identity.id
            if inputs.exists():
                shutil.rmtree(inputs)
            for key in list(self._records):
                if key[:2] == (str(self.root), path.name):
                    self._records.pop(key)
            return Absent()

    async def list_owned(self, project: str) -> list[ResourceIdentity]:
        with self._locked() as control:
            return [ResourceIdentity(path.parent.name)
                    for path in sorted(control.glob("*/resource.json"))
                    if _read(path)["spec"]["labels"].get("multiagents.project") == project]


from .docker_lifecycle import DockerBackend  # noqa: E402,F401
