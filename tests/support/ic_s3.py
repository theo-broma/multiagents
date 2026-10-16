"""Shared helpers for the slice 3 contract tests (IC-C14..IC-C19),
`context/specs/phase7-part2-contract.md`, slice 3.

Names and outcomes follow K17 (slice 3 clarifications).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

SUPPORT = Path(__file__).resolve().parent
sys.path.insert(0, str(SUPPORT))

import fake_docker as fdk  # noqa: E402
import ic_support as ic  # noqa: E402
import ic_workload as wl  # noqa: E402

HEX64 = re.compile(r"^[0-9a-f]{64}$")
PROJECT = "ic-test"
DOCKER_RESOURCES_ENV = "IC_DOCKER_RESOURCES"
NEEDS_KEY = wl.NEEDS_KEY


def declare(entry: dict, **needs: bool) -> dict:
    """K17.2: set a provider entry's declaration (all False by default)."""
    entry[NEEDS_KEY] = {"credentials": False, "network": False, "rpc": False, **needs}
    return entry


def resources(**values: Any) -> dict:
    """K17.1: the `executor.isolated.resources` mapping."""
    return dict(values)


def docker_backend(root: Path, seeds=None, **settings):
    """K17.1."""
    cls = getattr(ic.lifecycle(), "DockerBackend", None)
    assert callable(cls), "K17.1: public DockerBackend has not been implemented"
    return cls(Path(root), seeds=seeds, **settings)


# ------------------------------------------------------------ direct backend

class DockerWorld:
    """A Docker backend over the fake CLI (found on PATH), a host root, and
    the helpers the IC-C14 tests share."""

    def __init__(self, tmp_path: Path, monkeypatch, **fake_kw):
        self.tmp = tmp_path
        self.fd = fdk.FakeDocker(tmp_path, **fake_kw).install(monkeypatch)
        self.root = tmp_path / "backend-root"
        self.root.mkdir()
        self.backend = docker_backend(self.root)

    def spec(self, owner: str = "att_conf1", **over):
        return ic.launch_spec(self.root, owner=owner, **over)

    def provision(self, key: str = "key-1", **over):
        spec = self.spec(**over)
        return spec, ic.run(self.backend.provision(spec, key))

    def identity(self, key: str = "key-1", **over):
        spec, got = self.provision(key, **over)
        assert ic.kind_of(got) not in ("invalid_spec", "unsupported", "identity_conflict", "unknown"), got
        return spec, ic.identity_of(got)

    def container(self, identity) -> dict:
        found = self.fd.container(identity.id)
        assert found is not None, f"no container {identity.id} in the fake daemon"
        return found

    def invoke(self, identity, token: str = "tok", key: str = "inv-1", argv=None, env=None, stdin=None):
        spec = ic.invocation(argv or ["/bin/echo", token], env=env or {"IC_T": token}, stdin=stdin)
        return spec, ic.run(self.backend.run(identity, spec, key))

    def snapshot_dirs(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_dir())

    def warm(self) -> None:
        """Let the backend create its lazily made control state before a test
        compares the host tree."""
        ic.run(self.backend.probe())
        ic.run(self.backend.find("warm-up"))


# ---------------------------------------------------------------- scheduler

class ShortRPC:
    """Keep missing-feature failures bounded instead of the legacy 40s RPC default."""
    def rpc(self, *args, **kwargs):
        kwargs.setdefault("timeout", ic.WAIT)
        return super().rpc(*args, **kwargs)


def events_stdout(text: str = "done", sid: str = "ses_ic_fake") -> bytes:
    """What the fake opencode-shaped provider prints for a short successful run."""
    def ev(kind, part):
        return (json.dumps({"type": kind, "sessionID": sid, "part": dict(part, sessionID=sid)}) + "\n").encode()
    return (ev("step_start", {"id": "prt_s0", "type": "step-start"})
            + ev("step_finish", {"id": "prt_f0", "type": "step-finish", "reason": "tool-calls", "cost": 0,
                                 "tokens": {"input": 1, "output": 1, "reasoning": 0,
                                            "cache": {"read": 0, "write": 0}}})
            + ev("text", {"id": "prt_t0", "type": "text", "text": text}))


class FakeDockerSched(ShortRPC, ic.IcSched):
    """The scheduler on the REAL isolated backend (nothing injected) whose
    `docker` is the fake CLI. The provider declares that it needs nothing."""

    def __init__(self, tmp_path, monkeypatch, *, scheduler: dict | None = None, **fake_kw):
        assert callable(getattr(ic.lifecycle(), "DockerBackend", None)), "K17.1 DockerBackend is missing"
        super().__init__(tmp_path, monkeypatch, inject=False, kind="isolated", scheduler=scheduler)
        self.fd = fdk.FakeDocker(tmp_path, **fake_kw).install(monkeypatch)
        declare(self.fake.entry)
        self.p.write()
        self._reset_server()
        self.fd.workload(stdout=events_stdout())

    def provider_declares(self, **needs: bool) -> None:
        declare(self.fake.entry, **needs)
        self.p.write()
        self._reset_server()

    def provider_declares_nothing(self) -> None:
        self.fake.entry.pop(NEEDS_KEY, None)
        self.p.write()
        self._reset_server()

    def run_dir(self, run_id: str) -> Path:
        from multiagents.paths import ProjectPaths
        return ProjectPaths(self.root).run_dir(run_id)

    def host_output(self, run_id: str) -> tuple[bytes, bytes]:
        """Host output: (stdout, stderr) as the host recorded them for the run."""
        d = self.run_dir(run_id)
        read = lambda n: (d / n).read_bytes() if (d / n).is_file() else b""  # noqa: E731
        return read("output.ndjson"), read("stderr.log")

    def tf_causes(self, node_id: str) -> list[dict]:
        return ic.causes(self.get(node_id))

    def status(self) -> dict:
        reply = self.rpc("scheduler_status")
        assert reply.get("ok") is True, reply
        return reply["result"]


def wait_done(s: ic.IcSched, node: str, timeout: float = ic.WAIT) -> dict:
    return s.wait(lambda: (v := s.get(node)) and v["state"] in ("done", "failed", "held", "cancelled") and v,
                  timeout, f"{node} to end")


def evidence_of(s: ic.IcSched, node: str) -> dict:
    """`launch_evidence.executor` of the node's newest attempt (K5)."""
    run = s.runs(node)[-1]
    return s.evidence(run["attempt_id"]).get("executor", {})


# ------------------------------------------------------- injected-backend world

class S3Sched(ShortRPC, ic.IcSched):
    """The scheduler with the crash-injecting double (`ic_s3_backends`) around
    the reference backend, or (backend="docker", SG_TEST_DOCKER=1) around the
    real Docker backend. The provider is the scriptable workload
    (`ic_workload`); it declares that it needs nothing (D-S3.2)."""

    def __init__(self, tmp_path, monkeypatch, *, backend: str = "reference", scheduler: dict | None = None,
                 owner: str | None = None):
        required = "DockerBackend" if backend == "docker" else "ReferenceBackend"
        cls = getattr(ic.lifecycle(), required, None)
        assert callable(cls) and callable(getattr(cls, "attach_checkout", None)), "K17.3 provisioning seam is missing"
        self._monkeypatch = monkeypatch
        self.backend_name = backend
        super().__init__(tmp_path, monkeypatch, inject=True, kind="isolated", scheduler=scheduler)
        monkeypatch.setenv(ic.BACKEND_ENV, "ic_s3_backends:make_docker" if backend == "docker"
                           else "ic_s3_backends:make")
        if backend == "docker":
            import ic_docker as dk
            shim = dk.write_label_shim(tmp_path / "ic-label-shim", owner)
            monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ.get('PATH', '')}")
            dk.reap_once()
            dk.register_session_finalizer(owner)
            self.owner = owner
        work = wl.WorkloadProvider(tmp_path, "acme")
        if backend == "docker":
            wl.guest_runnable(work.entry)           # the host path of its script is not in the guest
        self.p.fakes["acme"] = work
        self.p.providers["acme"] = work.entry
        self.fake = work
        self.p.write()
        self._reset_server()
        self.plan({"calls": True})

    def executor(self, kind: str, **extra) -> None:
        """As `IcSched.executor`; the configured `isolated.resources` also reach
        the injected Docker backend (its factory takes only a root and seeds)."""
        resources = (extra.get("isolated") or {}).get("resources")
        if getattr(self, "backend_name", None) == "docker" and resources:
            self._monkeypatch.setenv(DOCKER_RESOURCES_ENV, json.dumps(resources))
        super().executor(kind, **extra)

    # -- nodes ------------------------------------------------------------
    def node(self, *ops: dict, **fields) -> str:
        return self.create(task=wl.task(*ops), **fields)["id"]

    # -- the backend and the store, as the host sees them ----------------------
    def be(self):
        """A backend over the same root, as a restarted host process would build it."""
        if self.backend_name == "docker":
            return docker_backend(self.backend_root)
        return importlib.import_module("ic_backends").make_reference(self.backend_root)

    def host_output(self, run_id: str) -> tuple[bytes, bytes]:
        from multiagents.paths import ProjectPaths
        d = ProjectPaths(self.root).run_dir(run_id)
        read = lambda n: (d / n).read_bytes() if (d / n).is_file() else b""  # noqa: E731
        return read("output.ndjson"), read("stderr.log")

    def crash_at(self, point: str) -> int:
        """Arm the K3 injector to kill this scheduler at a public backend boundary."""
        pid = self.pid()
        self.plan({"calls": True, "crash": point, "kill_pid": pid})
        return pid

    def run_ids(self, node: str) -> list[str]:
        return [r["run_id"] for r in self.runs(node) if r.get("run_id")]
