"""SG-R3's seam, without a docker daemon: `gitops.Git` and `ContainerGit`.

Contract: `context/specs/sandbox-git.md` (SG-R3 and the Decisions "after
implementer ag-9e10d8"). `test_sandbox_git_docker_live.py` proves the hooks
run in a real container; this file checks, where there is no daemon, that the
docker executor's agent commits go through `docker exec` as the agent runs
(its uid, its worktree, its environment) and that CI-R1 to CI-R7 hold across
that boundary: the identity fallback, the hook named from a trace written
where the host reads it, and no hang on an agent-planted FIFO.

`docker` is faked at the binary boundary, as `test_subagent_mcp.py` does: it
logs its argv and the env file it was handed, then runs the command on this
machine with only that environment.

Also here: the runner's `git_unreadable` rule — a `GitError` from a pinned
read never crashes it and never merges.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents import gitops  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.docker import ContainerGit, DockerExecutor  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.tree import Node  # noqa: E402

import c3_harness as c3  # noqa: E402

FAKE_DOCKER = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
if argv[:1] != ["exec"]:
    sys.exit(0)
rest = argv[1:]
workdir, env_file, user = None, None, None
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if flag in ("--workdir", "-w"):
        workdir = rest.pop(0)
    elif flag == "--env-file":
        env_file = rest.pop(0)
    elif flag in ("--user", "-u"):
        user = rest.pop(0)
env = {{}}
text = open(env_file).read() if env_file else ""
for line in text.splitlines():
    k, _, v = line.partition("=")
    env[k] = v
with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"container": rest[0], "command": rest[1:], "workdir": workdir,
                         "user": user, "env": env}}) + "\n")
os.chdir(workdir)
env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
os.execve("/bin/sh", ["sh", *rest[2:]], env)
'''

IDENTITY_VARS = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


class Box:
    """A project on the docker executor, an agent worktree, and fake docker."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "project"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "base.txt").write_text("base\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.paths = ProjectPaths(self.root)
        self.paths.ensure()
        self.agent_id = "ag-5eed01"
        self.worktree = tmp_path / "wt" / self.agent_id
        git(self.root, "worktree", "add", "-q", "-b", "agents/worker/5eed01",
            str(self.worktree))

        self.log = tmp_path / "docker.log"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable, log=str(self.log)))
        docker.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        for name in IDENTITY_VARS:
            monkeypatch.delenv(name, raising=False)

        # The agent's own HOME: no git identity anywhere in it.
        self.home = tmp_path / "agent-home"
        self.home.mkdir()
        self.executor = DockerExecutor({}, paths=self.paths)
        monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
        self.write_env()

    def write_env(self, **extra: str) -> None:
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"],
               "MULTIAGENTS_AGENT_ID": self.agent_id, "AGENT_MARKER": "from-start",
               "GIT_CONFIG_NOSYSTEM": "1", **extra}
        target = self.executor.env_file(self.agent_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    def execs(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def hook(self, name: str, body: str) -> None:
        path = self.root / ".git" / "hooks" / name
        path.write_text(body)
        path.chmod(0o755)


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def test_sg_r3_executors_hand_out_where_agent_commits_run(box):
    assert LocalExecutor().git("ag-x") is gitops.HOST
    sandbox = box.executor.git(box.agent_id)
    assert isinstance(sandbox, ContainerGit)
    assert DockerExecutor({}, paths=None).git("ag-x") is gitops.HOST


def test_sg_r3_commit_all_runs_every_git_command_through_docker_exec_as_the_agent(box):
    (box.worktree / "work.txt").write_text("left behind\n")
    result = gitops.commit_all(box.worktree, "wip", role="worker",
                               agent_id=box.agent_id, git=box.executor.git(box.agent_id))
    assert result.ok, result
    assert git(box.worktree, "show", "HEAD:work.txt") == "left behind"

    execs = box.execs()
    assert execs, "no git command went through docker exec"
    for call in execs:
        assert call["container"] == box.executor.container
        assert call["user"] == f"{os.getuid()}:{os.getgid()}"
        assert call["workdir"] == str(box.worktree)
        assert call["env"].get("AGENT_MARKER") == "from-start", \
            "git did not run with the environment the agent was started with"
        assert "git" in call["command"] and str(box.worktree) in call["command"]
    commit = [c for c in execs if "commit" in c["command"]]
    assert commit and "commit.gpgsign=false" in commit[0]["command"], "CI-R6 lost"

    # CI-R1: the agent's HOME has no identity, so the fallback names the agent.
    assert git(box.worktree, "log", "-1", "--format=%an <%ae>") == \
        f"multiagents worker <{box.agent_id}@multiagents.invalid>"


def test_sg_r3_the_agent_environment_decides_the_email_fallback(box):
    """CI-R1's EMAIL exception is judged on the environment git runs with —
    the agent's — not on this process's."""
    box.write_env(EMAIL="agent@own.invalid")
    (box.worktree / "work.txt").write_text("x\n")
    result = gitops.commit_all(box.worktree, "wip", role="worker",
                               agent_id=box.agent_id, git=box.executor.git(box.agent_id))
    assert result.ok, result
    assert git(box.worktree, "log", "-1", "--format=%ae") == "agent@own.invalid"


def test_sg_r3_a_refusing_hook_is_named_from_a_trace_the_host_reads(box):
    box.hook("pre-commit", "#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    box.hook("commit-msg", "#!/bin/sh\nexit 0\n")
    (box.worktree / "work.txt").write_text("x\n")
    result = gitops.commit_all(box.worktree, "wip", role="worker",
                               agent_id=box.agent_id, git=box.executor.git(box.agent_id))
    assert not result.ok
    assert result.hook == "pre-commit"
    assert "lint says no" in result.err
    commit = [c for c in box.execs() if "commit" in c["command"]][-1]
    trace = Path(commit["env"]["GIT_TRACE2_EVENT"])
    assert box.paths.run_dir(box.agent_id) in trace.parents, \
        "the trace must be on the shared run dir, at the same path on both sides"
    assert not trace.parent.exists(), "the trace's scratch dir was not cleaned up"


def test_sg_r3_a_hook_that_plants_a_fifo_does_not_hang_a_sandboxed_commit(box):
    """CI-R7 across the boundary: the hook replaces the trace with a FIFO."""
    box.hook("pre-commit",
             '#!/bin/sh\nrm -f "$GIT_TRACE2_EVENT"; mkfifo "$GIT_TRACE2_EVENT"\nexit 1\n')
    (box.worktree / "work.txt").write_text("x\n")
    got: dict = {}
    thread = threading.Thread(target=lambda: got.update(r=gitops.commit_all(
        box.worktree, "wip", git=box.executor.git(box.agent_id))), daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive(), "commit_all hung on a FIFO trace"
    assert not got["r"].ok and got["r"].hook == "pre-commit"


def test_sg_r3_an_env_file_replaced_by_a_fifo_does_not_hang_the_host(box):
    env_file = box.executor.env_file(box.agent_id)
    env_file.unlink()
    os.mkfifo(env_file)
    got: dict = {}
    thread = threading.Thread(
        target=lambda: got.update(env=box.executor.git(box.agent_id).environ()),
        daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "reading the agent's env file hung on a FIFO"
    assert "AGENT_MARKER" not in got["env"]


def test_sg_r3_restore_paths_commits_through_docker_exec(box):
    (box.worktree / "base.txt").write_text("tampered\n")
    git(box.worktree, "commit", "-q", "-am", "tamper")
    result = gitops.restore_paths(box.worktree, "main", ["base.txt"], "revert",
                                  git=box.executor.git(box.agent_id))
    assert result.ok, result
    assert git(box.worktree, "show", "HEAD:base.txt") == "base"
    assert any("commit" in c["command"] for c in box.execs()), \
        "restore_paths' commit did not go through docker exec"


# ---------------------------------------------------------------------------
# runner: a GitError from a pinned read never crashes it, never merges
# ---------------------------------------------------------------------------

def _events(runner, kind: str) -> list[dict]:
    path = runner.paths.events_file
    lines = path.read_text().splitlines() if path.is_file() else []
    return [e for e in map(json.loads, lines) if e.get("kind") == kind]


def test_sg_r4_an_unreadable_repository_blocks_the_merge(tmp_path, monkeypatch):
    spec = AgentSpec(name="worker", provider="p", model="m",
                     readonly_paths=["tests/**"])
    runner = c3.make_runner(tmp_path / "proj", monkeypatch, agents={"worker": spec})
    root = runner.paths.root
    git(root, "branch", "agents/worker/abc", "HEAD")
    runner.tree.add(Node(id="ag-abc", agent="worker", provider="p", model="m",
                         parent=None, depth=1, status="done",
                         branch="agents/worker/abc"))

    def unreadable(*args, **kwargs):
        raise gitops.GitError(f"no git directory for {root}")

    monkeypatch.setattr(gitops, "changed_paths", unreadable)
    merged = []
    monkeypatch.setattr(gitops, "merge", lambda *a, **k: merged.append(a) or ("merged", ""))

    result = runner.merge_agent("ag-abc")
    assert result["result"] == "blocked", result
    assert not merged, "a branch whose protected-file check could not run was merged"
    events = _events(runner, "git_unreadable")
    assert events and events[-1]["path"] == str(root)

    payload = runner.collect("ag-abc")
    assert payload.get("git_unreadable"), payload
