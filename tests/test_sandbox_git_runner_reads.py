"""SG-R4 / SG-R1 in the runner: the status poll and the conversation refresh.

Contract: context/specs/sandbox-git.md, SG-R1 and SG-R4, and the Decisions
"after implementer ag-ce8b54 on SG-R3":

- SG-R4 applies to every host-side git command that touches an agent
  worktree: ``runner._worktree_state`` (the ``git status`` polled while an
  agent runs), and every git call in ``_refresh_conversation`` —
  ``head_sha``, ``merge-base``, ``untracked_in_the_way``, ``symbolic-ref``
  and ``reset --keep``;
- reads are pinned (answers come from the real repository, nothing the agent
  wrote executes); a command that WRITES the worktree (``reset --keep``) runs
  inside the agent's executor, or pinned with filters and hooks disabled —
  the implementer's choice, but no agent config executes on the host.

Entry point: ``Runner.consult`` — the smallest public one that reaches both.
A conversational agent's first turn cuts its worktree; between turns the test
plays the agent and plants a vector; the second turn refreshes the worktree
(``_refresh_conversation``), and the agent's tool event makes the runner poll
the worktree's state (``_worktree_state``).

The agent runs on the docker executor, because the contract covers it (the
local executor's agents are trusted by construction, and its end-of-run
commit runs hooks on the host by design). ``docker`` is faked at the binary
boundary, as ``test_sandbox_git_commit_seam.py`` does: ``docker exec`` runs
the command on this machine, with the environment of the ``--env-file`` it
was handed plus ``SG_WHERE=container``. Every marker appends
``<where> <argv>`` to a log in ``tmp_path``, ``where`` being ``$SG_WHERE`` or
``host``. So a marker line starting ``container`` ran where the contract
allows it (the agent's own sandbox — for example ``reset --keep`` moved into
the executor), and one starting ``host`` is the SG-R1 violation.

Only the container's liveness is stubbed: ``DockerExecutor.inside`` is False,
``ensure_running`` reports the container up, and the image exists — what
``test_phase0_versioned_mount.py`` does for ``start()``.

Vectors (planted between the two turns, from the agent's side):

hook      a ``post-index-change`` hook in the repository's hooks dir;
fsmonitor ``core.fsmonitor`` in ``.git/worktrees/<id>/config.worktree``
          (``extensions.worktreeConfig`` on in the repository config);
gitfile   the worktree's ``.git`` file pointing to an agent-written git dir
          whose config sets ``core.fsmonitor`` and a filter driver, whose
          hooks include ``post-index-change``, and whose HEAD/refs disagree
          with the real repository;
commondir ``.git/worktrees/<id>/commondir`` pointing to that git dir;
filter    a filter driver (``clean``/``smudge`` = marker) in
          ``config.worktree``, applied to every path through a
          ``core.attributesFile`` the agent wrote.

Each vector has a plain-git control (``test_control_*``): the same git
commands the runner issues, run plainly in the worktree, fire the marker or
change the answer. Those pass today; they exist so an SG test cannot be green
because its vector was inert.

Not observable through the entry point, and so not asserted: the value of
the status poll's hash (it only feeds the doom-loop detector). For the poll,
only "nothing executes on the host" is tested.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as c3  # noqa: E402
from multiagents import gitops  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.docker import DockerExecutor  # noqa: E402

IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
SESSION = "s-sg-runner"

FAKE_DOCKER = r'''#!{python}
import os, sys
argv = sys.argv[1:]
if argv[:1] != ["exec"]:
    sys.exit(0)
rest = argv[1:]
workdir, env_file = None, None
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if flag in ("--workdir", "-w"):
        workdir = rest.pop(0)
    elif flag == "--env-file":
        env_file = rest.pop(0)
    elif flag in ("--user", "-u", "--env", "-e"):
        rest.pop(0)
env = {{}}
if env_file:
    for line in open(env_file).read().splitlines():
        k, _, v = line.partition("=")
        env[k] = v
env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
env["SG_WHERE"] = "container"
if workdir:
    os.chdir(workdir)
command = rest[1:]
os.execvpe(command[0], command, env)
'''

# The agent: never runs git (so no marker can be its doing), records its
# prompt, reports one tool call when the prompt says TOOL (which makes the
# runner poll the worktree), then answers.
FAKE_CLI = r'''#!{python}
import json, os, sys, time
from pathlib import Path
probe = Path({probe!r})
prompt = sys.argv[1]
(probe / f"turn-{{time.time_ns()}}.json").write_text(json.dumps(
    {{"prompt": prompt, "cwd": os.getcwd()}}))
if "TOOL" in prompt:
    print(json.dumps({{"type": "tool", "name": "Edit", "input": {{"path": "a"}},
                       "session": {session!r}}}))
    sys.stdout.flush()
    time.sleep(0.2)
print(json.dumps({{"type": "text", "text": "answered", "session": {session!r}}}))
sys.stdout.flush()
'''


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, env={**os.environ, **IDENT})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """No developer global/system git config; no ambient sandbox tag."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SG_WHERE", raising=False)
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


@dataclass
class Project:
    runner: object
    root: Path
    tmp: Path
    probe: Path
    log: Path
    marker: Path
    base: str

    # -- driving ------------------------------------------------------------

    def consult(self, message: str = "go") -> dict:
        return asyncio.run(self.runner.consult("advisor", message, timeout=60))

    def node(self):
        ids = [i for i, n in self.runner.tree.read()["nodes"].items()
               if n.get("agent") == "advisor"]
        assert len(ids) == 1, ids
        return self.runner.tree.get(ids[0])

    @property
    def wt(self) -> Path:
        return Path(self.node().worktree)

    @property
    def meta(self) -> Path:
        return self.root / ".git" / "worktrees" / self.wt.name

    def prompts(self) -> list[str]:
        turns = sorted(self.probe.glob("turn-*.json"))
        return [json.loads(p.read_text())["prompt"] for p in turns]

    def events(self, kind: str) -> list[dict]:
        path = self.runner.paths.events_file
        lines = path.read_text().splitlines() if path.is_file() else []
        return [e for e in map(json.loads, lines) if e.get("kind") == kind]

    def branch_sha(self) -> str:
        """The agent branch's tip in the REAL repository."""
        return git(self.root, "rev-parse", f"refs/heads/{self.node().branch}")

    def advance_base(self) -> str:
        (self.root / "c").write_text("c from base\n")
        git(self.root, "add", "-f", "c")
        git(self.root, "commit", "-q", "--no-verify", "-m", "base moves")
        return git(self.root, "rev-parse", "HEAD")

    # -- the marker log -----------------------------------------------------

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def on_host(self) -> list[str]:
        return [line for line in self.lines() if not line.startswith("container ")]

    def reset_log(self) -> None:
        self.log.unlink(missing_ok=True)


@pytest.fixture
def proj(tmp_path, monkeypatch) -> Project:
    """A project on the docker executor (fake docker), whose conversational
    agent has had one turn: its worktree exists, on base, clean."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER.format(python=sys.executable))
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(DockerExecutor, "inside", lambda self: False)
    monkeypatch.setattr(DockerExecutor, "ensure_running",
                        lambda self: {"ok": True, "container": self.container,
                                      "existed": True})
    monkeypatch.setattr(DockerExecutor, "image_exists", lambda self, name: True)

    probe = tmp_path / "probe"
    probe.mkdir()
    cli = tmp_path / "fake-agent"
    cli.write_text(FAKE_CLI.format(python=sys.executable, probe=str(probe),
                                   session=SESSION))
    cli.chmod(0o755)
    provider = {
        "bin": str(cli),
        "spawn": {"args": ["{prompt}"], "resume": ["--resume", "{session_id}"]},
        "stream": {"format": "ndjson", "session_id_paths": ["session"],
                   "rules": [
                       {"match": {"type": "tool"}, "as": "tool",
                        "fields": {"name": "name", "args": "input"}},
                       {"match": {"type": "text"}, "as": "text",
                        "fields": {"text": "text"}}]},
    }
    spec = AgentSpec("advisor", "fake", "m", conversational=True, timeout=60,
                     executor="docker")

    root = tmp_path / "proj"
    root.mkdir()
    c3.make_git_repo(root)
    (root / "a").write_text("a\n")
    (root / "b").write_text("b\n")
    # `c` is ignored until base starts tracking it: the refresh then has to
    # ask whether the agent's ignored copy is in the way (hash-object).
    (root / ".gitignore").write_text(".multiagents/\n__pycache__/\nc\n")
    git(root, "add", "a", "b", ".gitignore")
    git(root, "commit", "-q", "--no-verify", "-m", "base v1")
    base = git(root, "symbolic-ref", "--short", "HEAD")
    runner = c3.make_runner(root, monkeypatch, agents={"advisor": spec},
                            providers={"fake": provider}, git=False)

    log = tmp_path / "fired.log"
    marker = tmp_path / "marker.sh"
    marker.write_text(f'#!/bin/sh\necho "${{SG_WHERE:-host}} $0 $*" >> "{log}"\n'
                      f"exit 1\n")
    marker.chmod(0o755)

    p = Project(runner=runner, root=root, tmp=tmp_path, probe=probe, log=log,
                marker=marker, base=base)
    first = p.consult("first turn")
    assert first.get("error") is None, f"fixture: first turn failed: {first}"
    assert p.wt.is_dir() and p.meta.is_dir(), "fixture: worktree and its metadata"
    assert git(p.wt, "status", "--porcelain") == "", "fixture: worktree starts clean"
    assert p.branch_sha() == git(root, "rev-parse", "HEAD")
    p.reset_log()
    return p


# --- vectors -----------------------------------------------------------------

def fake_gitdir(p: Project) -> Path:
    """An agent-written git dir: a clone whose agent branch is three commits
    ahead of anything real (touching `b`), with fsmonitor, a filter driver
    applied to every path, and a post-index-change hook — all the marker."""
    top = p.tmp / "agent-written"
    subprocess.run(["git", "clone", "-q", str(p.root), str(top)], check=True,
                   capture_output=True)
    git(top, "checkout", "-q", "-b", p.node().branch)
    for n in range(3):
        (top / "b").write_text(f"fake {n}\n")
        git(top, "add", "b")
        git(top, "commit", "-q", "--no-verify", "-m", f"fake {n}")
    fake = top / ".git"
    git(top, "config", "core.fsmonitor", str(p.marker))
    git(top, "config", "filter.evil.clean", str(p.marker))
    git(top, "config", "filter.evil.smudge", str(p.marker))
    (fake / "info").mkdir(exist_ok=True)
    (fake / "info" / "attributes").write_text("* filter=evil\n")
    hook = fake / "hooks" / "post-index-change"
    hook.write_text(p.marker.read_text())
    hook.chmod(0o755)
    return fake


def vec_hook(p: Project) -> None:
    hook = p.root / ".git" / "hooks" / "post-index-change"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(p.marker.read_text())
    hook.chmod(0o755)


def vec_fsmonitor(p: Project) -> None:
    git(p.root, "config", "extensions.worktreeConfig", "true")
    (p.meta / "config.worktree").write_text(f"[core]\n\tfsmonitor = {p.marker}\n")


def vec_gitfile(p: Project) -> None:
    (p.wt / ".git").write_text(f"gitdir: {fake_gitdir(p)}\n")


def vec_commondir(p: Project) -> None:
    (p.meta / "commondir").write_text(f"{fake_gitdir(p)}\n")


def vec_filter(p: Project) -> None:
    attributes = p.tmp / "agent-attributes"
    attributes.write_text("* filter=evil\n")
    git(p.root, "config", "extensions.worktreeConfig", "true")
    (p.meta / "config.worktree").write_text(
        f"[core]\n\tattributesFile = {attributes}\n"
        f"[filter \"evil\"]\n\tclean = {p.marker}\n\tsmudge = {p.marker}\n")


def vec_hollow(p: Project) -> None:
    """`commondir` pointing to an agent-written git dir that holds the agent
    branch's ref, at the REAL sha, and no objects. HEAD therefore still
    names the right commit, and every other answer — ancestry, a diff,
    a reset — is the hollow dir's, not the repository's."""
    top = p.tmp / "hollow"
    subprocess.run(["git", "init", "-q", str(top)], check=True, capture_output=True)
    ref = top / ".git" / "refs" / "heads" / p.node().branch
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(p.branch_sha() + "\n")
    (p.meta / "commondir").write_text(f"{top / '.git'}\n")


VECTORS = {"hook": vec_hook, "fsmonitor": vec_fsmonitor, "gitfile": vec_gitfile,
           "commondir": vec_commondir, "filter": vec_filter}
REDIRECTS = {"gitfile": vec_gitfile, "commondir": vec_commondir}
ANSWER_VECTORS = {**VECTORS, "hollow": vec_hollow}


def touch(p: Project, name: str = "a") -> None:
    """Stat-dirty a tracked file, content unchanged: git then refreshes (and
    writes) the index, which is what fires post-index-change and asks
    fsmonitor and the clean filter."""
    path = p.wt / name
    path.write_text(path.read_text())


def plain(p: Project, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(p.wt), *args], capture_output=True,
                          text=True, env={**os.environ, **IDENT})


# --- controls: each vector is live under plain git -----------------------------

@pytest.mark.parametrize("name", sorted(VECTORS))
def test_control_sg_r1_vector_fires_under_the_plain_status_poll(proj, name):
    """What `_worktree_state` runs, plainly: `git status --porcelain`."""
    VECTORS[name](proj)
    touch(proj)
    plain(proj, "status", "--porcelain")
    assert proj.on_host(), f"vector {name} is inert under plain git status"


@pytest.mark.parametrize("name", sorted(VECTORS))
def test_control_sg_r1_vector_fires_under_the_plain_refresh_commands(proj, name):
    """What the refresh runs once base moved, plainly: `rev-parse HEAD`,
    `hash-object` of the ignored copy of a path base now tracks,
    `symbolic-ref`, `reset --keep`."""
    new_base = proj.advance_base()
    (proj.wt / "c").write_text("c from base\n")
    VECTORS[name](proj)
    touch(proj)
    plain(proj, "rev-parse", "HEAD")
    plain(proj, "hash-object", "--", "c")
    plain(proj, "symbolic-ref", "-q", "HEAD")
    plain(proj, "reset", "--keep", new_base)
    assert proj.on_host(), f"vector {name} is inert under the plain refresh commands"


@pytest.mark.parametrize("name", sorted(REDIRECTS))
def test_control_sg_r4_redirect_changes_plain_answers(proj, name):
    real_head = proj.branch_sha()
    REDIRECTS[name](proj)
    assert plain(proj, "rev-parse", "HEAD").stdout.strip() != real_head


def test_control_sg_r4_hollow_commondir_keeps_head_but_breaks_the_rest(proj):
    real_head = proj.branch_sha()
    new_base = proj.advance_base()
    vec_hollow(proj)
    assert plain(proj, "rev-parse", "HEAD").stdout.strip() == real_head
    assert plain(proj, "symbolic-ref", "-q", "HEAD").stdout.strip() == \
        f"refs/heads/{proj.node().branch}"
    assert plain(proj, "merge-base", "--is-ancestor", real_head, new_base).returncode != 0
    assert plain(proj, "diff", "--raw", real_head, new_base).returncode != 0


# --- SG-R4 / SG-R1: the status poll ---------------------------------------------

@pytest.mark.parametrize("name", sorted(VECTORS))
def test_sg_r4_status_poll_executes_nothing_on_the_host(proj, name):
    """Base has not moved, so the refresh has nothing to do; the agent's tool
    event makes the runner poll the worktree's state (`_worktree_state`).
    Nothing the agent wrote may run on the host for it. (The refresh tests
    below send no tool event, so they do not reach the poll.)"""
    VECTORS[name](proj)
    touch(proj)
    result = proj.consult("TOOL second turn")
    assert result.get("error") is None, result
    assert len(proj.prompts()) == 2, "the second turn never ran"
    assert proj.on_host() == [], (
        f"vector {name} executed on the host during a turn: {proj.on_host()}")


# --- SG-R4 / SG-R1: the refresh moves the worktree, executing nothing -----------

@pytest.mark.parametrize("name", sorted(VECTORS))
def test_sg_r1_refresh_to_a_moved_base_executes_nothing_on_the_host(proj, name):
    """Base moved and tracks `c`, which the worktree holds as an ignored file
    with the same content: every git call in the refresh runs — head_sha,
    untracked_in_the_way (with its hash-object), symbolic-ref, reset --keep.
    None of them may run the agent's hook, fsmonitor or filter on the host."""
    new_base = proj.advance_base()
    (proj.wt / "c").write_text("c from base\n")
    VECTORS[name](proj)
    touch(proj)
    proj.consult("second turn")
    assert len(proj.prompts()) == 2, "the second turn never ran"
    assert proj.on_host() == [], (
        f"vector {name} executed on the host during the refresh: {proj.on_host()}")
    assert new_base  # (the answer is the next test's business)


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_refresh_answers_from_the_real_repository(proj, name):
    """The agent's worktree is clean and behind base, on its own branch in the
    REAL repository: it is moved to base, and the turn is told so. What an
    agent-written git dir says about HEAD, the branch or the index does not
    decide it."""
    new_base = proj.advance_base()
    (proj.wt / "c").write_text("c from base\n")
    ANSWER_VECTORS[name](proj)
    result = proj.consult("second turn")
    assert result.get("error") is None, result
    assert proj.branch_sha() == new_base, (
        f"vector {name}: the agent branch was not moved to base in the real "
        f"repository")
    assert result["commit"] == git(proj.root, "rev-parse", "--short", new_base) \
        or new_base.startswith(result["commit"] or "-"), result
    assert result["behind"] == 0, result
    assert "was updated from" in proj.prompts()[-1], proj.prompts()[-1]
    assert proj.events("worktree_refreshed"), "no worktree_refreshed event"
    assert not proj.events("worktree_refresh_failed"), \
        proj.events("worktree_refresh_failed")
    assert not proj.events("git_unreadable"), proj.events("git_unreadable")


@pytest.mark.parametrize("name", sorted(REDIRECTS))
def test_sg_r4_refresh_with_base_unmoved_reads_the_real_head(proj, name):
    """Base has not moved and the real worktree is on it: the turn runs where
    it is, told nothing, and its view is the real HEAD — not the agent-written
    git dir's branch, three commits elsewhere."""
    real_head = proj.branch_sha()
    REDIRECTS[name](proj)
    result = proj.consult("second turn")
    assert result.get("error") is None, result
    assert real_head.startswith(result["commit"] or "-"), result
    assert result["behind"] == 0, result
    assert proj.prompts()[-1] == "second turn", proj.prompts()[-1]
    assert not proj.events("worktree_refresh_failed")
    assert not proj.events("git_unreadable")
    assert proj.on_host() == []


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_refresh_keeps_own_commits_ahead_of_base(proj, name):
    """The agent committed on its branch and base has not moved: ahead of
    base with work of its own, which is current, not stale (`merge-base
    --is-ancestor` of where it was placed). No notice, not moved, nothing
    executed on the host."""
    (proj.wt / "own.txt").write_text("own work\n")
    git(proj.wt, "add", "own.txt")
    git(proj.wt, "commit", "-q", "--no-verify", "-m", "own work")
    own = proj.branch_sha()
    ANSWER_VECTORS[name](proj)
    touch(proj)
    result = proj.consult("second turn")
    assert result.get("error") is None, result
    assert proj.branch_sha() == own, "own commits were moved"
    assert own.startswith(result["commit"] or "-"), result
    assert proj.prompts()[-1] == "second turn", proj.prompts()[-1]
    assert not proj.events("worktree_refresh_failed")
    assert not proj.events("git_unreadable")
    assert proj.on_host() == [], proj.on_host()


def test_sg_r4_refresh_does_not_follow_a_redirect_to_leave_the_branch(proj):
    """The agent-written git dir's HEAD is detached. The real worktree is on
    its branch, clean and behind base: it is moved, because `symbolic-ref`
    is asked of the real repository."""
    new_base = proj.advance_base()
    fake = fake_gitdir(proj)
    (fake / "HEAD").write_text(git(proj.root, "rev-parse", "HEAD~1") + "\n")
    (proj.wt / ".git").write_text(f"gitdir: {fake}\n")
    result = proj.consult("second turn")
    assert result.get("error") is None, result
    assert proj.branch_sha() == new_base
    assert proj.events("worktree_refreshed")
    assert proj.on_host() == []
