"""Harness for `tests/test_agent_survival.py` (contract `context/specs/agent-survival.md`).

Everything here is a real process:

- **The server** is `python -m multiagents.server`, spoken to over MCP stdio
  with raw JSON-RPC, exactly as the orchestrator's CLI does. It can be sent
  EOF, SIGTERM, SIGHUP, SIGKILL, SIGSTOP; two can run on one project at once.
- **The agent** is a small Python script standing in for a provider CLI. It
  prints opencode-shaped NDJSON (`step_start` / `text` / `step_finish` with
  `sessionID`, per-step token deltas and cost), so usage is summed rather than
  max'ed and a double count is visible. What it does is a plan carried in the
  task text as `SVPLAN:<base64 json>`; every invocation appends its argv, pid
  and pgid to a log, so a resume can be seen.
- **The CLI** (`multiagents stop`, `multiagents run --no-launch`) is run as a
  subprocess.

Nothing in multiagents is patched or mocked. The only configuration is the
project's own `.multiagents/config/*.yaml`: the shipped providers are disabled
(their budget readers shell out to real CLIs and the network), and one
provider, `svstub`, points at the stub. Its stream block mirrors the shipped
opencode block, so the parsing under test is the real rule engine.
"""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

SRC = str(Path(__file__).resolve().parents[2] / "src")
sys.path.insert(0, SRC)

from multiagents.paths import ProjectPaths                    # noqa: E402
from multiagents.tree import Node, Tree, now as tree_now       # noqa: E402

ORCH_SESSION = "sv-orch-session-0001"
INIT_SESSION = "sv-init-session-0001"


# ---------------------------------------------------------------------------
# The stub agent
# ---------------------------------------------------------------------------

STUB = r'''#!{python}
import base64, json, os, re, signal, subprocess, sys, time
LOG = {log!r}
argv = sys.argv[1:]
plan = None
for a in argv:
    m = re.search(r"SVPLAN:([A-Za-z0-9_=-]+)", a)
    if m:
        plan = json.loads(base64.urlsafe_b64decode(m.group(1)))
        break
with open(LOG, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "pid": os.getpid(), "pgid": os.getpgid(0),
                         "cwd": os.getcwd(), "t": time.time(),
                         "planned": plan is not None}}) + "\n")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if plan is None:
    # A resume (steer): the message is new, the plan is not in it.
    sid = ""
    if "--resume" in argv:
        sid = argv[argv.index("--resume") + 1]
    emit({{"type": "step_start", "sessionID": sid or "sv-resumed", "part": {{}}}})
    emit({{"type": "text", "sessionID": sid or "sv-resumed", "part": {{"text": "RESUMED-OK"}}}})
    emit({{"type": "step_finish", "sessionID": sid or "sv-resumed",
          "part": {{"reason": "stop", "tokens": {{"input": 1, "output": 1}}, "cost": 0}}}})
    sys.exit(0)

for step in plan["steps"]:
    op, args = step[0], step[1:]
    if op == "emit":
        emit(args[0])
    elif op == "line":            # a raw line, verbatim
        sys.stdout.write(args[0] + "\n"); sys.stdout.flush()
    elif op == "sleep":
        time.sleep(float(args[0]))
    elif op == "wait_for":        # a file the test creates
        deadline = time.time() + float(args[1])
        while not os.path.exists(args[0]) and time.time() < deadline:
            time.sleep(0.05)
    elif op == "pidfile":
        open(args[0], "w").write(str(os.getpid()))
    elif op == "child":           # a grandchild in the agent's process group
        c = subprocess.Popen(["sleep", "1000"])
        open(args[0], "w").write(str(c.pid))
    elif op == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    elif op == "touch":
        open(args[0], "a").close()
    elif op == "write":           # a file in the worktree, for merge_agent
        open(args[0], "w").write(args[1])
    elif op == "heartbeat":       # forever: the file grows while this lives
        while True:
            with open(args[0], "a") as fh:
                fh.write("x")
            time.sleep(0.1)
    elif op == "child_heartbeat": # the same, from a child in this process group
        subprocess.Popen(["sh", "-c", "while :; do printf x >> '%s'; sleep 0.1; done" % args[0]])
    elif op == "exit":
        sys.exit(int(args[0]))
sys.exit(0)
'''


def plan_token(steps: list[list[Any]]) -> str:
    raw = json.dumps({"steps": steps}).encode()
    return "SVPLAN:" + base64.urlsafe_b64encode(raw).decode()


# opencode-shaped lines -------------------------------------------------------

def step_start(sid: str) -> list:
    return ["emit", {"type": "step_start", "sessionID": sid, "part": {}}]


def text(sid: str, words: str) -> list:
    return ["emit", {"type": "text", "sessionID": sid, "part": {"text": words}}]


def step_finish(sid: str, inp: int, out: int, cost: float) -> list:
    return ["emit", {"type": "step_finish", "sessionID": sid,
                     "part": {"reason": "stop", "tokens": {"input": inp, "output": out},
                              "cost": cost}}]


def turns(sid: str, n: int, *, inp: int = 100, out: int = 10, cost: float = 0.01,
          gap: float = 0.0, label: str = "turn") -> list[list]:
    """`n` complete model turns, each a known usage delta."""
    steps: list[list] = []
    for i in range(n):
        steps += [step_start(sid), text(sid, f"{label}-{i}"), step_finish(sid, inp, out, cost)]
        if gap:
            steps.append(["sleep", gap])
    return steps


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def alive(pid: int | None) -> bool:
    """A live, non-zombie process."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X")
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def read_pid(path: Path, timeout: float = 15.0) -> int:
    assert wait_until(lambda: path.is_file() and path.read_text().strip(), timeout), (
        f"the stub never wrote {path.name}: it did not start")
    return int(path.read_text().strip())


def kill_quietly(*pids: int | None) -> None:
    for pid in pids:
        if not pid:
            continue
        for sig in (signal.SIGKILL,):
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
            try:
                os.kill(pid, sig)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class McpError(AssertionError):
    pass


class Server:
    """One `python -m multiagents.server` process, spoken to over stdio."""

    def __init__(self, project: "Project", *, session: str = ORCH_SESSION,
                 agent_id: str = "", depth: int = 0, handshake: bool = True):
        self.project = project
        env = project.env(session=session)
        if agent_id:
            env.update({"MULTIAGENTS_AGENT_ID": agent_id, "MULTIAGENTS_DEPTH": str(depth),
                        "MULTIAGENTS_CAN_SPAWN": "1", "MULTIAGENTS_PARENT_ID": ""})
        self.stderr_path = project.base / f"server-{time.monotonic_ns()}.err"
        self._err = self.stderr_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "multiagents.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err,
            env=env, cwd=str(project.root), text=True, bufsize=1)
        project.servers.append(self)
        self._id = 0
        self._buf = ""
        if handshake:
            self.initialize()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stderr(self) -> str:
        try:
            return self.stderr_path.read_text()[-3000:]
        except OSError:
            return ""

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _reply(self, want: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        out = self.proc.stdout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([out], [], [], 0.2)
            if not ready:
                if self.proc.poll() is not None:
                    break
                continue
            line = out.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want:
                return msg
        raise McpError(f"no reply to request {want} within {timeout}s "
                       f"(server exit={self.proc.poll()}); stderr:\n{self.stderr()}")

    def request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        return self._reply(self._id, timeout)

    def initialize(self, timeout: float = 60.0) -> None:
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "sv-test", "version": "0"}},
                     timeout=timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, timeout: float = 30.0, *, args: dict | None = None,
             **arguments: Any) -> Any:
        """A tool's result, decoded. Tool errors come back as the dict they are.
        `args` carries arguments whose names clash with this method's own
        (`wait_for_agents(timeout=...)`)."""
        arguments = {**arguments, **(args or {})}
        msg = self.request("tools/call", {"name": tool, "arguments": arguments},
                           timeout=timeout)
        if "error" in msg:
            raise McpError(f"{tool}: {msg['error']}")
        result = msg.get("result") or {}
        if result.get("structuredContent") is not None:
            sc = result["structuredContent"]
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        content = result.get("content") or []
        textual = "".join(c.get("text", "") for c in content if c.get("type") == "text")
        try:
            return json.loads(textual)
        except ValueError:
            return textual

    def start(self, task: str, *, agent: str = "worker", timeout: int = 0) -> str:
        args: dict[str, Any] = {"agent": agent, "task": task}
        if timeout:
            args["timeout"] = timeout
        result = self.call("start_agent", 60, args=args)
        assert isinstance(result, dict) and result.get("agent_id") and \
            "error" not in result, f"start_agent failed: {result}\n{self.stderr()}"
        return result["agent_id"]

    # --- endings ---------------------------------------------------------

    def eof(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def signal(self, sig: int) -> None:
        try:
            self.proc.send_signal(sig)
        except OSError:
            pass

    def kill(self) -> None:
        self.signal(signal.SIGKILL)
        self.proc.wait(timeout=10)

    def exited(self, timeout: float) -> bool:
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGCONT)
            except OSError:
                pass
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self._err.close()


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------

class Project:
    """A throwaway git project whose one agent, `worker`, runs the stub."""

    def __init__(self, tmp_path: Path, *, executor: str = "local",
                 agent_overrides: dict[str, Any] | None = None):
        self.base = tmp_path.resolve()
        self.servers: list[Server] = []
        self.executor = executor
        self.user_home = self.base / "home"
        self.user_home.mkdir(exist_ok=True)

        self.root = self.base / "proj"
        self.root.mkdir(exist_ok=True)
        # The stub, its log and every marker live on the project's writable
        # `.multiagents` mount, so a docker-executed stub sees the same paths
        # (Facts: the run dir sits on the writable bind mount).
        self.sv = self.root / ".multiagents" / "sv"
        self.sv.mkdir(parents=True, exist_ok=True)
        self.stub_log = self.sv / "stub.log"
        self.stub = self.sv / "svstub"
        python = sys.executable if executor == "local" else "/usr/bin/env python3"
        self.stub.write_text(STUB.format(python=python, log=str(self.stub_log)))
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        # `.multiagents/` is gitignored in a real project; untracked here it
        # would leave the root dirty and `gitops.merge` refuses a dirty target
        # tree (SV-R9: merge_agent on an adopted node).
        (self.root / ".gitignore").write_text(".multiagents/\n")
        for args in (["init", "-q", "-b", "main"], ["add", ".gitignore"],
                     ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", str(self.root), *args], env=genv,
                           check=True, capture_output=True)

        cfg = self.root / ".multiagents" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        providers: dict[str, Any] = {
            name: {"enabled": False} for name in ("claude", "opencode", "agy")}
        providers["svstub"] = {
            "bin": str(self.stub),
            "family": "svstub",
            "usage_mode": "delta",
            "spawn": {"args": ["{prompt}"], "resume": ["--resume", "{session_id}"]},
            "stream": {
                "format": "ndjson",
                "session_id_paths": ["sessionID"],
                "rules": [
                    {"match": {"type": "text"}, "as": "text",
                     "fields": {"text": "part.text"}},
                    {"match": {"type": "step_finish"}, "as": "step",
                     "fields": {"tokens": "part.tokens", "cost": "part.cost",
                                "state": "part.reason"}},
                    {"match": {"type": "step_start"}, "as": "step", "fields": {}},
                    # A final result, for the "Decided" rule on a dead process
                    # with no exit status: judged from the stream.
                    {"match": {"type": "result"}, "as": "result",
                     "fields": {"status": "subtype", "text": "result"}},
                ],
            },
        }
        (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": providers}))
        project: dict[str, Any] = {"team": ""}
        if executor == "docker":
            project["executor"] = {"kind": "docker"}
        (cfg / "project.yaml").write_text(yaml.safe_dump(project))
        worker = {"provider": "svstub", "model": "m", "can_spawn": False,
                  "description": "stub", "instructions": "", "timeout": 120,
                  "silence_timeout": 120, **(agent_overrides or {})}
        spawner = {**worker, "can_spawn": True}
        (cfg / "agents.yaml").write_text(yaml.safe_dump(
            {"agents": {"worker": worker, "spawner": spawner}}))

        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        # The two launched sessions, as `driver.py` records them: session role
        # is read off these (Decided, "Session role").
        for nid, role, sess in (("dr-orch01", "orchestrator", ORCH_SESSION),
                                ("dr-init01", "initializer", INIT_SESSION)):
            self.tree.add(Node(id=nid, agent=role, provider="svstub", model="m",
                               parent=None, depth=0, status="running",
                               task=f"{role} session", session=sess, role=role,
                               started_at=tree_now()))

    # --- environment -------------------------------------------------------

    def env(self, *, session: str = ORCH_SESSION) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MULTIAGENTS_", "CLAUDE_"))}
        env.update({
            "PYTHONPATH": SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "HOME": str(self.user_home),
            "MULTIAGENTS_PROJECT": str(self.root),
            "MULTIAGENTS_STATE_DIR": os.environ["MULTIAGENTS_STATE_DIR"],
            "MULTIAGENTS_CONFIG_DIR": os.environ["MULTIAGENTS_CONFIG_DIR"],
            "PYTHONUNBUFFERED": "1",
        })
        if session:
            env["MULTIAGENTS_SESSION_ID"] = session
        return env

    def server(self, **kw: Any) -> Server:
        return Server(self, **kw)

    def cli(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "multiagents.cli", *args],
                              cwd=str(self.root), env=self.env(session=""),
                              capture_output=True, text=True, timeout=timeout)

    # --- files -------------------------------------------------------------

    def marker(self, name: str) -> Path:
        return self.sv / name

    def run_dir(self, agent_id: str) -> Path:
        return self.paths.run_dir(agent_id)

    def exit_status(self, agent_id: str) -> Path:
        return self.run_dir(agent_id) / "exit_status"

    def node(self, agent_id: str) -> Node | None:
        return Tree(self.paths.tree_file, self.paths.events_file).get(agent_id)

    def status(self, agent_id: str) -> str:
        node = self.node(agent_id)
        return node.status if node else "absent"

    def describe(self, agent_id: str) -> str:
        node = self.node(agent_id)
        if node is None:
            return "absent"
        return f"{node.status}: {node.reason!r}"

    def events(self, agent_id: str, kind: str | None = None) -> list[dict]:
        if not self.paths.events_file.is_file():
            return []
        out = []
        for line in self.paths.events_file.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("agent") == agent_id and (kind is None or e.get("kind") == kind):
                out.append(e)
        return out

    def invocations(self) -> list[dict]:
        if not self.stub_log.is_file():
            return []
        return [json.loads(l) for l in self.stub_log.read_text().splitlines() if l]

    def wait_status(self, agent_id: str, statuses: set[str], timeout: float) -> str:
        wait_until(lambda: self.status(agent_id) in statuses, timeout)
        return self.status(agent_id)

    def cleanup(self) -> None:
        for s in self.servers:
            s.close()
        for inv in self.invocations():
            try:
                os.killpg(inv["pgid"], signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(inv["pid"], signal.SIGKILL)
            except OSError:
                pass


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def growing(path: Path, window: float = 0.6) -> bool:
    """Is something still appending to `path`? (A heartbeat, any executor.)"""
    before = size(path)
    time.sleep(window)
    return size(path) > before >= 0 or (before < 0 and size(path) > 0)


def stopped_growing(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not growing(path, 0.5):
            return True
    return False


def docker_ready() -> str:
    """Why the docker variants cannot run here, or "" when they can."""
    import shutil
    if os.environ.get("SV_TEST_DOCKER") != "1":
        return "docker cases are opt-in: set SV_TEST_DOCKER=1 with a working container setup"
    if not shutil.which("docker"):
        return "no docker binary"
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        return "docker daemon not reachable"
    return ""
