"""Shared helpers for the phase 7 part 2 contract tests (IC-C*),
`context/specs/phase7-part2-contract.md`, slice 1.

Nothing here reaches into a scheduler internal. Where the contract names a
field or an outcome but not its Python spelling, the helper is *tolerant*
(attribute, mapping key or class name) and says so; the few guesses that
cannot be made tolerant live in ONE place each, listed here, so the
implementer can see exactly which spelling a failing test wants:

  G1  `lifecycle.ReferenceBackend(root: Path)`, with `.version` (str); built by
      `ic_backends.make_reference` only.
  G2  `lifecycle.LaunchSpec(spec_version=1, owner=, lifetime=, labels=,
      writable_paths=, readonly_paths=, guest_repo=, guest_home=, guest_temp=,
      seed=, session_state=, resources=, credential_handle=,
      transport_handle=)`; built by `launch_spec` only. Its digest is read by
      `digest_of` (attribute, method, or `lifecycle.spec_digest(spec)`).
  G3  `lifecycle.InvocationSpec(argv=, env=, cwd=, stdin=, kind=)`; built by
      `invocation` only.
  G4  (settled by K3) env var `BACKEND_ENV` = "module:callable", the callable
      takes the project's private backend root (Path) and returns a backend.
  G6  (open, NEED_INFO) the reserved project label's key (`PROJECT_LABEL`) and
      what `list_owned(project)` takes as `project` (assumed: the project slug,
      `IcSched.slug`; the label is supplied in `LaunchSpec.labels`).
  G5  typed outcomes: `kind_of(o)` reads `.state` / `.kind` / a mapping key /
      the lower-cased class name, so `Absent`, `absent`, "absent" all match.
"""
from __future__ import annotations

import asyncio
import hashlib
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

import nc_harness as nch  # noqa: E402

BACKEND_ENV = "MULTIAGENTS_TEST_ISOLATED_BACKEND"       # G4
PLAN_ENV = "IC_TEST_PLAN"                               # read by ic_backends only
BACKEND_SPEC = "ic_backends:make"
INJECTED = (BACKEND_ENV, PLAN_ENV, "IC_BACKEND_ROOT")    # K3: never in a run's environment
PROJECT_LABEL = "multiagents.project"                   # G6

# Short explicit bounds on every wait: a red test reaches its assertion in seconds.
WAIT = 6             # a scheduler-visible condition
SHORT = 3            # something that should already be true
QUIET = 1.5          # "nothing more happens" window
OP = 8               # one backend operation


def lifecycle():
    return importlib.import_module("multiagents.executor.lifecycle")


def run(coro, timeout: float = OP):
    async def bounded():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(bounded())


# ------------------------------------------------------------ typed outcomes

def kind_of(outcome: Any) -> str:
    """G5: the outcome's name, whichever way it is spelled."""
    if isinstance(outcome, str):
        return outcome.split(":", 1)[0].lower()
    if isinstance(outcome, dict):
        for key in ("state", "kind", "outcome", "status"):
            if isinstance(outcome.get(key), str):
                return outcome[key].split(":", 1)[0].lower()
    for key in ("state", "kind", "outcome", "status"):
        value = getattr(outcome, key, None)
        if isinstance(value, str):
            return value.split(":", 1)[0].lower()
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(outcome).__name__).lower()


def field(outcome: Any, name: str, default: Any = None) -> Any:
    if isinstance(outcome, dict):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


def identity_of(outcome: Any) -> Any:
    return field(outcome, "identity", outcome) if not isinstance(outcome, (str, bytes)) else outcome


def data_of(chunk: Any) -> bytes:
    """The bytes of an `output` reply: bytes, or an object/mapping carrying them."""
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk)
    for key in ("data", "bytes", "content"):
        value = field(chunk, key)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode()
    raise AssertionError(f"cannot read bytes from an output reply: {chunk!r}")


def canonical_digest(spec_dict: dict) -> str:
    text = json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(spec: Any) -> str:
    value = getattr(spec, "digest", None)
    if callable(value):
        value = value()
    if value is None:
        value = lifecycle().spec_digest(spec)
    return value


# ----------------------------------------------------------------- the specs

GUEST_REPO, GUEST_HOME, GUEST_TEMP = "/work/repo", "/work/home", "/work/tmp"


def launch_spec(root: Path, owner: str = "att_conf1", lifetime: str = "attempt", **over):
    """G2. Host paths in a spec lie under `root` (the host-issued root)."""
    lc = lifecycle()
    fields: dict[str, Any] = dict(
        spec_version=1, owner=owner, lifetime=lifetime, labels={"ic.test": "1", PROJECT_LABEL: "ic-test"},
        writable_paths=[], readonly_paths=[],
        guest_repo=GUEST_REPO, guest_home=GUEST_HOME, guest_temp=GUEST_TEMP)
    fields.update(over)
    return lc.LaunchSpec(**fields)


def invocation(argv: list[str], *, kind: str = "agent", env: dict | None = None,
               cwd: str = GUEST_REPO, stdin: Any = None):
    """G3."""
    return lifecycle().InvocationSpec(argv=list(argv), env=dict(env or {}), cwd=cwd,
                                      stdin=stdin, kind=kind)


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ------------------------------------------------------------- the scheduler

class IcSched(nch.Sched):
    """`nc_harness.Sched` for isolated projects, with the injected backend (G4)
    and a plan file the test rewrites while the scheduler runs."""

    def __init__(self, tmp_path, monkeypatch, *, inject: bool = True,
                 scheduler: dict | None = None, kind: str | None = "isolated"):
        super().__init__(tmp_path, monkeypatch, enabled=True, scheduler={
            "tick_seconds": 0.1, "admission_timeout_seconds": 5,
            "retries": {"backoff_seconds": [0.1, 0.1, 0.1]}, **(scheduler or {})})
        self.fake = self.p.fakes["acme"]
        self.plan_file = tmp_path / "ic-plan.json"
        self.plan({})
        self.backend_root = tmp_path / "ic-backend"
        if inject:
            monkeypatch.setenv(BACKEND_ENV, BACKEND_SPEC)
            monkeypatch.setenv(PLAN_ENV, str(self.plan_file))
            monkeypatch.setenv("IC_BACKEND_ROOT", str(self.backend_root))
            monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
                [str(SUPPORT), str(Path(__file__).resolve().parents[2] / "src"),
                 os.environ.get("PYTHONPATH", "")]))
        if kind:
            self.executor(kind)

    # -- configuration ----------------------------------------------------
    def plan(self, plan: dict) -> None:
        """What the injected double does next: see `ic_backends`."""
        self.plan_file.write_text(json.dumps(plan))

    def executor(self, kind: str, **extra: Any) -> None:
        self.p.project["executor"] = {"kind": kind, **extra}
        self.p.write()
        self._reset_server()

    def pin_agent(self, agent: str, executor: str) -> None:
        self.p.agents[agent]["executor"] = executor
        self.p.write()
        self._reset_server()

    # -- observation ------------------------------------------------------
    def wait(self, predicate, timeout: float = WAIT, what: str = "condition"):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = predicate()
            if last:
                return last
            time.sleep(0.1)
        raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")

    def runs(self, node_id: str) -> list[dict]:
        return [r for r in self.get(node_id).get("runs") or [] if isinstance(r, dict)]

    def wait_runs(self, node_id: str, n: int, timeout: float = WAIT) -> list[dict]:
        return self.wait(lambda: len(self.runs(node_id)) >= n and self.runs(node_id),
                         timeout, f"{n} run(s) of {node_id}")

    def blocked(self, node_id: str) -> list:
        return self.get(node_id).get("blocked") or []

    def record_env(self) -> Path:
        """Wrap the fake provider's CLI so every spawn appends the NAMES of the
        environment it received to the returned file (one JSON list per line)."""
        log = self.tmp / "spawn-env.jsonl"
        wrapper = self.tmp / "envdump-cli.py"
        wrapper.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"open({str(log)!r}, 'a').write(json.dumps(sorted(os.environ)) + '\\n')\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {self.fake.entry['bin']!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        self.fake.entry["bin"] = str(wrapper)
        self.p.write()
        self._reset_server()
        log.touch()
        return log

    def spawn_envs(self, log: Path) -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    def owned(self, root: Path | None = None) -> list:
        """Resources the injected reference backend holds for this project (G6)."""
        be = importlib.import_module("ic_backends").make_reference(root or self.backend_root)
        return [identity_of(x) for x in asyncio.run(be.list_owned(self.slug))]

    def evidence(self, attempt_id: str) -> dict:
        """`launch_evidence` of an attempt, read from the scheduler's own
        store: the contract names this field (IC-C2) and exposes no tool for
        it except the `executor` projection in `get_node().runs[]`."""
        from multiagents.scheduler.engine import attempts
        from multiagents.scheduler.store import Store
        store = Store(self.root)
        with store.transaction() as db:
            return json.loads(json.dumps(attempts(db).get(attempt_id, {}).get("launch_evidence") or {}))


def objects(value: Any):
    """Every mapping nested in a public JSON view (TF tests find causes this way)."""
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def causes(view: Any) -> list[dict]:
    return [o for o in objects(view) if isinstance(o.get("class"), str)]


def text_of(value: Any) -> str:
    return json.dumps(value, default=str)
