"""A project, a scheduler process, an RPC client and fixture providers.

Everything here goes through the contract's public surfaces
(`context/specs/phase7-part1-contract.md`): the CLI `multiagents scheduler ...`
(NC-R16), the NC-R8 socket protocol, `multiagents.scheduler.root_capability`
(NC-R71), and the observable state (`tree.json`, `events.jsonl`). Nothing
imports a scheduler internal.

Assumptions the contract leaves open (each loose on purpose):
- `scheduler start` may daemonize (the CLI returns) or stay in the foreground;
  readiness is "the socket answers `scheduler_status`".
- the first `create_node` needs a `plan_revision`; `create_simple` reads
  `plan_revision` from `list_nodes` when present, and on a `conflict` reply
  retries with the `current_revision` it carries (NC-R7).
- a node op's `result` is the node dict itself or `{"node": {...}}`.
- the `scheduler_status` result carries the scheduler's `pid` (NC-R16).
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import yaml

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "support"))

import c3_harness as h  # noqa: E402
from multiagents.paths import ProjectPaths, state_root  # noqa: E402
from multiagents.tree import Tree  # noqa: E402

from nc_fixture.agent import FixtureProvider, alive, task  # noqa: E402

CLI = "import sys; from multiagents.cli import main; sys.exit(main(sys.argv[1:]))"


class RpcError(AssertionError):
    pass


def err_code(reply: dict) -> str | None:
    """The error code of a reply: `error` may be a string or an object."""
    if reply.get("ok"):
        return None
    e = reply.get("error")
    if isinstance(e, dict):
        return e.get("error") or e.get("code")
    if isinstance(e, str):
        return e
    res = reply.get("result")
    if isinstance(res, dict):
        return res.get("error")
    return None


def unwrap(result: Any) -> dict:
    """The node dict of a node-op result."""
    if isinstance(result, dict) and isinstance(result.get("node"), dict):
        return result["node"]
    return result


def run_id_of(active_run: Any) -> str | None:
    if isinstance(active_run, dict):
        return active_run.get("run_id") or active_run.get("id")
    return active_run or None


def blocked_codes(node: dict) -> list[str]:
    return [b.get("code") if isinstance(b, dict) else b for b in (node.get("blocked") or [])]


class World:
    """One project with a gate-on config, fixture providers and a scheduler."""

    def __init__(self, tmp_path: Path, monkeypatch, *, scheduler: dict | None = None,
                 gate: bool = True):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        h.as_root(monkeypatch)
        self.root = tmp_path / "proj"
        h.make_git_repo(self.root)
        (self.root / ".multiagents" / "config" / "agents").mkdir(parents=True)
        self.paths = ProjectPaths(self.root)
        self.providers: dict[str, FixtureProvider] = {}
        self.agents: dict[str, dict] = {}
        self.project: dict[str, Any] = {
            "team": "", "limits": {"max_depth": 7},
            "budget": {"blind_cooldown_seconds": 1},
            "scheduler": {"enabled": gate, "tick_seconds": 1,
                          "admission_timeout_seconds": 5, **(scheduler or {})}}
        self._tick = time.time()
        self._popen: list[subprocess.Popen] = []
        self.last_start: subprocess.CompletedProcess | None = None
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        self.fx = self.provider("fx")
        self.agent("worker", "fx", writes=False)
        self.agent("spawner", "fx", writes=False, can_spawn=True, max_children=6)

    # ------------------------------------------------------------ config
    def provider(self, name: str, **extra: Any) -> FixtureProvider:
        fx = FixtureProvider(self.tmp, name, **extra)
        self.providers[name] = fx
        return fx

    def agent(self, name: str, provider: str, **extra: Any) -> None:
        self.agents[name] = {"provider": provider, "model": f"{provider}/m1",
                             "writes": False, **extra}

    def _write(self, rel: str, data: Any) -> None:
        path = self.root / ".multiagents" / "config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data))
        self._tick += 10
        os.utime(path, (self._tick, self._tick))

    def write_config(self) -> None:
        self._write("providers.yaml",
                    {"providers": {n: p.entry for n, p in self.providers.items()}})
        self._write("agents.yaml", {"agents": self.agents})
        self._write("project.yaml", self.project)
        self._write("models.yaml", {"models": {}})

    # ----------------------------------------------------- scheduler process
    def _env(self) -> dict[str, str]:
        return dict(os.environ, MULTIAGENTS_PROJECT=str(self.root))

    def cli(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", CLI, "--path", str(self.root), *args],
                              cwd=self.root, env=self._env(), capture_output=True,
                              text=True, timeout=timeout)

    @property
    def sock(self) -> Path:
        return state_root() / "scheduler-rpc" / self.paths.slug / "rpc.sock"

    def start_scheduler(self, timeout: float = 20) -> int:
        """`multiagents scheduler start`; returns the scheduler's pid once it
        answers on the socket. Fails with the CLI output if it never does."""
        self.write_config()
        proc = subprocess.Popen(
            [sys.executable, "-c", CLI, "--path", str(self.root), "scheduler", "start"],
            cwd=self.root, env=self._env(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self._popen.append(proc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.status()
                if status.get("pid"):
                    return int(status["pid"])
            except (OSError, ValueError, KeyError, ImportError, AssertionError):
                pass
            if proc.poll() not in (None, 0):
                break
            time.sleep(0.1)
        out = ""
        if proc.poll() is not None and proc.stdout:
            out = proc.stdout.read()
        raise AssertionError(
            f"the scheduler did not come up (exit {proc.poll()}): {out[-1500:]}")

    def stop_scheduler(self) -> subprocess.CompletedProcess:
        res = self.cli("scheduler", "stop")
        self.until(lambda: not self.sock_answers(), timeout=20,
                   what="the scheduler to stop answering")
        return res

    def kill9(self) -> int:
        pid = self.status()["pid"]
        os.kill(int(pid), signal.SIGKILL)
        self.until(lambda: not alive(int(pid)), timeout=10, what="the scheduler to die")
        return int(pid)

    def restart_scheduler(self) -> int:
        self.stop_scheduler()
        return self.start_scheduler()

    def sock_answers(self) -> bool:
        try:
            self.status()
            return True
        except (OSError, ValueError, KeyError, AssertionError):
            return False

    def close(self) -> None:
        pid = None
        try:
            pid = int(self.status().get("pid") or 0)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.cli("scheduler", "stop", timeout=30)
        except Exception:                                   # noqa: BLE001
            pass
        if pid and alive(pid):
            time.sleep(1)
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        for proc in self._popen:
            if proc.poll() is None:
                proc.kill()
        for fx in self.providers.values():
            for pid in fx.pids():
                if alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

    # ------------------------------------------------------------------ rpc
    def raw(self, op: str, token: str | None, args: dict | None = None,
            request_id: str | None = None, timeout: float = 30) -> dict:
        req = {"op": op, "token": token, "args": args or {},
               "request_id": request_id or uuid.uuid4().hex}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(self.sock))
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode())

    def root_token(self) -> str:
        from multiagents.scheduler import root_capability
        return root_capability(self.root)

    def rpc(self, op: str, args: dict | None = None, token: str | None = "root",
            request_id: str | None = None) -> dict:
        """The whole reply. `token="root"` presents the root capability."""
        tok = self.root_token() if token == "root" else token
        return self.raw(op, tok, args, request_id)

    def ok(self, op: str, args: dict | None = None, token: str | None = "root",
           request_id: str | None = None) -> Any:
        """The `result` of a reply that must be ok."""
        reply = self.rpc(op, args, token, request_id)
        if not reply.get("ok"):
            raise RpcError(f"{op} {args} -> {reply}")
        return reply.get("result")

    def status(self) -> dict:
        reply = self.raw("scheduler_status", self.root_token())
        assert reply.get("ok"), reply
        return reply["result"]

    # ---------------------------------------------------------------- nodes
    def plan_revision(self, token: str = "root") -> int:
        res = self.rpc("list_nodes", {}, token).get("result")
        if isinstance(res, dict):
            for key in ("plan_revision", "revision"):
                if isinstance(res.get(key), int):
                    return res[key]
        return 0

    def create(self, fields: dict, token: str = "root", request_id: str | None = None) -> dict:
        """create_node with revision retry; the reply (ok or not)."""
        reply = {}
        rev = self.plan_revision(token)
        for _ in range(4):
            reply = self.rpc("create_node", {**fields, "plan_revision": rev}, token, request_id)
            if err_code(reply) == "conflict":
                e = reply.get("error")
                cur = (e.get("current_revision") if isinstance(e, dict)
                       else (reply.get("result") or {}).get("current_revision"))
                if isinstance(cur, int):
                    rev = cur
                    continue
            break
        return reply

    def simple(self, tag: str, agent: str = "worker", *, token: str = "root",
               parent: str | None = None, prose: str = "do the work", **fields: Any) -> str:
        """Create a simple node whose task is the fixture script `tag` +
        directives; returns the node id. Fields other than directives go to the
        node (`urgent`, `locks`, ...); directives are given as `fx=dict`."""
        fx = fields.pop("fx", {})
        node = {"kind": "simple", "agent": agent, "task": task(tag, prose, **fx),
                "parent": parent, **fields}
        reply = self.create(node, token)
        if not reply.get("ok"):
            raise RpcError(f"create_node {node} -> {reply}")
        return unwrap(reply["result"])["id"]

    def get(self, node_id: str, token: str = "root") -> dict:
        return unwrap(self.ok("get_node", {"id": node_id}, token))

    def list(self, token: str = "root", **filt: Any) -> list[dict]:
        res = self.ok("list_nodes", filt, token)
        if isinstance(res, dict):
            res = res.get("nodes", [])
        return [unwrap(n) for n in res]

    def cancel(self, node_id: str, token: str = "root") -> dict:
        rev = self.get(node_id, token)["revision"]
        return self.rpc("cancel_node", {"id": node_id, "revision": rev}, token)

    def state(self, node_id: str) -> str:
        return self.get(node_id).get("state")

    # ------------------------------------------------------------- observing
    def tree_json(self) -> dict:
        return json.loads(self.paths.tree_file.read_text())

    def tree_nodes(self) -> dict[str, dict]:
        return self.tree_json().get("nodes", {})

    def deferred(self) -> list:
        return self.tree_json().get("deferred", [])

    def events(self, prefix: str = "node.") -> list[dict]:
        path = self.paths.events_file
        if not path.is_file():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            kind = e.get("kind") or e.get("type") or e.get("event") or ""
            if str(kind).startswith(prefix):
                out.append(e)
        return out

    @staticmethod
    def kind_of(event: dict) -> str:
        return str(event.get("kind") or event.get("type") or event.get("event") or "")

    def kinds(self) -> list[str]:
        """Every `node.<transition>` kind in the event log, in order."""
        return [self.kind_of(e) for e in self.events()]

    def index_of(self, transition: str, node_id: str) -> int:
        """Position in the node events of the first `node.<transition>` about
        `node_id` (a ValueError if there is none)."""
        for i, e in enumerate(self.events()):
            if self.kind_of(e) == f"node.{transition}" and node_id in json.dumps(e):
                return i
        raise ValueError(f"no node.{transition} for {node_id}")

    def transitions(self, node_id: str) -> list[str]:
        """The `node.<transition>` kinds that mention `node_id`, in order."""
        return [str(e.get("kind") or e.get("type") or e.get("event"))[len("node."):]
                for e in self.events() if node_id in json.dumps(e)]

    def scheduler_files(self) -> list[Path]:
        base = state_root() / "scheduler" / self.paths.slug
        return [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []

    # --------------------------------------------------------------- waiting
    def until(self, pred: Callable[[], Any], timeout: float = 30, step: float = 0.1,
              what: str = "the condition") -> Any:
        end = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < end:
            try:
                last = pred()
            except (OSError, ValueError, KeyError, RpcError):
                last = None
            if last:
                return last
            time.sleep(step)
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    def wait_state(self, node_id: str, state: str, timeout: float = 30) -> dict:
        return self.until(lambda: (n := self.get(node_id))["state"] == state and n,
                          timeout, what=f"{node_id} to be {state}")

    def wait_running(self, node_id: str, timeout: float = 30) -> dict:
        return self.wait_state(node_id, "running", timeout)

    def wait_spawn(self, tag: str, fx: FixtureProvider | None = None,
                   timeout: float = 30) -> dict:
        fx = fx or self.fx
        return self.until(lambda: (fx.by_tag(tag) or [None])[0], timeout,
                          what=f"the fixture run {tag!r} to start")

    def quiet(self, seconds: float) -> None:
        """Let the scheduler have `seconds` of ticks (it ticks every second)."""
        time.sleep(seconds)

    def gate(self, name: str, fx: FixtureProvider | None = None) -> None:
        (fx or self.fx).open_gate(name)


def write_tree_entries(world: World, build: Callable[[Tree], None]) -> None:
    """Run `build(tree)` against the project's real `Tree` (gate-off state):
    the way legacy deferred entries are made, in their real shape."""
    world.paths.ensure()
    build(Tree(world.paths.tree_file, world.paths.events_file))


_LEGACY = r'''
import asyncio, json, sys
from multiagents import server
async def main():
    r = await server.start_agent(sys.argv[1], sys.argv[2])
    print(json.dumps(r), flush=True)
    rid = r.get("agent_id")
    if rid:
        await server.wait_for_agents([rid], 3000)
asyncio.run(main())
'''


def legacy_start(world: World, agent: str, task_text: str,
                 timeout: float = 60) -> tuple[str, subprocess.Popen]:
    """Launch a run the way today's code does (gate OFF: the config of this
    world must say `scheduler.enabled: false` when this is called), from a
    helper process that keeps supervising it. Returns (run id, helper)."""
    world.write_config()
    proc = subprocess.Popen([sys.executable, "-c", _LEGACY, agent, task_text],
                            cwd=world.root, env=world._env(), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    world._popen.append(proc)
    line = proc.stdout.readline()
    result = json.loads(line)
    assert result.get("agent_id"), f"legacy start was not admitted: {result}"
    return result["agent_id"], proc


def enable_gate(world: World) -> None:
    world.project["scheduler"]["enabled"] = True
    world.write_config()


def call_tool(world: World, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call an MCP tool function of `multiagents.server` as the orchestrator
    (root) does, in this process, sync or async."""
    import asyncio
    import inspect
    from multiagents import server
    server._reset()
    fn = getattr(server, name)
    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out
