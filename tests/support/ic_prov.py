"""Support for the phase 7 part 2 slice 2 tests (IC-C9..C13),
`context/specs/phase7-part2-contract.md`, "Slice 2".

The public slice 2 seams use Q1..Q3 exactly: ProjectPaths, seeds=, and
SeedError(code, reason). No compatibility fallback changes their meaning.

* `IcWorld`: `nc_fixture.m4_world.M4World` (a real scheduler process, fixture
  providers, loop templates with session aliases) with the injected reference
  backend (K3) and a provider that can run shell/python steps in its working
  directory and report the repository it finds there (`ic_report.py`).

Provider directives added to the M4 fixture agent (all optional):

    report      bool        write a repository report on start
                            (`IcProvider.reports(tag)`)
    early       str|[str]   python source run at the very start, BEFORE any
                            `gate` (same globals as `exec`)
    report_end  bool        and one at the end, after every step
    exec        str|[str]   python source run in the working directory after
                            `write`/`delete`, BEFORE `commit` (globals: os,
                            sys, subprocess, json, time, base (= FX_DIR), fx,
                            sh(cmd))
    after       str|[str]   same, AFTER `commit` and `leave`

Nothing here imports a scheduler or backend private name.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SUPPORT = Path(__file__).resolve().parent
TESTS = SUPPORT.parent
for _p in (str(SUPPORT), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ic_support as ic  # noqa: E402
import multiagents as _ma  # noqa: E402

SRC = Path(_ma.__file__).resolve().parents[1]      # wherever the package under test is imported from
from ic_support import kind_of  # noqa: E402,F401

REPORT_FILE = SUPPORT / "ic_report.py"
BACKEND_SPEC = "ic_prov_backend:make"

# Load-tolerant ceilings; polling returns as soon as the condition is visible.
WAIT = 90            # one activation / one transition through a real scheduler
ROUNDS = 180          # several activations in a row
SPAWN = 90            # a node's provider process to start (a missing feature stalls here)
OP = ic.OP

GIT_ENV = dict(GIT_AUTHOR_NAME="ic", GIT_AUTHOR_EMAIL="ic@example.invalid",
               GIT_COMMITTER_NAME="ic", GIT_COMMITTER_EMAIL="ic@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


# ------------------------------------------------------------------ plain git

def git(repo: Path, *args: str, check: bool = True, env: dict | None = None,
        input: bytes | str | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, input=input,
                          text=isinstance(input, str) or input is None,
                          env={**os.environ, **GIT_ENV, **(env or {})})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc.stdout.strip() if isinstance(proc.stdout, str) else proc.stdout


def git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          env={**os.environ, **GIT_ENV}).returncode == 0


class HostRepo:
    """A real repository the tests commit to."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                       env={**os.environ, **GIT_ENV})

    def write(self, rel: str, text: str | bytes) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode())

    def commit(self, message: str, files: dict[str, str | bytes] | None = None) -> str:
        for rel, text in (files or {}).items():
            self.write(rel, text)
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD")

    def branch(self, name: str, start: str | None = None) -> None:
        git(self.path, "checkout", "-q", "-B", name, *([start] if start else []))

    def checkout(self, name: str) -> None:
        git(self.path, "checkout", "-q", name)

    def reachable(self, commit: str) -> set[str]:
        out = git(self.path, "rev-list", "--objects", commit)
        return {line.split()[0] for line in out.splitlines() if line.strip()}

    def all_objects(self) -> set[str]:
        out = git(self.path, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
        return {line for line in out.splitlines() if line}

    def has(self, oid: str) -> bool:
        return git_ok(self.path, "cat-file", "-e", oid)


def standard_world(repo: HostRepo) -> dict[str, Any]:
    """main: base -> work; sibling: base -> S (unmerged); orphan history U.

    Returns the ids the tests assert on."""
    base = repo.commit("base", {"a.txt": "alpha\n", "dir/b.txt": "beta\n"})
    work = repo.commit("work", {"c.txt": "gamma\n", "dir/b.txt": "beta2\n"})
    repo.branch("nodes/sibling", base)
    sibling = repo.commit("sibling unmerged", {"secret.txt": "sibling-secret-blob\n"})
    secret_blob = git(repo.path, "rev-parse", sibling + ":secret.txt")
    git(repo.path, "checkout", "-q", "--orphan", "unrelated")
    git(repo.path, "rm", "-rqf", ".")
    unrelated = repo.commit("unrelated history", {"u.txt": "unrelated-blob\n"})
    repo.checkout("main")
    git(repo.path, "branch", "-D", "unrelated")
    git(repo.path, "branch", "keep-unrelated", unrelated)
    return {"base": base, "work": work, "sibling": sibling, "secret_blob": secret_blob,
            "unrelated": unrelated, "input": work}


# -------------------------------------------------------------------- bundles

def bundle_header(path: Path) -> list[str]:
    data = Path(path).read_bytes()[:65536]
    head = data.split(b"\n\n", 1)[0]
    return head.decode("utf-8", "replace").splitlines()


def bundle_heads(path: Path) -> list[tuple[str, str]]:
    out = subprocess.run(["git", "bundle", "list-heads", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return [tuple(line.split(" ", 1)) for line in out.stdout.splitlines() if line.strip()]


def bundle_verifies(path: Path, scratch: Path) -> bool:
    """Verify in an EMPTY repository: a bundle that needs prerequisites fails."""
    empty = Path(scratch) / "verify-empty"
    if not empty.exists():
        subprocess.run(["git", "init", "-q", str(empty)], check=True, env={**os.environ, **GIT_ENV})
    return subprocess.run(["git", "bundle", "verify", str(path)], cwd=empty,
                          capture_output=True, env={**os.environ, **GIT_ENV}).returncode == 0


def bundle_objects(path: Path, scratch: Path) -> set[str]:
    """Every object a bundle carries: unpack it into a scratch bare repository."""
    target = scratch / ("unpacked-" + hashlib.sha1(str(path).encode()).hexdigest()[:8])
    shutil.rmtree(target, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "--bare", str(target)], check=True, env={**os.environ, **GIT_ENV})
    out = subprocess.run(["git", "-C", str(target), "fetch", "-q", str(path), "+refs/*:refs/bundle/*"],
                         capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert out.returncode == 0, out.stderr
    assert not (target / "shallow").exists(), "the seed is a shallow cut"
    objs = git(target, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    return {line for line in objs.splitlines() if line}


# ------------------------------------------------------------ the slice 2 seams


def lifecycle():
    try:
        return ic.lifecycle()
    except ModuleNotFoundError as exc:
        if exc.name in {"multiagents.executor.lifecycle", "multiagents.executor"}:
            raise NotImplementedError("IC-C10: the public backend lifecycle is not implemented") from exc
        raise


def seed_registry(root: Path):
    """Q1: the registry is constructed with host ProjectPaths only."""
    from multiagents.paths import ProjectPaths
    try:
        seeds = importlib.import_module("multiagents.provision.seeds")
    except ModuleNotFoundError as exc:
        if exc.name in {"multiagents.provision", "multiagents.provision.seeds"}:
            raise NotImplementedError("IC-C9: the public seed registry is not implemented") from exc
        raise
    paths = ProjectPaths(Path(root))
    paths.ensure()
    return seeds.SeedRegistry(paths)


def wired_backend(backend_root: Path, registry):
    """Q2: only the specified seeds= wiring is accepted."""
    backend_root = Path(backend_root)
    backend_root.mkdir(parents=True, exist_ok=True)
    return lifecycle().ReferenceBackend(backend_root, seeds=registry)


def refused(fn, *args, code=None) -> BaseException:
    """Q3: registry refusal is a SeedError with a contractual code."""
    seeds = importlib.import_module("multiagents.provision.seeds")
    try:
        fn(*args)
    except seeds.SeedError as exc:
        assert exc.code in {"unauthorised", "seed_unavailable", "corrupt"}
        if code is not None:
            assert exc.code == code
        return exc
    raise AssertionError("the call was not refused")


def reason_of(outcome: Any) -> str:
    return json.dumps(outcome, default=lambda o: getattr(o, "__dict__", str(o)))


# -------------------------------------------------------------------- git shim

SHIM = r'''#!{python}
import json, os, subprocess, sys
real, state = {real!r}, {state!r}
args = sys.argv[1:]
sub, skip = None, False
for a in args:
    if skip:
        skip = False
        continue
    if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
        skip = True
        continue
    if a.startswith("-"):
        continue
    sub = a
    break
cfg = json.load(open(state))
mutating = sub in cfg["mutating"]
n = cfg["seen"] + (1 if mutating else 0)
rc = subprocess.run([real] + args).returncode
if mutating:
    cfg["seen"] = n
    cfg["log"].append(sub)
    json.dump(cfg, open(state, "w"))
    if n == cfg["fail_at"]:
        sys.exit(75)
sys.exit(rc)
'''
MUTATING = ["clone", "fetch", "checkout", "switch", "reset", "read-tree", "restore", "init",
            "unpack-objects", "index-pack", "bundle", "update-ref", "symbolic-ref", "branch",
            "worktree", "config", "remote", "pull", "unbundle", "repack", "gc", "checkout-index",
            "reflog", "pack-refs", "update-index", "write-tree"]


class GitShim:
    """A `git` on PATH that runs the real one and then fails the N-th
    state-changing call (exit 75): a crash at every step of a provisioning."""

    def __init__(self, tmp: Path, monkeypatch):
        self.dir = tmp / "git-shim"
        self.dir.mkdir()
        self.state = tmp / "git-shim.json"
        script = self.dir / "git"
        script.write_text(SHIM.format(python=sys.executable, real=shutil.which("git"),
                                      state=str(self.state)))
        script.chmod(0o755)
        self.arm(0)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")

    def arm(self, fail_at: int) -> None:
        self.state.write_text(json.dumps({"fail_at": fail_at, "seen": 0, "log": [],
                                          "mutating": MUTATING}))

    def seen(self) -> int:
        return json.loads(self.state.read_text())["seen"]

    def log(self) -> list[str]:
        return json.loads(self.state.read_text())["log"]


# --------------------------------------------------------------- the provider

_REPORT_BLOCK = '''
REPORT_FILE = %(report)r
def ic_report(when):
    r = subprocess.run([sys.executable, REPORT_FILE], capture_output=True, text=True)
    body = r.stdout.split("IC-REPORT ", 1)[-1] if "IC-REPORT " in r.stdout else "{}"
    with open(base + "/reports.jsonl", "a") as f:
        f.write(json.dumps({"tag": fx.get("tag"), "when": when, "report": json.loads(body),
                            "stderr": r.stderr[-500:]}) + "\\n")
def ic_sh(cmd):
    env = dict(os.environ, FX_DIR=base, GIT_AUTHOR_NAME="fx", GIT_AUTHOR_EMAIL="fx@example.invalid",
               GIT_COMMITTER_NAME="fx", GIT_COMMITTER_EMAIL="fx@example.invalid")
    r = subprocess.run(["sh", "-c", cmd], cwd=os.getcwd(), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("sh failed (%%d): %%s: %%s" %% (r.returncode, cmd[:200], r.stderr[-400:]))
    return r
def ic_exec(codes):
    for code in ([codes] if isinstance(codes, str) else codes):
        scope = {"os": os, "sys": sys, "subprocess": subprocess, "json": json, "time": time,
                 "base": base, "fx": fx, "sh": ic_sh}
        try:
            exec(compile(code, "fx-exec", "exec"), scope)
        except BaseException as e:
            with open(base + "/exec_errors.jsonl", "a") as f:
                f.write(json.dumps({"tag": fx.get("tag"), "error": repr(e)}) + "\\n")
if fx.get("report"):
    ic_report("start")
if fx.get("early"):
    ic_exec(fx["early"])
'''


def patch_agent_script(text: str) -> str:
    marks = {
        'if fx.get("ignore_term"):': _REPORT_BLOCK % {"report": str(REPORT_FILE)},
        'if fx.get("commit"):': 'if fx.get("exec"):\n    ic_exec(fx["exec"])\n',
        'v = fx.get("verdict")': ('if fx.get("after"):\n    ic_exec(fx["after"])\n'
                                  'if fx.get("report_end"):\n    ic_report("end")\n'),
    }
    for marker, block in marks.items():
        assert text.count(marker) == 1, marker
        text = text.replace(marker, block + marker, 1)
    return text


def _provider_class():
    from nc_fixture.m4_agent import M4Provider

    class IcProvider(M4Provider):
        def __init__(self, tmp: Path, name: str, sock: Path, **extra):
            super().__init__(tmp, name, sock, **extra)
            script = self.dir / "agent.py"
            script.write_text(patch_agent_script(script.read_text()))
            # K17.2: a provider that declares nothing is refused by slice 3.
            self.entry["needs"] = {"credentials": False, "network": False, "rpc": False}

        def reports(self, tag: str | None = None, when: str | None = None) -> list[dict]:
            path = self.dir / "reports.jsonl"
            if not path.is_file():
                return []
            rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
            return [r for r in rows if (tag is None or r["tag"] == tag)
                    and (when is None or r["when"] == when)]

        def exec_errors(self) -> list[dict]:
            path = self.dir / "exec_errors.jsonl"
            return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.is_file() else []

        def markers(self) -> list[str]:
            return sorted(p.name for p in self.dir.glob("marker-*"))

    return IcProvider


def _world_class():
    from nc_fixture.m4_world import M4World
    from nc_fixture.world import unwrap  # noqa: F401
    Provider = _provider_class()

    class IcWorld(M4World):
        def __init__(self, tmp_path, monkeypatch, *, kind: str | None = "isolated",
                     import_limits: dict | None = None, scheduler: dict | None = None,
                     inject: bool = True, backend: str = BACKEND_SPEC):
            self.plan_file = tmp_path / "ic-plan.json"
            self.plan_file.write_text("{}")
            self.backend_root = tmp_path / "ic-backend"
            super().__init__(tmp_path, monkeypatch, tick_seconds=0.2, scheduler={
                "tick_seconds": 0.2, "retries": {"backoff_seconds": [0.2, 0.2, 0.2]},
                **(scheduler or {})})
            self.checkpoint_provider = self.provider("iccheckpoint")
            self.agent("iccheckpoint", "iccheckpoint", writes=False)
            self.checkpoint_number = 0
            self.max_wait = ROUNDS
            if inject:
                monkeypatch.setenv(ic.BACKEND_ENV, backend)
                monkeypatch.setenv(ic.PLAN_ENV, str(self.plan_file))
                monkeypatch.setenv("IC_BACKEND_ROOT", str(self.backend_root))
                monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
                    [str(SUPPORT), str(SRC), os.environ.get("PYTHONPATH", "")]))
            if kind:
                self.project["executor"] = {"kind": kind}
            if import_limits:
                self.project["isolation"] = {"import": dict(import_limits)}

        def start_scheduler(self, timeout=WAIT):
            # Explicit missing-surface stub: do not wait for an impossible launch.
            seed_registry(self.root)
            lifecycle()
            return super().start_scheduler(timeout=timeout)

        def until(self, pred, timeout=WAIT, **kw):
            return super().until(pred, timeout=timeout, **kw)

        def wait_state(self, node_id, state, timeout=WAIT, **kw):
            return super().wait_state(node_id, state, timeout=timeout, **kw)

        def crash_paused_provision(self):
            """Crash scheduler and the detached owner at the provisioning gate."""
            owner = json.loads(Path(str(self.plan_file) + ".paused-owner").read_text())
            pid, pgid = owner["pid"], owner["pgid"]
            assert pid != os.getpid() and pgid != os.getpgrp()
            assert os.getpgid(pid) == pgid
            self.kill9()
            os.killpg(pgid, signal.SIGKILL)
            from nc_harness import DEAD_TIMEOUT, alive
            wait_until(lambda: not alive(pid), "the paused provisioning owner to die",
                       timeout=DEAD_TIMEOUT)

        def checkpoint(self):
            """Wait for independent scheduler work, never a fixed delay."""
            self.checkpoint_number += 1
            node = self.simple(f"CHECK-{self.checkpoint_number}", "iccheckpoint", fx={})
            return self.wait_state(node, "done", timeout=WAIT)

        # -- configuration --------------------------------------------------
        def provider(self, name, **extra):
            fx = Provider(self.tmp, name, self.sock, **extra)
            self.providers[name] = fx
            return fx

        def pair(self):
            """Providers fxw (worker) / fxr (reviewer), agents wk / rv."""
            self.fxw = self.provider("fxw")
            self.fxr = self.provider("fxr")
            self.agent("wk", "fxw", writes=True)
            self.agent("rv", "fxr", writes=True)

        def plan(self, plan: dict) -> None:
            self.plan_file.write_text(json.dumps(plan))

        def set_executor(self, kind: str) -> None:
            """A live config change (IC-C1: it applies to attempts not yet recorded)."""
            self.project["executor"] = {"kind": kind}
            self.write_config()

        # -- observation ----------------------------------------------------
        def runs(self, node_id: str) -> list[dict]:
            return [r for r in self.get(node_id).get("runs") or [] if isinstance(r, dict)]

        def evidence(self, attempt_id: str) -> dict:
            from multiagents.scheduler.engine import attempts
            from multiagents.scheduler.store import Store
            with Store(self.root).transaction(write=False) as db:
                return json.loads(json.dumps(attempts(db).get(attempt_id, {}).get("launch_evidence") or {}))

        def attempt(self, attempt_id: str) -> dict:
            from multiagents.scheduler.engine import attempts
            from multiagents.scheduler.store import Store
            with Store(self.root).transaction(write=False) as db:
                return json.loads(json.dumps(attempts(db).get(attempt_id, {})))

        def host_store(self) -> dict[str, str]:
            """Every file of the host object store with its digest: the
            'byte-for-byte unchanged' of P5."""
            base = self.root / ".git" / "objects"
            out = {}
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    out[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
            return out

        def host_refs(self) -> dict[str, str]:
            out = git(self.root, "for-each-ref", "--format=%(refname) %(objectname)")
            return dict(line.split() for line in out.splitlines() if line.strip())

        def host_has(self, oid: str) -> bool:
            return git_ok(self.root, "cat-file", "-e", oid)

        def wait_spawn(self, tag, fx=None, timeout: float = SPAWN):
            return super().wait_spawn(tag, fx, timeout)

        def spawn_started(self, fx, n: int, timeout: float = WAIT):
            return self.until(lambda: len(fx.calls()) >= n and fx.calls(), timeout,
                              what=f"{n} spawn(s) of {fx.name}")

        def report(self, fx, tag: str, when: str = "start", timeout: float = SPAWN) -> dict:
            rows = self.until(lambda: fx.reports(tag, when), timeout,
                              what=f"the {when} report of {tag}")
            return rows[0]["report"]

    return IcWorld


def make_world(tmp_path, monkeypatch, **kw):
    return _world_class()(tmp_path, monkeypatch, **kw)


PAIR = """\
template: icpair
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: wrk, kind: simple, agent: wk, task: "implement the thing"}
    - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
"""


def start_pair(w, rounds: int = 3):
    """Register and instantiate the worker/reviewer loop; (top, wk, rv)."""
    w.start_scheduler()
    w.register_ok(PAIR)
    top = w.instantiate_ok("icpair", {"rounds": rounds})
    wk, rv = w.children(top)
    w.until(lambda: w.fxw.calls(), SPAWN, what="the first worker round to start")
    return top, wk, rv


# --------------------------------------------------------------- report checks

def host_set(repo: Path, commit: str) -> set[str]:
    out = git(repo, "rev-list", "--objects", commit)
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def isolated_clone_problems(rep: dict, host: Path, commit: str) -> list[str]:
    """What is wrong with a fresh seeded repository (IC-C10); [] when sound."""
    bad = []
    if rep.get("head") != commit:
        bad.append(f"HEAD {rep.get('head')} is not the checkout commit {commit}")
    if rep.get("alternates"):
        bad.append("alternates present: " + rep["alternates"].strip())
    if not rep.get("git_is_dir"):
        bad.append(".git is not a directory of its own")
    if rep.get("commondir"):
        bad.append("a linked worktree (commondir)")
    if len([x for x in rep.get("worktrees", []) if x.startswith("worktree ")]) != 1:
        bad.append("more than one worktree is listed: " + repr(rep.get("worktrees")))
    if rep.get("status"):
        bad.append("the checkout is not clean: " + repr(rep["status"]))
    if not rep.get("fsck_ok"):
        bad.append("fsck fails")
    if str(host) in rep.get("config", "") or str(host) in "\n".join(rep.get("remotes", [])):
        bad.append("a remote or config value points at the host repository")
    outside = sorted(set(rep.get("objects", [])) - host_set(host, commit))
    if outside:
        bad.append(f"{len(outside)} object(s) outside the seed's reachability, e.g. {outside[0]}")
    missing = sorted(host_set(host, commit) - set(rep.get("objects", [])))
    if missing:
        bad.append(f"{len(missing)} reachable object(s) missing, e.g. {missing[0]}")
    if rep.get("local_object_count") != len(rep.get("objects", [])):
        bad.append(f"own store holds {rep.get('local_object_count')} objects but the "
                   f"repository sees {len(rep.get('objects', []))}: borrowed objects")
    stray = sorted(oid for oid in rep.get("refs", {}).values() if oid not in host_set(host, commit))
    if stray:
        bad.append("a ref points outside the seed: " + stray[0])
    unreach = sorted(set(rep.get("reflog_oids", [])) - host_set(host, commit))
    if unreach:
        bad.append("a reflog names an object outside the seed: " + unreach[0])
    return bad


def reachability_problems(rep: dict, host: Path, commit: str) -> list[str]:
    """P8: nothing in the repository outside the seed root's reachability."""
    allowed = host_set(host, commit)
    bad = []
    outside = sorted(set(rep.get("objects", [])) - allowed)
    if outside:
        bad.append(f"{len(outside)} object(s) outside reachability, e.g. {outside[0]}")
    for name, oid in rep.get("refs", {}).items():
        if oid not in allowed:
            bad.append(f"ref {name} -> {oid} is outside reachability")
    unreach = sorted(set(rep.get("reflog_oids", [])) - allowed)
    if unreach:
        bad.append("a reflog names " + unreach[0])
    if rep.get("alternates"):
        bad.append("alternates present")
    return bad


# The first activation of an alias leaves this behind (IC-C12): a ref and a tag on an
# unreferenced commit, a dangling blob, a marker inside .git and a file in HOME.
JUNK = r'''
env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@b")
tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
ghost = subprocess.run(["git", "commit-tree", tree, "-m", "junk of activation one"],
                       capture_output=True, text=True, env=env).stdout.strip()
subprocess.run(["git", "branch", "junk", ghost])
subprocess.run(["git", "tag", "junk-tag", ghost])
blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input="junk blob of activation one\n",
                      capture_output=True, text=True).stdout.strip()
open(".git/ic-marker", "w").write("activation one")
open(os.path.join(os.environ["HOME"], "keep.txt"), "w").write("home of activation one")
open(base + "/junk", "w").write(ghost + " " + blob)
'''


def wait_until(fn, what: str, timeout: float = WAIT, step: float = 0.1):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")
