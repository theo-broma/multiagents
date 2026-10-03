"""Harness for `tests/test_session_persistence.py` (contract
`context/specs/session-persistence.md`, SP-R1..SP-R5).

Built on `sv_harness.Project`: a throwaway git project whose one agent,
`worker`, runs a stub CLI (`svstub`) through the real server over MCP stdio.
This adds what the session-persistence contract needs on top:

- a `transcript:` declaration on the stub provider, so the provider "declares
  where its sessions live" exactly as claude does in `providers.yaml`;
- a finished node seeded in the tree with a real branch, a real commit and a
  real worktree, as a steer finds one after its run ended;
- a fake `docker` binary for the docker-executor cases, which answers the
  queries the executor makes and runs `docker exec` commands locally, so a
  docker project can be exercised without a daemon (the same approach as
  `tests/test_subagent_mcp.py`). Every call it receives is logged.

Nothing in multiagents is patched. Configuration is the project's own
`.multiagents/config/*.yaml`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

import sv_harness as sv   # also puts src/ on sys.path
from sv_harness import wait_until   # noqa: F401  (re-exported for tests)

from multiagents.tree import Node, Tree, now as tree_now   # noqa: E402

GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

# The stub provider's declared transcript location. `~` is the user's HOME as
# the host tooling sees it (the test points HOME at a temp dir).
TRANSCRIPT_DIR = "~/.svstub/sessions/{slug}"
# Claude's shipped usage vocabulary (C12-R1a); the stub's records use it.
CLAUDE_USAGE_DECLARATION = {
    "usage_path": "message.usage",
    "context_fields": ["input_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens"],
}
TRANSCRIPT_PREFIX = ".svstub/sessions"


def slug(path: Path | str) -> str:
    """Claude's own folding of a working directory, as `providers.yaml`
    documents for `{slug}`: every character outside [A-Za-z0-9] becomes '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env={**os.environ, **GIT_ID}, check=check)


def branches(repo: Path) -> set[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def worktrees(repo: Path) -> set[str]:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    return {line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")}


# ---------------------------------------------------------------------------
# A fake docker
# ---------------------------------------------------------------------------

FAKE_DOCKER = r'''#!{python}
import json, os, sys
LOG = {log!r}
MODE = {mode!r}
argv = sys.argv[1:]
with open(LOG, "a") as fh:
    fh.write(json.dumps(argv) + "\n")
fmt = " ".join(argv)
if argv[:1] == ["info"]:
    print("27.0"); sys.exit(0)
if argv[:1] == ["inspect"]:
    if ".State.Status" in fmt:
        print("running"); sys.exit(0)
    if ".State.StartedAt" in fmt:
        print("2026-09-23T10:00:00.000000000Z"); sys.exit(0)
    if MODE == "drift" and ".Mounts" in fmt:
        # A container created against an older configuration: it has one
        # mount the configuration no longer wants and none of those it does.
        if ".Source" in fmt:
            print("/obsolete/source>/obsolete/dest")
        else:
            print("/obsolete/dest:true")
        sys.exit(0)
    sys.exit(1)                     # mount queries: "cannot tell", never "stale"
if argv[:1] != ["exec"]:
    sys.exit(0)                     # image inspect, network, run, rm, start: fine
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
if not rest:
    sys.exit(0)
container, command = rest[0], rest[1:]
if env_file:
    for line in open(env_file).read().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
if workdir:
    os.chdir(workdir)
env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
env.setdefault("HOME", os.environ.get("HOME", "/"))
if not command:
    sys.exit(0)
os.execvpe(command[0], command, env)
'''


def install_fake_docker(base: Path, monkeypatch, mode: str = "") -> Path:
    """Put a fake `docker` first on PATH; returns its call log."""
    bindir = base / "dockerbin"
    bindir.mkdir(exist_ok=True)
    log = base / "docker.log"
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER.format(python=sys.executable, log=str(log), mode=mode))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def docker_calls(log: Path) -> list[list[str]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def removed_container(log: Path, container: str) -> bool:
    """Did anything run `docker rm ... <container>`?"""
    return any(c[:1] == ["rm"] and container in c for c in docker_calls(log))


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------

class Project(sv.Project):
    """`sv_harness.Project`, with the stub provider declaring a transcript."""

    def __init__(self, tmp_path: Path, monkeypatch, *, executor: str = "local",
                 transcript: bool = True):
        super().__init__(tmp_path, executor=executor)
        self.monkeypatch = monkeypatch
        # Host tooling in this process (building an executor to read its
        # mounts) sees the same HOME as the server the test talks to.
        monkeypatch.setenv("HOME", str(self.user_home))
        cfg = self.root / ".multiagents" / "config"
        providers = yaml.safe_load((cfg / "providers.yaml").read_text())
        if transcript:
            providers["providers"]["svstub"]["transcript"] = {
                "dir": TRANSCRIPT_DIR, "glob": "*.jsonl",
                **CLAUDE_USAGE_DECLARATION}
        (cfg / "providers.yaml").write_text(yaml.safe_dump(providers))
        if executor == "docker":
            project = yaml.safe_load((cfg / "project.yaml").read_text())
            # Bridge networking: no egress proxy sidecar to fake.
            project["executor"] = {"kind": "docker", "docker": {"network": "bridge"}}
            (cfg / "project.yaml").write_text(yaml.safe_dump(project))
            self.docker_log = install_fake_docker(self.base, monkeypatch)
        else:
            self.docker_log = self.base / "docker.log"

    # --- where the session is --------------------------------------------

    def host_transcript_dir(self, worktree: Path) -> Path:
        """The declared location, expanded on the host (the local executor)."""
        return self.user_home / TRANSCRIPT_PREFIX / slug(worktree)

    def docker_executor(self):
        from multiagents.config import load as load_config
        from multiagents.executor.docker import DockerExecutor
        from multiagents.paths import ProjectPaths, global_config_dir
        from multiagents.providers import load_providers
        paths = ProjectPaths(self.root)
        config = load_config(paths, seed=False)
        return DockerExecutor(config.project.get("executor", {}).get("docker", {}),
                              paths, load_providers(config.providers), global_config_dir())

    # --- a finished node ---------------------------------------------------

    def finished_node(self, agent_id: str, session_id: str, *,
                      status: str = "done", reason: str = "finished") -> Node:
        """A node whose run ended: its own branch with one commit, a worktree
        on it, and a captured session id."""
        branch = f"agents/worker/{agent_id.removeprefix('ag-')}"
        worktree = self.paths.worktree(agent_id)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(self.root, "worktree", "add", "-q", str(worktree), "-b", branch, "main")
        (worktree / "work.txt").write_text(f"work of {agent_id}\n")
        git(worktree, "add", "work.txt")
        git(worktree, "commit", "-q", "-m", f"work of {agent_id}")
        node = Node(id=agent_id, agent="worker", provider="svstub", model="m",
                    parent=None, depth=1, task="earlier task", status=status,
                    reason=reason, branch=branch, worktree=str(worktree),
                    session_id=session_id, started_at=tree_now(),
                    ended_at=tree_now())
        self.tree.add(node)
        return node

    def commit_of(self, agent_id: str) -> str:
        node = self.node(agent_id)
        return git(self.root, "rev-parse", node.branch).stdout.strip()

    def remove_worktree(self, agent_id: str) -> None:
        node = self.node(agent_id)
        git(self.root, "worktree", "remove", "--force", node.worktree)

    def write_session(self, directory: Path, session_id: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{session_id}.jsonl"
        path.write_text(json.dumps({"type": "user", "sessionId": session_id}) + "\n")
        return path

    def resumed(self, session_id: str) -> list[dict]:
        """Stub invocations that resumed `session_id`."""
        return [inv for inv in self.invocations()
                if "--resume" in inv["argv"]
                and inv["argv"][inv["argv"].index("--resume") + 1] == session_id]


# ---------------------------------------------------------------------------
# A docker-executor project for the CLI's container commands
# ---------------------------------------------------------------------------

class CliProject:
    """A git project configured for the docker executor, with a fake docker,
    and a tree to seed. For `multiagents docker ...` and `run --no-launch`."""

    def __init__(self, tmp_path: Path, monkeypatch, *, drift: bool = False):
        self.base = tmp_path.resolve()
        self.user_home = self.base / "home"
        self.user_home.mkdir()
        monkeypatch.setenv("HOME", str(self.user_home))
        self.root = self.base / "proj"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".multiagents/\n")
        for args in (["init", "-q", "-b", "main"], ["add", ".gitignore"],
                     ["commit", "-q", "-m", "init"]):
            git(self.root, *args)
        cfg = self.root / ".multiagents" / "config"
        cfg.mkdir(parents=True)
        stub = self.base / "svstub"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        providers: dict[str, Any] = {n: {"enabled": False}
                                     for n in ("claude", "opencode", "agy")}
        providers["svstub"] = {"bin": str(stub), "family": "svstub",
                               "spawn": {"args": ["{prompt}"]},
                               "stream": {"format": "ndjson", "rules": []}}
        (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": providers}))
        (cfg / "project.yaml").write_text(yaml.safe_dump(
            {"team": "", "executor": {"kind": "docker", "docker": {"network": "bridge"}}}))
        (cfg / "agents.yaml").write_text(yaml.safe_dump({"agents": {"worker": {
            "provider": "svstub", "model": "m", "can_spawn": False,
            "description": "stub", "instructions": ""}}}))
        from multiagents.paths import ProjectPaths
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        self.docker_log = install_fake_docker(self.base, monkeypatch,
                                              mode="drift" if drift else "")
        self.container = f"multiagents-{self.paths.slug}"
        self.procs: list[subprocess.Popen] = []

    ACTIVE = ("running", "detached", "stuck", "pending")

    def seed(self, agent_id: str, status: str) -> None:
        """A node in `status`. An active one is genuinely alive: a real process
        holds its pid and its run dir has the wrapper's output file, so
        `run`'s reconciliation leaves it running (SV-R6) instead of writing it
        off as orphaned before anything asks about the container."""
        pid = None
        if status in self.ACTIVE:
            proc = subprocess.Popen(["sleep", "600"], start_new_session=True)
            self.procs.append(proc)
            pid = proc.pid
            run_dir = self.paths.run_dir(agent_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "output.ndjson").write_text("")
        self.tree.add(Node(id=agent_id, agent="worker", provider="svstub", model="m",
                           parent=None, depth=1, task="t", status=status,
                           reason="seeded", pid=pid, started_at=tree_now()))

    def close(self) -> None:
        for proc in self.procs:
            proc.kill()
            proc.wait()

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MULTIAGENTS_", "CLAUDE_"))}
        env.update({
            "PYTHONPATH": sv.SRC + (os.pathsep + env["PYTHONPATH"]
                                    if env.get("PYTHONPATH") else ""),
            "HOME": str(self.user_home),
            "MULTIAGENTS_PROJECT": str(self.root),
            "MULTIAGENTS_STATE_DIR": os.environ["MULTIAGENTS_STATE_DIR"],
            "MULTIAGENTS_CONFIG_DIR": os.environ["MULTIAGENTS_CONFIG_DIR"],
            **GIT_ID,
        })
        return env

    def cli(self, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "multiagents.cli", *args],
                              cwd=str(self.root), env=self.env(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
