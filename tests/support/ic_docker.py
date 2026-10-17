"""World for the IC-C6 isolation suite: a scheduler with `executor.kind:
isolated` on the REAL backend (nothing injected), the probe provider
(`ic_probe.ProbeProvider`) and the helpers that read what the probes report.

Opt-in with SG_TEST_DOCKER=1. With the switch set, a missing prerequisite (no
daemon, no image) is a FAILURE (`require_docker`), never a skip.

Cleanup: the existing session guard (`st_container_guard`) labels what it sees
and reaps earlier sessions' leftovers at session start; this world adds a
finalizer that removes every container carrying this session's owner label and
every container that mounts or names this test's directory, even on failure.
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ic_workload as wl  # noqa: E402
import ic_probe  # noqa: E402
from st_container_guard import process_start  # noqa: E402
import ic_support as ic  # noqa: E402
from st_docker import WAIT_SECONDS  # noqa: E402

OPT_IN = os.environ.get("SG_TEST_DOCKER") == "1"
OWNER_LABEL = "multiagents.test.owner"

START_WAIT = 120     # a container's first report (create, seed, start, python startup)
PROBE_WAIT = 120     # one probe attempt to finish
HOLD = 150           # how long a victim keeps itself alive; always cancelled earlier
DOCKER_CMD = 20


# --------------------------------------------------------------- K12: labels
#
# The existing guard shim (`st_container_guard`, not ours) labels only
# `docker run`. This module adds a second shim, ours, in front of it: every
# resource-creating command carries the test-session owner label, `create`,
# `network create` and `volume create` included, then the call goes on to the
# next `docker` on PATH (the guard, which journals and execs the real one).

LABEL_SHIM = r'''#!{python}
import os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
mine = Path(__file__).parent.resolve()
path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                       if Path(p or ".").resolve() != mine)
target = shutil.which("docker", path=path)
os.environ["PATH"] = path  # the next shim must not find this one again
if not target:
    print("docker is not installed", file=sys.stderr); sys.exit(127)
label = {label!r}
head = args[:2] if args[:1] in (["network"], ["volume"], ["container"]) else args[:1]
if head in (["run"], ["create"], ["container", "run"], ["container", "create"],
            ["network", "create"], ["volume", "create"]) and label not in args:
    args = args[:len(head)] + ["--label", label] + args[len(head):]
os.execv(target, [target, *args])
'''


def write_label_shim(directory: Path, owner: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "docker"
    shim.write_text(LABEL_SHIM.format(python=sys.executable, label=f"{OWNER_LABEL}={owner}"))
    shim.chmod(0o755)
    return directory


def test_owner(request) -> str:
    """The owner label of ONE test: the session's label plus a token of the
    test id, so a fixture's sweep (`remove_session_resources`) can only remove
    what this test created, never another xdist worker's containers."""
    token = hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:10]
    return f"{request.config.st_docker_owner}/{token}"


def test_token(owner: str) -> str:
    return owner.rpartition("/")[2]


class KeyedBackend:
    """A real Docker backend whose creation keys and project
    label values are private to one test. The daemon is shared by every xdist
    worker and `find(key)` is daemon-wide, so tests that reuse a literal key
    ("key-1") or project ("ic-test") would otherwise find each other's
    containers. Everything else is delegated untouched; the same token on a
    second backend over the same root keeps same-root recovery working."""

    def __init__(self, inner, token: str):
        self._inner, self._token = inner, token

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _key(self, key):
        return f"{key}-{self._token}" if isinstance(key, str) and key else key

    def _project(self, value):
        return f"{value}-{self._token}" if isinstance(value, str) and value else value

    async def provision(self, spec, key):
        labels = getattr(spec, "labels", None)
        if isinstance(labels, dict) and ic.PROJECT_LABEL in labels:
            spec = dataclasses.replace(spec, labels={**labels, ic.PROJECT_LABEL: self._project(labels[ic.PROJECT_LABEL])})
        return await self._inner.provision(spec, self._key(key))

    async def find(self, key):
        return await self._inner.find(self._key(key))

    async def list_owned(self, project):
        return await self._inner.list_owned(self._project(project))


def keyed(backend, owner: str):
    return KeyedBackend(backend, test_token(owner))


def owner_is_dead(label: str) -> bool:
    """True only when the owning session is verifiably gone (same host, pid
    absent or reused). Anything unparseable or foreign is kept."""
    try:
        host, pid_text, started = label.rsplit(":", 2)
        started, slash, token = started.partition("/")       # per-test suffix: <start>/<token>
        if slash and not token.isalnum():
            return False
        pid = int(pid_text)
        if host != socket.gethostname() or pid <= 0 or not started.isdecimal():
            return False
        try:
            return process_start(pid) != started
        except FileNotFoundError:
            return True
    except (ValueError, OSError):
        return False


def _sweep(binary: str | None, wanted) -> list[str]:
    """Remove containers, networks and volumes whose owner label satisfies
    `wanted(label)`. Returns what it removed; removes nothing without an
    answering daemon."""
    binary = binary or shutil.which("docker")
    removed: list[str] = []
    if not binary:
        return removed
    for kind in ("container", "network", "volume"):
        listing = [binary, kind, "ls", "-q", "--filter", f"label={OWNER_LABEL}"] if kind != "container" \
            else [binary, "ps", "-aq", "--filter", f"label={OWNER_LABEL}"]
        try:
            ids = subprocess.run(listing, capture_output=True, text=True, timeout=DOCKER_CMD).stdout.split()
            if not ids:
                continue
            info = subprocess.run([binary, kind, "inspect", *ids], capture_output=True, text=True,
                                  timeout=DOCKER_CMD)
            if info.returncode:
                continue
            for item in json.loads(info.stdout):
                labels = (item.get("Config", {}).get("Labels") if kind == "container"
                          else item.get("Labels")) or {}
                label = labels.get(OWNER_LABEL)
                ident = item.get("Id") or item.get("Name")
                if isinstance(label, str) and ident and wanted(label):
                    rm = [binary, kind, "rm", *(["-f"] if kind != "volume" else []), ident]
                    if subprocess.run(rm, capture_output=True, text=True, timeout=DOCKER_CMD).returncode == 0:
                        removed.append(f"{kind}:{ident}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            continue                                    # no authority to remove anything
    return removed


def reap_dead_sessions(binary: str | None = None) -> list[str]:
    """Remove resources labelled by DEAD test sessions and nothing else."""
    return _sweep(binary, owner_is_dead)


def remove_session_resources(owner: str, binary: str | None = None) -> list[str]:
    """Teardown: remove every resource carrying exactly this session's label.
    Other sessions' resources, live or dead, are never touched."""
    return _sweep(binary, lambda label: label == owner)


_registered: set[str] = set()


def register_session_finalizer(owner: str) -> None:
    """Also remove this session's resources at interpreter exit (interrupted
    runs), once per owner."""
    if owner not in _registered:
        _registered.add(owner)
        atexit.register(remove_session_resources, owner)


_reaped = False


def reap_once() -> None:
    global _reaped
    if not _reaped:
        _reaped = True
        reap_dead_sessions()


def docker(*args: str, timeout: float = DOCKER_CMD) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def default_image() -> str:
    from multiagents.paths import shipped_defaults_dir
    shipped = yaml.safe_load((shipped_defaults_dir() / "project.yaml").read_text())
    return ((shipped.get("executor") or {}).get("docker") or {}).get("image") \
        or "multiagents/workspace:latest"


def require_docker() -> None:
    """Under the opt-in switch a missing prerequisite is a failure."""
    info = docker("info", "--format", "{{.ServerVersion}}")
    assert info.returncode == 0, f"SG_TEST_DOCKER=1 but no Docker daemon: {info.stderr.strip()}"
    image = default_image()
    assert docker("image", "inspect", image).returncode == 0, \
        f"SG_TEST_DOCKER=1 but the workspace image is missing: {image}"


def owned(owner: str) -> list[str]:
    out = docker("ps", "-aq", "--filter", f"label={OWNER_LABEL}={owner}")
    return out.stdout.split() if out.returncode == 0 else []


def referencing(needle: str) -> list[str]:
    ids = docker("ps", "-aq").stdout.split()
    found = []
    for cid in ids:
        info = docker("inspect", cid)
        if info.returncode == 0 and needle in info.stdout:
            found.append(cid)
    return found


class IsolationWorld(ic.IcSched):
    """Scheduler + probe provider on the real isolated backend."""

    def __init__(self, tmp_path, monkeypatch, owner: str):
        super().__init__(tmp_path, monkeypatch, inject=False, kind="isolated")
        self.owner = owner
        shim = write_label_shim(tmp_path / "ic-label-shim", owner)
        monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
        reap_once()
        register_session_finalizer(owner)
        self.tmp_marker = str(tmp_path)
        probe = ic_probe.ProbeProvider(tmp_path, "acme")
        wl.guest_runnable(probe.entry)              # the host path of its script is not in the guest
        self.p.fakes["acme"] = probe
        self.p.providers["acme"] = probe.entry
        self.p.add_agent("reader", "acme", "acme/m1", permission="readonly", writes=False)
        self.p.write()
        self._reset_server()
        self.nonces: dict[str, str] = {}

    def nonce(self, tag: str) -> str:
        return self.nonces.setdefault(tag, f"{tag}{uuid.uuid4().hex}")

    def host_output(self, run_id: str) -> tuple[bytes, bytes]:
        """Host output: (stdout, stderr) as the host recorded them for the run."""
        from multiagents.paths import ProjectPaths
        d = ProjectPaths(self.root).run_dir(run_id)
        read = lambda n: (d / n).read_bytes() if (d / n).is_file() else b""  # noqa: E731
        return read("output.ndjson"), read("stderr.log")

    # -- nodes ------------------------------------------------------------
    def probe_node(self, probe: str, agent: str = "worker", **fields) -> str:
        extra = {k: fields.pop(k) for k in ("depends_on", "inputs") if k in fields}
        return self.create(agent=agent, task=ic_probe.instruction(probe, **fields), **extra)["id"]

    # -- reading reports --------------------------------------------------
    def texts(self, node_id: str) -> str:
        from multiagents import server
        chunks: list[str] = []
        for run in self.runs(node_id):
            run_id = run.get("run_id")
            if not run_id:
                continue
            try:
                result = server.collect_agent(run_id, "full")
                chunks.append(json.dumps(result, default=str))
                log_dir = result.get("log_dir") if isinstance(result, dict) else None
            except Exception as exc:                      # noqa: BLE001
                chunks.append(repr(exc))
                log_dir = None
            from multiagents.paths import ProjectPaths
            for base in filter(None, [log_dir, str(ProjectPaths(self.root).run_dir(run_id))]):
                for f in sorted(Path(base).rglob("*")) if Path(base).is_dir() else []:
                    if f.is_file() and f.stat().st_size < 4_000_000:
                        try:
                            chunks.append(f.read_text(errors="ignore"))
                        except OSError:
                            pass
        return "\n".join(chunks)

    def reports(self, node_id: str) -> list[dict]:
        seen, out = set(), []
        for rep in ic_probe.parse_reports(self.texts(node_id)):
            key = json.dumps(rep, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(rep)
        return out

    def report(self, node_id: str, phase: str | None = None, timeout: float = START_WAIT) -> dict:
        def find():
            for rep in self.reports(node_id):
                if phase is None or rep.get("phase") == phase:
                    return rep
        return self.wait(find, timeout, f"the {phase or 'first'} report of {node_id}")

    def final_report(self, node_id: str, timeout: float = PROBE_WAIT) -> dict:
        self.wait(lambda: self.get(node_id)["state"] in ("done", "held", "failed", "cancelled"),
                  timeout, f"{node_id} to finish")
        state = self.get(node_id)["state"]
        assert state == "done", f"probe node {node_id} ended {state}: {ic.text_of(self.get(node_id))[:600]}"
        return self.report(node_id, timeout=SHORT_AFTER_DONE)

    def last_hold(self, node_id: str, after: int = 0, timeout: float = 30) -> dict:
        """The newest `hold` report, once at least one arrived beyond `after`."""
        def newest():
            holds = [r for r in self.reports(node_id) if r.get("phase") == "hold"]
            return holds[-1] if len(holds) > after else None
        return self.wait(newest, timeout, f"a hold report of {node_id}")

    def hold_count(self, node_id: str) -> int:
        return sum(1 for r in self.reports(node_id) if r.get("phase") == "hold")

    def stop_victim(self, node_id: str) -> None:
        reply = self.cancel_raw(node_id)
        assert reply.get("ok") is True, reply


SHORT_AFTER_DONE = 20


def cleanup(world: IsolationWorld) -> None:
    """Finalizer: the scheduler (by Sched.close), then every container this
    test or this session owns."""
    world.close()
    remove_session_resources(world.owner)
    ids = set(referencing(world.tmp_marker))
    for cid in ids:
        docker("rm", "-f", cid)
