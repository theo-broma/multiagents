"""Harness for the phase 7 part 1 node-scheduler contract tests (NC-R*),
`context/specs/phase7-part1-contract.md`, milestone M1.

Everything here talks to the scheduler the way a client does: the CLI
(`multiagents scheduler start|stop`), the unix socket of NC-R8, and the host
seams of NC-R71. Nothing imports a private name of the scheduler.

Where the contract fixes a name, the harness uses it exactly. Where it is
silent, the harness is *tolerant* about the encoding (error envelope, the key
that carries a transition's name) and says so in the helper's docstring, so a
developer can see which spelling the suite does not care about. The few
assumptions that cannot be made tolerant are listed at the top of each test
module that relies on them.

A scheduler a test starts is always stopped by the fixture finalizer, by the
pid it itself reported, never by pattern.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents.paths import ProjectPaths, state_root  # noqa: E402

# Every wait is bounded by one of these: short by default, overridable per call.
CLI_TIMEOUT = 20         # one `multiagents ...` command
START_TIMEOUT = 12       # `scheduler start` until the socket answers
STATUS_TIMEOUT = 2       # one status probe while waiting for start
DEAD_TIMEOUT = 5         # a stopped / killed scheduler until its pid is gone
RPC_TIMEOUT = 10         # one request on the socket (plus the op's own `timeout`)
CONNECT_TIMEOUT = 10     # a bare connection's socket timeout
THREAD_START_TIMEOUT = 5   # in_threads: all callables reaching the barrier
THREAD_JOIN_TIMEOUT = 20   # in_threads: each callable finishing
TEARDOWN_STOP = 8        # close(): the polite `scheduler stop`
REAP_TIMEOUT = 3         # close(): waiting for a killed child

ROOT = object()          # token sentinel: the root capability
ABSENT = object()        # token sentinel: no `token` key at all

# Fields the scheduler derives on read (NC-R23, NC-R52); never part of "the
# store" when a test asks whether a refused write changed it.
DERIVED = {"eligible", "blocked", "ready", "ready_since", "eligible_since", "active_run"}


# ---------------------------------------------------------------- replies

def code(reply: dict) -> str | None:
    """The error code of a reply, whichever way it is enveloped: `error` as a
    string, `error` as an object carrying `error` or `code`, or an error
    object sitting in `result`."""
    for holder in (reply.get("error"), reply.get("result")):
        if isinstance(holder, str) and reply.get("ok") is False:
            return holder
        if isinstance(holder, dict):
            found = holder.get("error") or holder.get("code")
            if isinstance(found, str):
                return found
    return None


def problems(reply: dict) -> list:
    for holder in (reply, reply.get("error"), reply.get("result")):
        if isinstance(holder, dict) and isinstance(holder.get("problems"), list):
            return holder["problems"]
    return []


def current_revision(reply: dict):
    for holder in (reply, reply.get("error"), reply.get("result")):
        if isinstance(holder, dict) and "current_revision" in holder:
            return holder["current_revision"]
    return None


def stored(node: dict) -> dict:
    return {k: v for k, v in node.items() if k not in DERIVED}


def tname(transition: dict) -> str:
    """The name of a transition, whichever key carries it; a `node.` prefix
    (the events.jsonl spelling) is dropped."""
    for key in ("transition", "kind", "type", "event", "name"):
        if isinstance(transition.get(key), str):
            return transition[key].removeprefix("node.")
    raise AssertionError(f"transition has no name under transition/kind/type/event/name: {transition}")


def tnode(transition: dict):
    for key in ("node", "node_id", "id"):
        if key in transition:
            return transition[key]
    return None


def find_count(value: Any, key: str):
    """The number under `key` in the first (depth-first) mapping that has it."""
    if isinstance(value, dict):
        if key in value and isinstance(value[key], int) and not isinstance(value[key], bool):
            return value[key]
        for v in value.values():
            found = find_count(v, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for v in value:
            found = find_count(v, key)
            if found is not None:
                return found
    return None


def tool(fn, *args, **kwargs):
    """Call an MCP tool function, sync or async."""
    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


# ---------------------------------------------------------------- scheduler

class Sched:
    """One project with the gate on or off, and the scheduler's client side."""

    def __init__(self, tmp_path: Path, monkeypatch, enabled: bool = True,
                 scheduler: dict | None = None):
        self.monkeypatch = monkeypatch
        h.as_root(monkeypatch)
        self.tmp = tmp_path
        self.p = sc.Project(tmp_path)
        self.p.add_provider("acme")
        self.p.add_provider("acme2")
        self.p.add_agent("worker", "acme", "acme/m1")
        self.p.add_agent("reviewer", "acme", "acme/m1")
        self.p.add_agent("other", "acme2", "acme2/m1")
        self.root = self.p.root
        self.slug = ProjectPaths(self.root).slug
        self.set_scheduler({"enabled": enabled, **(scheduler or {})})
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        self._pids: set[int] = set()
        self._procs: list[subprocess.Popen] = []
        self._log = tmp_path / "scheduler.log"

    # -- configuration ----------------------------------------------------
    def set_scheduler(self, block: dict | None) -> None:
        if block is None:
            self.p.project.pop("scheduler", None)
        else:
            self.p.project["scheduler"] = block
        self.p.write()
        self._reset_server()

    def set_gate(self, enabled: bool) -> None:
        block = dict(self.p.project.get("scheduler") or {})
        block["enabled"] = enabled
        self.set_scheduler(block)

    def _reset_server(self) -> None:
        from multiagents import server
        server._reset()

    # -- locations --------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return state_root() / "scheduler" / self.slug

    @property
    def rpc_dir(self) -> Path:
        return state_root() / "scheduler-rpc" / self.slug

    @property
    def sock(self) -> Path:
        return self.rpc_dir / "rpc.sock"

    @property
    def events(self) -> Path:
        return self.root / ".multiagents" / "events.jsonl"

    # -- lifecycle --------------------------------------------------------
    def cli(self, *args: str, timeout: float = CLI_TIMEOUT) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "multiagents.cli", "--path", str(self.root), *args],
            capture_output=True, text=True, timeout=timeout, cwd=self.root)

    def start(self, wait: float = START_TIMEOUT) -> None:
        """`multiagents scheduler start`, then wait until the socket answers.

        The command may return once the scheduler is ready (detached) or stay
        in the foreground; both are accepted. A command that exits non-zero
        fails the test with its own output."""
        log = open(self._log, "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "multiagents.cli", "--path", str(self.root),
             "scheduler", "start"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd=self.root,
            start_new_session=True)
        self._procs.append(proc)
        deadline = time.time() + wait
        last: Exception | None = None
        while time.time() < deadline:
            rc = proc.poll()
            if rc not in (None, 0):
                raise AssertionError(
                    f"`multiagents scheduler start` exited {rc}:\n{self._log.read_text()[-2000:]}")
            try:
                reply = self.rpc("scheduler_status", timeout=STATUS_TIMEOUT)
                if reply.get("ok") is True:
                    pid = (reply.get("result") or {}).get("pid")
                    if isinstance(pid, int):
                        self._pids.add(pid)
                    return
            except (OSError, ValueError, ImportError) as exc:
                last = exc
            time.sleep(0.1)
        raise AssertionError(
            f"the scheduler never answered on {self.sock} within {wait}s ({last!r}):\n"
            f"{self._log.read_text()[-2000:] if self._log.exists() else ''}")

    def pid(self) -> int:
        reply = self.rpc("scheduler_status")
        assert reply.get("ok") is True, reply
        pid = reply["result"]["pid"]
        self._pids.add(pid)
        return pid

    def stop(self) -> None:
        pid = self.pid() if self.sock.exists() else None
        out = self.cli("scheduler", "stop")
        assert out.returncode == 0, (out.stdout, out.stderr)
        if pid is not None:
            self.wait_dead(pid)

    def wait_dead(self, pid: int, timeout: float = DEAD_TIMEOUT) -> None:
        for proc in self._procs:
            if proc.pid == pid:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        end = time.time() + timeout
        while alive(pid) and time.time() < end:
            time.sleep(0.05)
        assert not alive(pid), f"scheduler pid {pid} is still alive"

    def kill9(self) -> None:
        pid = self.pid()
        os.kill(pid, signal.SIGKILL)
        self.wait_dead(pid)

    def restart(self, hard: bool = False) -> None:
        if hard:
            self.kill9()
        else:
            self.stop()
        self.start()

    def close(self) -> None:
        """Finalizer: never raises, never signals a process it did not start."""
        try:
            if self.sock.exists():
                self.cli("scheduler", "stop", timeout=TEARDOWN_STOP)
        except Exception:
            pass
        for pid in list(self._pids):
            if alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        for proc in self._procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.kill()
            try:
                proc.wait(timeout=REAP_TIMEOUT)
            except Exception:
                pass
        try:
            self._reset_server()
        except Exception:
            pass

    # -- tokens -----------------------------------------------------------
    def root_token(self) -> str:
        from multiagents.scheduler import root_capability
        return root_capability(self.root)

    def issue(self, run_id: str, node_id: str,
              permissions: set[str] | None = None) -> str:
        from multiagents.scheduler import issue_run_capability
        return issue_run_capability(
            self.root, run_id, node_id,
            {"read", "delegate"} if permissions is None else permissions)

    def revoke(self, run_id: str) -> None:
        from multiagents.scheduler import revoke_run_capability
        revoke_run_capability(self.root, run_id)

    # -- the wire ---------------------------------------------------------
    def connect(self, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(self.sock))
        return conn

    def rpc(self, op: str, args: dict | None = None, token: Any = ROOT,
            request_id: str | None = None, timeout: float | None = None,
            extra: dict | None = None) -> dict:
        """One request on a fresh connection; the parsed reply line."""
        if token is ROOT:
            token = self.root_token()
        if timeout is None:
            # a long-poll op (`wait_for_nodes`) is given room to answer
            wait = (args or {}).get("timeout")
            timeout = RPC_TIMEOUT + (wait if isinstance(wait, (int, float)) else 0)
        request: dict[str, Any] = {"op": op, "args": args or {},
                                   "request_id": request_id or uuid.uuid4().hex}
        if token is not ABSENT:
            request["token"] = token
        request.update(extra or {})
        with self.connect(timeout) as conn:
            conn.sendall(json.dumps(request).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    raise ConnectionError(f"scheduler closed the connection: {buf!r}")
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])

    def ok(self, op: str, args: dict | None = None, token: Any = ROOT, **kw) -> Any:
        reply = self.rpc(op, args, token, **kw)
        assert reply.get("ok") is True, f"{op} {args}: {reply}"
        return reply["result"]

    # -- plan helpers -----------------------------------------------------
    def plan_revision(self, token: Any = ROOT) -> int:
        result = self.ok("list_nodes", {}, token)
        assert isinstance(result.get("plan_revision"), int), result
        return result["plan_revision"]

    def create_raw(self, fields: dict, token: Any = ROOT, **kw) -> dict:
        args = dict(fields)
        if "plan_revision" not in args:
            args["plan_revision"] = self.plan_revision(token)
        return self.rpc("create_node", args, token, **kw)

    def create(self, token: Any = ROOT, **fields) -> dict:
        fields.setdefault("kind", "simple")
        if fields["kind"] == "simple":
            fields.setdefault("agent", "worker")
            fields.setdefault("task", "do the thing")
        reply = self.create_raw(fields, token)
        assert reply.get("ok") is True, f"create_node {fields}: {reply}"
        return reply["result"]

    def get(self, node_id: str, token: Any = ROOT) -> dict:
        return self.ok("get_node", {"id": node_id}, token)

    def update_raw(self, node_id: str, revision: int | None = None, token: Any = ROOT,
                   **fields) -> dict:
        if revision is None:
            revision = self.get(node_id)["revision"]
        return self.rpc("update_node", {"id": node_id, "revision": revision, **fields}, token)

    def update(self, node_id: str, token: Any = ROOT, **fields) -> dict:
        reply = self.update_raw(node_id, token=token, **fields)
        assert reply.get("ok") is True, f"update_node {node_id} {fields}: {reply}"
        return reply["result"]

    def cancel_raw(self, node_id: str, revision: int | None = None, token: Any = ROOT) -> dict:
        if revision is None:
            revision = self.get(node_id)["revision"]
        return self.rpc("cancel_node", {"id": node_id, "revision": revision}, token)

    def snapshot(self, token: Any = ROOT) -> dict:
        """The whole visible plan as stored: plan revision and every node's
        stored fields. Equal before and after a refused write."""
        result = self.ok("list_nodes", {}, token)
        return {"plan_revision": result["plan_revision"],
                "nodes": sorted((stored(n) for n in result["nodes"]),
                                key=lambda n: n["id"])}

    def transitions(self, cursor: int = 0) -> list[dict]:
        """Every transition with seq > cursor, without touching root's cursor."""
        result = self.ok("wait_for_nodes", {"cursor": cursor, "timeout": 0.2})
        return result["transitions"]

    def names(self, cursor: int = 0) -> list[str]:
        return [tname(t) for t in self.transitions(cursor)]

    def event_kinds(self) -> list[str]:
        """The `node.*` kinds in events.jsonl, in file order."""
        if not self.events.is_file():
            return []
        out = []
        for line in self.events.read_text().splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("kind") or entry.get("type") or entry.get("event")
            if isinstance(kind, str) and kind.startswith("node."):
                out.append(kind.removeprefix("node."))
        return out

    def in_threads(self, calls: list) -> list:
        """Run zero-argument callables at once; results in order."""
        results: list = [None] * len(calls)
        gate = threading.Barrier(len(calls))

        def run(i, fn):
            gate.wait(timeout=THREAD_START_TIMEOUT)
            try:
                results[i] = fn()
            except Exception as exc:        # surfaced by the assertion on the result
                results[i] = exc

        threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=THREAD_JOIN_TIMEOUT)
        return results


def files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in [root, *root.rglob("*")]]


# ---------------------------------------------------------------- fixtures
# Imported by the test modules (`from nc_harness import nc, live, off`).

import pytest  # noqa: E402


@pytest.fixture
def nc(tmp_path, monkeypatch):
    """A project with the gate on and no scheduler running."""
    sched = Sched(tmp_path, monkeypatch, enabled=True)
    yield sched
    sched.close()


@pytest.fixture
def off(tmp_path, monkeypatch):
    """A project with the gate off (the shipped default)."""
    sched = Sched(tmp_path, monkeypatch, enabled=False)
    yield sched
    sched.close()


@pytest.fixture
def live(nc):
    """The gate on and the scheduler started."""
    nc.start()
    return nc
