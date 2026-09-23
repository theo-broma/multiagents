"""Tooling defect 6 — agents with spawn rights get the multiagents MCP server.

Contract: `context/specs/subagent-mcp.md`, SM-R1..SM-R5.

Black box throughout. A spawn goes in through the server's public tool
(`server.start_agent`) against a throwaway project whose `providers.yaml`
overrides only the `bin:` of each SHIPPED provider (claude, opencode, agy), so
the provider's real spawn block, its real script and whatever this work adds to
either are all in play. The binary at the end is a fake CLI that records what
it was actually handed: argv, cwd, environment, and the contents of every file
or directory those name. "The composed launch" is exactly that record — what
the process receives — so the tests do not care whether the server arrives as
a flag, an inline JSON argument, an environment variable naming a config file,
or a config file in a directory the CLI is pointed at.

The docker executor is exercised the same way. `docker` is faked at the binary
boundary: it reports the project container as running and, for `docker exec`,
records its argv and env-file and then runs the command it was given on this
host, with that environment and working directory. So the argv and env that
would have entered the container reach the same fake CLI, and a spawn under
docker is observed in the same terms as one under local.

"Names the multiagents server" is read structurally: somewhere in what the CLI
receives there is a parseable config (JSON, YAML or TOML — inline or in a file)
holding a mapping keyed `multiagents` whose value declares how to start it
(`command`, `args` or `url`). That is the shape of an MCP server entry in all
three CLIs' config formats, and the name is the one the orchestrator's own
registration already uses (`driver._write_mcp_config`).
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h  # noqa: E402

from multiagents import budget as budget_mod  # noqa: E402
from multiagents import server  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402

PROVIDERS = ("claude", "opencode", "agy")
EXECUTORS = ("local", "docker")
TASK = "SM-TASK-7f3a: do the thing"


# ---------------------------------------------------------------------------
# The fake CLI — records what it was handed, then plays its provider's stream
# ---------------------------------------------------------------------------

FAKE_CLI = r'''#!{python}
import json, os, sys
LOG = {log!r}
FLAVOR = {flavor!r}
MCP_STATUS = {mcp_status!r}
CONFIG_SUFFIXES = (".json", ".jsonc", ".yaml", ".yml", ".toml")

def read(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read(400_000)
    except OSError:
        return None

files = {{}}
def capture(value, walk_dirs=True):
    candidates = [value]
    if value.startswith("-") and "=" in value:
        candidates.append(value.split("=", 1)[1])
    for cand in candidates:
        if not cand or len(cand) > 4096 or "\n" in cand:
            continue
        path = cand if os.path.isabs(cand) else os.path.join(os.getcwd(), cand)
        if os.path.isfile(path):
            text = read(path)
            if text is not None:
                files[path] = {{"real": os.path.realpath(path), "text": text}}
        elif walk_dirs and os.path.isdir(path) and os.path.isabs(cand):
            base_depth = path.rstrip("/").count("/")
            for dirpath, dirnames, filenames in os.walk(path, followlinks=True):
                if dirpath.rstrip("/").count("/") - base_depth >= 2:
                    dirnames[:] = []
                for name in filenames:
                    if name.endswith(CONFIG_SUFFIXES):
                        full = os.path.join(dirpath, name)
                        text = read(full)
                        if text is not None:
                            files[full] = {{"real": os.path.realpath(full), "text": text}}

argv = sys.argv[1:]
for token in argv:
    capture(token)
for key, value in os.environ.items():
    # Identity variables name the project's whole state directory; walking it
    # would find files this launch never pointed the CLI at.
    capture(value, walk_dirs=not key.startswith("MULTIAGENTS_") and key != "PATH")

with open(LOG, "a") as fh:
    fh.write(json.dumps({{"argv": argv, "cwd": os.getcwd(),
                         "env": dict(os.environ), "files": files}}) + "\n")

def emit(obj):
    print(json.dumps(obj)); sys.stdout.flush()

if FLAVOR == "claude":
    init = {{"type": "system", "subtype": "init", "session_id": "sess-1"}}
    if MCP_STATUS:
        init["mcp_servers"] = [{{"name": "multiagents", "status": MCP_STATUS}}]
    emit(init)
    emit({{"type": "assistant", "session_id": "sess-1",
          "message": {{"id": "msg-1", "content": [{{"type": "text", "text": "done"}}]}}}})
    emit({{"type": "result", "subtype": "success", "session_id": "sess-1",
          "result": "done", "usage": {{"input_tokens": 1, "output_tokens": 1}},
          "total_cost_usd": 0}})
elif FLAVOR == "opencode":
    emit({{"type": "step_start", "sessionID": "sess-1", "part": {{}}}})
    emit({{"type": "text", "sessionID": "sess-1", "part": {{"text": "done"}}}})
    emit({{"type": "step_finish", "sessionID": "sess-1",
          "part": {{"reason": "stop", "tokens": {{"input": 1, "output": 1}}, "cost": 0}}}})
else:
    emit({{"event": "init", "conversation_id": "conv-1"}})
    emit({{"event": "result", "conversation_id": "conv-1",
          "result": {{"status": "success", "response": "done",
                     "usage": {{"input_tokens": 1, "output_tokens": 1}}}}}})
sys.exit(0)
'''


FAKE_DOCKER = r'''#!{python}
import json, os, sys
LOG = {log!r}
argv = sys.argv[1:]
fmt = " ".join(argv)
if argv[:1] == ["inspect"]:
    if ".State.Status" in fmt:
        print("running"); sys.exit(0)
    if ".State.StartedAt" in fmt:
        print("2026-09-23T10:00:00.000000000Z"); sys.exit(0)
    sys.exit(1)                     # mount queries: "cannot tell", never "stale"
if argv[:1] != ["exec"]:
    sys.exit(0)                     # image inspect, network, start, ...: fine
rest = argv[1:]
workdir, env_file, env = None, None, {{}}
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if flag in ("--workdir", "-w"):
        workdir = rest.pop(0)
    elif flag == "--env-file":
        env_file = rest.pop(0)
    elif flag in ("--env", "-e"):
        k, _, v = rest.pop(0).partition("=")
        env[k] = v
    elif flag in ("--user", "-u"):
        rest.pop(0)
container, command = rest[0], rest[1:]
text = ""
if env_file:
    text = open(env_file).read()
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
with open(LOG, "a") as fh:
    fh.write(json.dumps({{"docker_exec": argv, "container": container,
                         "workdir": workdir, "env_file": text}}) + "\n")
if workdir:
    os.chdir(workdir)
env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
os.execve("/bin/sh", ["sh", *command[1:]], env)
'''


def _executable(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# The user's own home, seeded with the configuration a real one has
# ---------------------------------------------------------------------------

USER_CONFIG = {
    ".claude/settings.json": json.dumps({"permissions": {"allow": []}}),
    ".claude.json": json.dumps({"projects": {}, "mcpServers": {}}),
    ".config/opencode/opencode.json": json.dumps({"$schema": "https://opencode.ai/config.json"}),
    ".local/share/opencode/auth.json": json.dumps({}),
    ".gemini/settings.json": json.dumps({"mcpServers": {}}),
    ".gemini/antigravity-cli/settings.json": json.dumps({}),
    ".gemini/oauth_creds.json": json.dumps({}),
}


def _snapshot(root: Path) -> dict[str, bytes | str]:
    out: dict[str, bytes | str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = f"-> {os.readlink(p)}"
            elif p.is_file():
                out[rel] = p.read_bytes()
            else:
                out[rel] = "<dir>"
    return out


# ---------------------------------------------------------------------------
# One project, one spawn
# ---------------------------------------------------------------------------

class Project:
    def __init__(self, tmp_path: Path, monkeypatch, provider: str, executor: str,
                 mcp_status: str = ""):
        self.provider = provider
        self.executor = executor
        self.monkeypatch = monkeypatch
        base = tmp_path.resolve()
        self.log = base / "cli.log"
        self.docker_log = base / "docker.log"

        self.user_home = base / "userhome"
        for rel, text in USER_CONFIG.items():
            target = self.user_home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        monkeypatch.setenv("HOME", str(self.user_home))

        bindir = base / "bin"
        bindir.mkdir()
        self.fake = _executable(bindir / provider, FAKE_CLI.format(
            python=sys.executable, log=str(self.log), flavor=provider,
            mcp_status=mcp_status))
        if executor == "docker":
            dockerdir = base / "dockerbin"
            dockerdir.mkdir()
            _executable(dockerdir / "docker", FAKE_DOCKER.format(
                python=sys.executable, log=str(self.docker_log)))
            monkeypatch.setenv("PATH", f"{dockerdir}{os.pathsep}{os.environ.get('PATH', '')}")

        self.root = h.make_git_repo(base / "proj")
        cfg = self.root / ".multiagents" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        # Only the binary changes: the shipped block, its script, and anything
        # this work adds to either are what is under test.
        (cfg / "providers.yaml").write_text(yaml.safe_dump(
            {"providers": {provider: {"bin": str(self.fake)}}}))
        project: dict = {"team": ""}
        if executor == "docker":
            project["executor"] = {"kind": "docker", "docker": {"network": "bridge"}}
        (cfg / "project.yaml").write_text(yaml.safe_dump(project))
        agents = {}
        for name, spawn in (("spawner", True), ("leaf", False)):
            agents[name] = {"provider": provider, "model": "m", "can_spawn": spawn,
                            "description": "x", "instructions": ""}
        (cfg / "agents.yaml").write_text(yaml.safe_dump({"agents": agents}))
        self.paths = ProjectPaths(self.root)

        h.as_root(monkeypatch)
        monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.root))
        monkeypatch.chdir(self.root)
        # The budget readers look at the real machine's billing state; nothing
        # here is about routing, so every provider reads as available.
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {})
        server._reset()

    def calls(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def spawn(self, agent: str, task: str = TASK, settle: float = 30.0) -> tuple[str, dict]:
        """Start `agent`, wait for it to finish, return (id, the CLI's record)."""
        async def go() -> str:
            result = await server.start_agent(agent, task)
            assert "error" not in result, result
            agent_id = result["agent_id"]
            deadline = time.monotonic() + settle
            while time.monotonic() < deadline:
                node = server.runner().tree.get(agent_id)
                if node is not None and node.status not in ("pending", "running"):
                    break
                await asyncio.sleep(0.1)
            return agent_id

        try:
            agent_id = asyncio.run(go())
        finally:
            server._reset()
        if self.executor == "docker":
            # The fixture's own guard: the run really went through `docker exec`.
            execs = (self.docker_log.read_text() if self.docker_log.is_file() else "")
            assert agent_id in execs, f"no docker exec for {agent_id}: {self.status(agent_id)}"
        mine = [c for c in self.calls() if any(task in a for a in c["argv"])]
        assert mine, (f"the {self.provider} CLI was never run with the task under "
                      f"{self.executor}; calls seen: {[c['argv'][:3] for c in self.calls()]}; "
                      f"node: {self.status(agent_id)}")
        return agent_id, mine[-1]

    def status(self, agent_id: str) -> str:
        node = h.Tree(self.paths.tree_file, self.paths.events_file).get(agent_id)
        return f"{node.status}: {node.__dict__.get('reason') or ''}" if node else "absent"

    def run_dir(self, agent_id: str) -> Path:
        return self.paths.run_dir(agent_id).resolve()

    def events(self, agent_id: str) -> list[dict]:
        if not self.paths.events_file.is_file():
            return []
        out = []
        for line in self.paths.events_file.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("agent") == agent_id:
                out.append(e)
        return out


# ---------------------------------------------------------------------------
# Reading the record: where, if anywhere, is the multiagents server named?
# ---------------------------------------------------------------------------

def _parse(text: str):
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _server_entries(obj) -> list[dict]:
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (key == "multiagents" and isinstance(value, dict)
                    and any(k in value for k in ("command", "args", "url"))):
                found.append(value)
            found.extend(_server_entries(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_server_entries(item))
    return found


def server_refs(call: dict) -> list[tuple[str, dict, str | None]]:
    """Every multiagents server entry the CLI was handed, as
    (where it came from, the entry, the real path of the file it was in)."""
    refs = []
    inline = [("argv", a) for a in call["argv"]]
    inline += [("argv", a.split("=", 1)[1]) for a in call["argv"]
               if a.startswith("-") and "=" in a]
    inline += [(f"env {k}", v) for k, v in call["env"].items()]
    for where, value in inline:
        if value.lstrip().startswith(("{", "[")):
            for entry in _server_entries(_parse(value)):
                refs.append((where, entry, None))
    for path, info in call["files"].items():
        for entry in _server_entries(_parse(info["text"])):
            refs.append((f"file {path}", entry, info["real"]))
    return refs


def _program(entry: dict) -> str:
    command = entry.get("command")
    if isinstance(command, list):
        return str(command[0]) if command else ""
    return str(command or "")


def _under(path: Path, ancestor: Path) -> bool:
    return path == ancestor or ancestor in path.parents


# ---------------------------------------------------------------------------
# SM-R1 — an agent with spawn rights has the server
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r1_an_agent_with_spawn_rights_is_handed_the_multiagents_server(
        tmp_path, monkeypatch, provider, executor):
    project = Project(tmp_path, monkeypatch, provider, executor)
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, (f"{provider} under {executor}: a can_spawn agent's launch names no "
                  f"multiagents MCP server. argv={call['argv']}")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r1_under_docker_the_server_command_is_not_uv(tmp_path, monkeypatch, provider):
    """The CLI, and so the server it starts, runs inside the container, and
    `uv` is not there (spec, "facts the implementation must respect")."""
    project = Project(tmp_path, monkeypatch, provider, "docker")
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, f"no multiagents server in {provider}'s docker launch: {call['argv']}"
    for where, entry, _ in refs:
        program = _program(entry)
        assert program and Path(program).name != "uv", (
            f"{provider} under docker starts the server with {program!r} ({where}); "
            f"uv is not in the container")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r1_the_named_server_starts_and_offers_consult_and_start_agent(
        tmp_path, monkeypatch, provider):
    """What the CLI would do with the entry it was given: start it with the
    agent's own environment and ask for its tools over MCP stdio."""
    project = Project(tmp_path, monkeypatch, provider, "local")
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, f"no multiagents server in {provider}'s launch: {call['argv']}"
    where, entry, _ = refs[0]
    command = entry.get("command")
    argv = ([str(c) for c in command] if isinstance(command, list)
            else [str(command)] + [str(a) for a in entry.get("args") or []])
    env = {**call["env"], **{k: str(v) for k, v in (entry.get("env")
                                                     or entry.get("environment") or {}).items()}}
    cwd = entry.get("cwd") or call["cwd"]
    tools = _mcp_tool_names(argv, env, cwd)
    assert {"consult", "start_agent"} <= tools, (
        f"the server {provider} was handed ({where}: {argv}) offers {sorted(tools)}")


def _mcp_tool_names(argv: list[str], env: dict, cwd: str, timeout: float = 90.0) -> set[str]:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=cwd, text=True)
    deadline = time.monotonic() + timeout

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def reply(want_id):
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want_id:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "sm-test", "version": "0"}}})
        init = reply(1)
        assert init is not None, (f"server {argv} did not answer initialize; "
                                  f"stderr: {_stderr(proc)}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = reply(2)
        assert listed is not None, f"no tools/list reply; stderr: {_stderr(proc)}"
        return {t["name"] for t in listed.get("result", {}).get("tools", [])}
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _stderr(proc) -> str:
    proc.kill()
    try:
        return (proc.stderr.read() or "")[-1500:]
    except Exception:
        return "<unreadable>"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r1_a_server_entry_that_sets_identity_names_the_child_not_its_parent(
        tmp_path, monkeypatch, provider):
    """SM-R3's attribution depends on the server seeing the CHILD's id. An
    entry is free to rely on inheriting the CLI's environment; if it sets the
    identity itself, it must be this agent's."""
    project = Project(tmp_path, monkeypatch, provider, "local")
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, f"no multiagents server in {provider}'s launch: {call['argv']}"
    assert call["env"].get("MULTIAGENTS_AGENT_ID") == agent_id
    for where, entry, _ in refs:
        env = entry.get("env") or entry.get("environment") or {}
        if "MULTIAGENTS_AGENT_ID" in env:
            assert env["MULTIAGENTS_AGENT_ID"] == agent_id, (where, env)


# ---------------------------------------------------------------------------
# SM-R2 — an agent without spawn rights still has none
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r2_an_agent_without_spawn_rights_is_handed_no_server(
        tmp_path, monkeypatch, provider, executor):
    project = Project(tmp_path, monkeypatch, provider, executor)
    agent_id, call = project.spawn("leaf")
    assert server_refs(call) == [], (
        f"{provider} under {executor}: a can_spawn: false agent was handed the "
        f"multiagents server: {[(w, e) for w, e, _ in server_refs(call)]}")
    if provider == "claude":
        assert "--strict-mcp-config" in call["argv"], call["argv"]
        assert not any(a == "--mcp-config" or a.startswith("--mcp-config=")
                       for a in call["argv"]), call["argv"]


@pytest.mark.parametrize("executor", EXECUTORS)
def test_sm_r2_the_same_project_gives_it_to_one_agent_and_not_the_other(
        tmp_path, monkeypatch, executor):
    """The decision is per agent, not per project or per provider: two spawns
    from one project on one provider differ only by `can_spawn`."""
    project = Project(tmp_path, monkeypatch, "claude", executor)
    _, with_rights = project.spawn("spawner", task=TASK + " (a)")
    _, without = project.spawn("leaf", task=TASK + " (b)")
    assert server_refs(with_rights), with_rights["argv"]
    assert not server_refs(without), without["argv"]


# ---------------------------------------------------------------------------
# SM-R3 — the caller is the agent, and the gates hold
# ---------------------------------------------------------------------------

class Tree3:
    """A project with a conversational advisor, as seen from a child's server."""

    def __init__(self, tmp_path, monkeypatch, *, limits: dict | None = None):
        self.project = Project(tmp_path, monkeypatch, "claude", "local")
        cfg = self.project.root / ".multiagents" / "config"
        agents = yaml.safe_load((cfg / "agents.yaml").read_text())
        agents["agents"]["advisor"] = {"provider": "claude", "model": "m",
                                       "conversational": True, "can_spawn": False,
                                       "description": "x", "instructions": ""}
        (cfg / "agents.yaml").write_text(yaml.safe_dump(agents))
        if limits:
            project = yaml.safe_load((cfg / "project.yaml").read_text())
            project["limits"] = limits
            (cfg / "project.yaml").write_text(yaml.safe_dump(project))
        paths = self.project.paths
        self.tree = h.Tree(paths.tree_file, paths.events_file)
        self.tree.add(h.Node(id="ag-child", agent="spawner", provider="claude",
                             model="m", parent=None, depth=1, status="running"))
        self.tree.add(h.Node(id="ag-sibling", agent="spawner", provider="claude",
                             model="m", parent=None, depth=1, status="done"))
        self.monkeypatch = monkeypatch

    def as_child(self, *, can_spawn: bool = True, depth: int = 1,
                 agent_id: str = "ag-child") -> None:
        h.as_subagent(self.monkeypatch, agent_id=agent_id, parent="", depth=depth,
                      can_spawn=can_spawn)
        server._reset()

    def call(self, coro_or_value):
        async def go():
            value = coro_or_value()
            return await value if asyncio.iscoroutine(value) else value
        try:
            return asyncio.run(go())
        finally:
            server._reset()


def test_sm_r3_a_child_may_consult_and_the_consulted_node_is_its_own(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch)
    t.as_child()
    result = t.call(lambda: server.consult("advisor", "is this right?", 60))
    assert not result.get("error"), result
    node = t.tree.get(result["agent_id"])
    assert node is not None and node.parent == "ag-child", node
    assert node.depth == 2, node


def test_sm_r3_a_child_cannot_merge_a_node_it_does_not_own(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch)
    t.as_child()
    result = t.call(lambda: server.merge_agent("ag-sibling"))
    assert result.get("error"), result
    assert "ag-sibling" in result["error"]
    assert t.tree.get("ag-sibling").status == "done"


def test_sm_r3_a_child_cannot_stop_a_node_it_does_not_own(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch)
    t.as_child()
    result = t.call(lambda: server.stop_agent("ag-sibling"))
    assert result.get("error"), result
    assert t.tree.get("ag-sibling").status == "done"


def test_sm_r3_a_child_without_spawn_rights_cannot_consult(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch)
    t.as_child(can_spawn=False)
    result = t.call(lambda: server.consult("advisor", "hello", 60))
    assert result.get("error"), result
    assert not any(n.get("parent") == "ag-child"
                   for n in t.tree.read()["nodes"].values()), "a node was created anyway"


def test_sm_r3_max_depth_holds_for_a_child(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch, limits={"max_depth": 2})
    t.as_child(depth=2)
    result = t.call(lambda: server.consult("advisor", "hello", 60))
    assert result.get("error"), result
    assert "depth" in result["error"].lower(), result


def test_sm_r3_max_children_holds_for_a_child(tmp_path, monkeypatch):
    t = Tree3(tmp_path, monkeypatch)
    cfg = t.project.root / ".multiagents" / "config"
    agents = yaml.safe_load((cfg / "agents.yaml").read_text())
    agents["agents"]["leaf"]["max_children"] = 1
    (cfg / "agents.yaml").write_text(yaml.safe_dump(agents))
    t.tree.add(h.Node(id="ag-grandchild", agent="leaf", provider="claude", model="m",
                      parent="ag-child", depth=2, status="running"))
    t.as_child()
    result = t.call(lambda: server.start_agent("leaf", "another"))
    assert result.get("error"), result
    assert "children" in result["error"], result


# ---------------------------------------------------------------------------
# SM-R4 — the user's configuration is never touched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", PROVIDERS)
def test_sm_r4_every_config_naming_the_server_belongs_to_the_run(
        tmp_path, monkeypatch, provider, executor):
    """The run's own state is `runs/<id>/` or the agent's private home under
    the multiagents state directory (`homes/<id>`) — decided in the spec,
    "Decided, from the tester's questions". The path compared is the file's
    REAL path, so a private home that merely symlinks into the user's own CLI
    configuration (as `home_links` do) still fails."""
    project = Project(tmp_path, monkeypatch, provider, executor)
    agent_id, call = project.spawn("spawner")
    refs = server_refs(call)
    assert refs, f"no multiagents server in {provider}'s launch: {call['argv']}"
    allowed = (project.run_dir(agent_id), project.paths.home(agent_id).resolve())
    for where, _, real in refs:
        if real is None:
            continue                       # passed on the command line / inline
        assert any(_under(Path(real), a) for a in allowed), (
            f"{provider} under {executor}: server config {real} ({where}) is "
            f"outside the run's own state {[str(a) for a in allowed]}")


@pytest.mark.parametrize("executor", EXECUTORS)
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("agent", ["spawner", "leaf"])
def test_sm_r4_a_spawn_creates_or_modifies_nothing_in_the_users_home(
        tmp_path, monkeypatch, provider, executor, agent):
    project = Project(tmp_path, monkeypatch, provider, executor)
    before = _snapshot(project.user_home)
    project.spawn(agent)
    after = _snapshot(project.user_home)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"{provider}/{executor}/{agent} touched the user's home: "
        f"added={added} removed={removed} changed={changed}")


# ---------------------------------------------------------------------------
# SM-R5 — a server that cannot start does not break the agent
# ---------------------------------------------------------------------------

def _unavailable_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        text = json.dumps(e).lower()
        if ("mcp" in text or "server" in text) and any(
                w in text for w in ("unavailable", "fail", "could not", "not start",
                                    "did not start", "missing")):
            out.append(e)
    return out


def test_sm_r5_the_agent_finishes_and_the_run_records_the_server_was_unavailable(
        tmp_path, monkeypatch):
    """The CLI reports the multiagents server failed to start (claude's own
    stream-json init event, `mcp_servers[].status == "failed"`) and carries on."""
    project = Project(tmp_path, monkeypatch, "claude", "local", mcp_status="failed")
    agent_id, _ = project.spawn("spawner")
    status = project.status(agent_id)
    assert status.startswith("done"), f"the run did not complete: {status}"
    assert _unavailable_events(project.events(agent_id)), (
        f"no event says the server was unavailable: {project.events(agent_id)}")


def test_sm_r5_a_server_that_started_records_no_such_event(tmp_path, monkeypatch):
    project = Project(tmp_path, monkeypatch, "claude", "local", mcp_status="connected")
    agent_id, _ = project.spawn("spawner")
    assert project.status(agent_id).startswith("done")
    assert not _unavailable_events(project.events(agent_id)), project.events(agent_id)
