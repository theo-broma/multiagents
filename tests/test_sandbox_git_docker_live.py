"""Sandbox and git, against a real docker daemon: SG-R1, SG-R2 (live half), SG-R3.

Contract: `context/specs/sandbox-git.md` and its Decisions sections. The
Decisions place these here: SG-R1 and SG-R3 for `commit_all` and
`restore_paths` need real docker, because the local executor still runs hooks
by design (CI-R5). SG-R2 is checked statically in `test_sandbox_git_mounts.py`,
and "plus opt-in real-docker checks", which is this file.

**Opt-in.** Every test skips unless `SG_TEST_DOCKER=1` is set, a `docker`
binary is on PATH, `docker info` succeeds and the workspace image
(`multiagents/workspace:latest`, from `multiagents docker build`) exists. Each
test creates its own project container (`multiagents-<slug>`, `--network
none`, so no proxy image is needed) and removes it afterwards.

Run on the host, from a checkout of this branch:

    SG_TEST_DOCKER=1 uv run --frozen python -m pytest tests/test_sandbox_git_docker_live.py -v

Black box. The agent is `sv_harness`'s stub, started through a real
`python -m multiagents.server` on the docker executor, so the run, the
end-of-run commit and `merge_agent` are multiagents' own. The oracle is a
recorder hook: it appends one line per invocation to
`.multiagents/sv/hooks.log` (runtime state, writable from both sides, and it
outlives the agent's worktree, which a merge may remove). Each line holds:

    tag, hostname, the container marker, /.dockerenv, toplevel, message

The container marker is `MULTIAGENTS_CONTAINER=<name>` read from
`/proc/1/environ`. Inside the project container, pid 1 runs as the agent's uid
and carries it from `docker run`. On the host, pid 1 is root's and unreadable.
A line counts as "in the container" only when its hostname is the container's
and it carries that container's marker. The host-side squash commit of
`merge_agent` also runs the user's hooks, legitimately (SG-R5), and its line
is the control showing the oracle tells host from container.

SG-R2 is driven with `docker exec --user <uid>:<gid>`, as the agent runs. A
refused write is judged on the host: the exit status must be non-zero AND the
host's bytes unchanged. Each attack has a control on a path the contract
leaves writable, so a broken exec (no shell, wrong uid) cannot pass for
protection.

Not covered here: SG-R2's "a nested child spawn and merge still succeed"
needs the MCP server and a spawning provider inside the image, which this
stub setup does not have. The CI-R5 fix-loop commits (SG-R3) are not driven
either. A hook that fails once and then passes would be needed, and the fix
loop needs an agent that answers a resume.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import sv_harness as h  # noqa: E402

IMAGE = "multiagents/workspace:latest"
SID = "sg-live-sess"
HOOK_LOG_NAME = "hooks.log"
TERMINAL = {"done", "failed", "cancelled", "timeout", "error", "merged"}
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def docker_ready() -> str:
    """Why these cannot run here, or "" when they can (after sv_harness)."""
    if os.environ.get("SG_TEST_DOCKER") != "1":
        return "docker cases are opt-in: set SG_TEST_DOCKER=1 with a working docker setup"
    if not shutil.which("docker"):
        return "no docker binary"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "docker daemon not reachable"
    if subprocess.run(["docker", "image", "inspect", IMAGE],
                      capture_output=True).returncode != 0:
        return f"image {IMAGE} not built: run `multiagents docker build`"
    return ""


DOCKER_SKIP = docker_ready()
pytestmark = pytest.mark.skipif(bool(DOCKER_SKIP), reason=DOCKER_SKIP or "docker")


# ---------------------------------------------------------------------------
# host-side helpers
# ---------------------------------------------------------------------------

def host_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env={**os.environ, **IDENT})
    if check and proc.returncode != 0:
        raise AssertionError(f"host git {args} failed: {proc.stderr}")
    return proc


def recorder(tag: str, log: Path) -> str:
    """A hook that records where it ran. `$1` is the message file for
    commit-msg and absent for pre-commit."""
    return (
        "#!/bin/sh\n"
        "host=$(cat /proc/sys/kernel/hostname 2>/dev/null)\n"
        "box=$(tr '\\0' '\\n' < /proc/1/environ 2>/dev/null"
        " | grep '^MULTIAGENTS_CONTAINER=' | head -n1)\n"
        "if [ -e /.dockerenv ]; then denv=dockerenv; else denv=-; fi\n"
        "msg=-\n"
        'if [ -n "$1" ] && [ -f "$1" ]; then msg=$(head -n1 "$1"); fi\n'
        f"printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' '{tag}' \"$host\" \"$box\" \"$denv\""
        f" \"$(pwd -P)\" \"$msg\" >> '{log}'\n"
        "exit 0\n"
    )


def install(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


class Live:
    """One docker project: the sv_harness project, its container, the oracle."""

    def __init__(self, tmp_path: Path, **project_kw):
        self.p = h.Project(tmp_path, executor="docker", **project_kw)
        self.root = self.p.root
        cfg = self.root / ".multiagents" / "config" / "project.yaml"
        project = yaml.safe_load(cfg.read_text()) or {}
        project["executor"] = {"kind": "docker", "docker": {"network": "none"}}
        cfg.write_text(yaml.safe_dump(project))
        self.container = f"multiagents-{self.p.paths.slug}"
        self.log = self.p.sv / HOOK_LOG_NAME
        self.uid = f"{os.getuid()}:{os.getgid()}"
        self.host_name = socket.gethostname()
        self._box_name = ""

    # --- container ---------------------------------------------------------

    def up(self) -> None:
        up = self.p.cli("docker", "up", timeout=600)
        assert up.returncode == 0, f"`multiagents docker up` failed:\n{up.stdout}{up.stderr}"

    def exec(self, script: str, *args: str, cwd: Path | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """`sh -c script sh args...` inside the container, as the agent's uid."""
        argv = ["docker", "exec", "--user", self.uid, "-e", "HOME=/tmp",
                "-w", str(cwd or self.root)]
        for k, v in {**IDENT, **(env or {})}.items():
            argv += ["-e", f"{k}={v}"]
        argv += [self.container, "sh", "-c", script, "sh", *args]
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)

    @property
    def box_name(self) -> str:
        if not self._box_name:
            got = subprocess.run(["docker", "exec", self.container, "cat",
                                  "/proc/sys/kernel/hostname"],
                                 capture_output=True, text=True, timeout=60)
            assert got.returncode == 0, got.stderr
            self._box_name = got.stdout.strip()
        return self._box_name

    def cleanup(self) -> None:
        try:
            self.p.cleanup()
        finally:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)

    # --- the oracle --------------------------------------------------------

    def records(self) -> list[dict]:
        if not self.log.is_file():
            return []
        out = []
        for line in self.log.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            tag, host, box, denv, top, msg = parts
            out.append({"tag": tag, "host": host, "box": box, "dockerenv": denv,
                        "toplevel": top, "msg": msg, "where": self.where(host, box)})
        return out

    def where(self, host: str, box: str) -> str:
        if host == self.box_name and box == f"MULTIAGENTS_CONTAINER={self.container}":
            return "container"
        if host == self.host_name and not box:
            return "host"
        return "unknown"

    # --- agents ------------------------------------------------------------

    def run_agent(self, steps: list) -> tuple[h.Server, str]:
        server = self.p.server()
        agent_id = server.start("sg live " + h.plan_token(steps))
        h.wait_until(lambda: self.p.status(agent_id) in TERMINAL, 180, interval=0.2)
        assert self.p.status(agent_id) == "done", (
            f"the agent did not finish: {self.p.describe(agent_id)}\n{server.stderr()}")
        return server, agent_id


def finishing(sid: str = SID) -> list:
    return [h.step_start(sid), h.text(sid, "done"), h.step_finish(sid, 10, 1, 0.0), ["exit", 0]]


@pytest.fixture
def live(tmp_path):
    made: list[Live] = []

    def build(**kw) -> Live:
        base = tmp_path / f"p{len(made)}"
        base.mkdir()
        lv = Live(base, **kw)
        made.append(lv)
        return lv

    yield build
    for lv in made:
        lv.cleanup()


def _seed(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    host_git(root, "add", "-A")
    host_git(root, "commit", "-q", "--no-verify", "-m", "seed")


def _worktree(lv: Live, agent_id: str) -> Path:
    node = lv.p.node(agent_id)
    assert node is not None and node.worktree, f"no worktree recorded for {agent_id}"
    return Path(node.worktree).resolve()


# ===========================================================================
# SG-R3 / SG-R1 — an agent's own commits, and their hooks, run in its sandbox
# ===========================================================================

def test_sg_r3_end_of_run_commit_hook_runs_inside_the_container(live):
    """The user's own `.git/hooks/pre-commit` and `commit-msg` still run on
    the end-of-run commit (CI-R5, no --no-verify), but inside the container."""
    lv = live()
    _seed(lv.root, {"src/app.py": "print('base')\n"})
    install(lv.root / ".git" / "hooks" / "pre-commit", recorder("pre-commit", lv.log))
    install(lv.root / ".git" / "hooks" / "commit-msg", recorder("commit-msg", lv.log))
    lv.up()

    _, agent_id = lv.run_agent([["write", "work.txt", "left behind\n"]] + finishing())
    node = lv.p.node(agent_id)
    assert host_git(lv.root, "show", f"{node.branch}:work.txt").stdout == "left behind\n", \
        "the agent's leftover work was not committed at the end of the run"

    wt = _worktree(lv, agent_id)
    mine = [r for r in lv.records() if r["toplevel"] == str(wt)]
    assert any(r["tag"] == "pre-commit" for r in mine), (
        f"the end-of-run commit ran no pre-commit hook (CI-R5 keeps hooks); "
        f"records={lv.records()}")
    for r in mine:
        assert r["where"] == "container", (
            f"the end-of-run commit's {r['tag']} hook ran outside the container "
            f"(hostname {r['host']!r}, marker {r['box']!r}; container is "
            f"{lv.box_name!r}, host is {lv.host_name!r})")


def test_sg_r3_restore_paths_commit_hook_runs_inside_the_container(live):
    """`merge_agent` on an agent that touched a read-only path reverts it with
    a commit on the agent's branch (`restore_paths`). That commit's hooks run
    in the container. The squash commit on the base that follows runs on the
    host, which is the control for the oracle."""
    lv = live(agent_overrides={"readonly_paths": ["protected.txt"]})
    _seed(lv.root, {"protected.txt": "original\n", "src/app.py": "print('base')\n"})
    install(lv.root / ".git" / "hooks" / "pre-commit", recorder("pre-commit", lv.log))
    install(lv.root / ".git" / "hooks" / "commit-msg", recorder("commit-msg", lv.log))
    lv.up()

    server, agent_id = lv.run_agent([["write", "protected.txt", "tampered\n"],
                                     ["write", "work.txt", "real work\n"]] + finishing())
    wt = _worktree(lv, agent_id)
    branch = lv.p.node(agent_id).branch
    end_of_run = host_git(lv.root, "log", "-1", "--format=%s", branch).stdout.strip()
    result = server.call("merge_agent", 120, agent_id=agent_id)
    assert (lv.root / "work.txt").is_file(), f"the agent's work was not merged: {result}"
    assert (lv.root / "protected.txt").read_text() == "original\n", \
        f"the read-only path was not reverted: {result}"

    records = lv.records()
    # The revert is the worktree commit made after the run: any commit-msg
    # record there other than the end-of-run commit's.
    reverts = [r for r in records if r["toplevel"] == str(wt) and r["tag"] == "commit-msg"
               and r["msg"] != end_of_run]
    assert reverts, (f"no commit-msg hook recorded restore_paths' revert commit in the "
                     f"agent's worktree; records={records}")
    for r in (r for r in records if r["toplevel"] == str(wt)):
        assert r["where"] == "container", (
            f"a commit on the agent's branch ran its {r['tag']} hook outside the "
            f"container ({r['msg']!r}, hostname {r['host']!r}, marker {r['box']!r})")

    on_base = [r for r in records if r["toplevel"] == str(lv.root.resolve())]
    assert on_base and all(r["where"] == "host" for r in on_base), (
        f"control: the host-side squash commit should have run the user's hooks on "
        f"the host, and been recognised as such; records={records}")


def test_sg_r1_an_agent_rewritten_in_tree_hook_never_runs_on_the_host(live):
    """`core.hooksPath=.husky` (in-tree hooks). The agent's branch rewrites
    `.husky/pre-commit`. Its version may run only in the sandbox: on the
    end-of-run commit (in the container, SG-R3) and never on the host, not on
    that commit and not on `merge_agent`'s squash commit (SG-R5 runs the
    base's version there)."""
    lv = live()
    install(lv.root / ".husky" / "pre-commit", recorder("base-husky", lv.log))
    host_git(lv.root, "config", "core.hooksPath", ".husky")
    _seed(lv.root, {"src/app.py": "print('base')\n"})
    lv.up()

    server, agent_id = lv.run_agent(
        [["write", ".husky/pre-commit", recorder("agent-husky", lv.log)],
         ["write", "work.txt", "real work\n"]] + finishing())
    result = server.call("merge_agent", 120, agent_id=agent_id)
    assert (lv.root / "work.txt").is_file(), f"the agent's work was not merged: {result}"

    records = lv.records()
    agent_runs = [r for r in records if r["tag"] == "agent-husky"]
    on_host = [r for r in agent_runs if r["where"] != "container"]
    assert not on_host, (f"the agent-written in-tree hook ran outside the container: "
                         f"{on_host}")
    assert agent_runs, (f"control: the end-of-run commit in the agent's worktree should "
                        f"run the branch's .husky/pre-commit, in the container; "
                        f"records={records}")


# ===========================================================================
# SG-R2 live — the container cannot change what the host trusts
# ===========================================================================

# (label, path relative to the root). Existing files are overwritten; the
# `x` entries are new names inside a protected directory.
PROTECTED = [
    ("main checkout file", "src/app.py"),
    ("new file in the main checkout", "sg-new.txt"),
    (".git/config", ".git/config"),
    (".git/hooks/x", ".git/hooks/x"),
    (".git/info/x", ".git/info/x"),
    (".git/HEAD", ".git/HEAD"),
    (".git/index", ".git/index"),
    (".git/refs/heads/<base>", ".git/refs/heads/main"),
    (".multiagents/config/x", ".multiagents/config/x"),
    # Decisions, "SG-R2, protected-path additions".
    (".git/config.worktree", ".git/config.worktree"),
    (".git/modules/x", ".git/modules/x"),
    (".git/refs/tags/x", ".git/refs/tags/x"),
    (".git/refs/heads/x (a non-agent branch)", ".git/refs/heads/x"),
]
PROTECTED_DIRS = [".git/hooks", ".git/info", ".multiagents/config"]


def _state(path: Path):
    """What the host sees at `path`: bytes, a directory listing, or None."""
    if path.is_dir():
        return sorted((p.name, p.read_bytes() if p.is_file() else None)
                      for p in path.iterdir())
    return path.read_bytes() if path.exists() else None


@pytest.fixture
def box(live):
    lv = live()
    _seed(lv.root, {"src/app.py": "print('base')\n"})
    lv.up()
    return lv


def test_sg_r2_live_writing_each_protected_path_fails(box):
    control = box.exec('printf ok > "$1"', str(box.p.sv / "sg-control"))
    assert control.returncode == 0 and (box.p.sv / "sg-control").read_text() == "ok", (
        f"control: the agent could not write its runtime state at all: {control.stderr}")
    head = host_git(box.root, "rev-parse", "main").stdout.strip()

    breached = []
    for label, rel in PROTECTED:
        target = box.root / rel
        before = _state(target)
        got = box.exec('printf "sg-evil\\n" >> "$1"', str(target))
        after = _state(target)
        if got.returncode == 0 or after != before:
            breached.append(f"{label}: exit {got.returncode}, "
                            f"{'changed on the host' if after != before else 'unchanged'}")
    assert not breached, "writable from the container:\n  " + "\n  ".join(breached)
    assert host_git(box.root, "rev-parse", "main").stdout.strip() == head


def test_sg_r2_live_replacing_each_protected_path_by_rename_fails(box):
    """The replacement is written in `.git/`, which stays writable (packed-refs
    is rewritten by lock-and-rename), then renamed over the target. A
    protected directory is also moved aside wholesale."""
    staging = box.root / ".git"
    breached = []
    for n, (label, rel) in enumerate(PROTECTED):
        target = box.root / rel
        tmp = staging / f"sg-rename-{n}"
        made = box.exec('printf "sg-evil\\n" > "$1"', str(tmp))
        assert made.returncode == 0, (
            f"control: `.git/` must stay writable from the container: {made.stderr}")
        before = _state(target)
        got = box.exec('mv -f "$1" "$2"', str(tmp), str(target))
        after = _state(target)
        if got.returncode == 0 or after != before:
            breached.append(f"{label}: exit {got.returncode}, "
                            f"{'replaced on the host' if after != before else 'unchanged'}")
    for rel in PROTECTED_DIRS:
        target = box.root / rel
        aside = target.with_name(target.name + ".sg-old")
        before = _state(target)
        got = box.exec('mv "$1" "$2" && mkdir "$1"', str(target), str(aside))
        if got.returncode == 0 or _state(target) != before or aside.exists():
            breached.append(f"{rel} moved aside: exit {got.returncode}")
    assert not breached, "replaceable from the container:\n  " + "\n  ".join(breached)


def test_sg_r2_live_git_cannot_move_the_base_or_the_main_head(box):
    """The same attacks through git itself: `update-ref` on the base,
    `symbolic-ref HEAD`, and a rewritten `packed-refs` naming another commit
    for the base (the base keeps a loose ref, which wins)."""
    head = host_git(box.root, "rev-parse", "main").stdout.strip()
    symbolic = host_git(box.root, "symbolic-ref", "HEAD").stdout.strip()
    evil = box.exec('git -C "$1" commit-tree -m evil "$(git -C "$1" rev-parse main^{tree})"',
                    str(box.root))
    assert evil.returncode == 0, f"control: objects must stay writable: {evil.stderr}"
    evil_sha = evil.stdout.strip()

    box.exec('git -C "$1" update-ref refs/heads/main "$2"', str(box.root), evil_sha)
    assert host_git(box.root, "rev-parse", "main").stdout.strip() == head, \
        "`git update-ref` from the container moved the base branch"
    box.exec('git -C "$1" symbolic-ref HEAD refs/heads/agents/sg-evil', str(box.root))
    assert host_git(box.root, "symbolic-ref", "HEAD").stdout.strip() == symbolic, \
        "the container moved the main checkout's HEAD"
    wrote = box.exec('printf "%s refs/heads/main\\n" "$2" >> "$1/.git/packed-refs"',
                     str(box.root), evil_sha)
    assert wrote.returncode == 0, f"control: packed-refs must stay writable: {wrote.stderr}"
    assert host_git(box.root, "rev-parse", "main").stdout.strip() == head, \
        "a rewritten packed-refs moved the base branch"


def test_sg_r2_live_worktree_and_branch_operations_still_succeed(live):
    """What worktrees and nested orchestration need: a worktree add, a commit
    in it, lock/unlock, prune, and creating and deleting `agents/...`
    branches, one of them packed so that deleting it rewrites packed-refs."""
    lv = live()
    _seed(lv.root, {"src/app.py": "print('base')\n"})
    host_git(lv.root, "branch", "agents/sg-packed", "main")
    host_git(lv.root, "pack-refs", "--all")
    assert "refs/heads/agents/sg-packed" in (lv.root / ".git" / "packed-refs").read_text()
    lv.up()
    head = host_git(lv.root, "rev-parse", "main").stdout.strip()
    wt = lv.p.paths.worktrees / "sg-live"
    root = str(lv.root)

    def ok(what: str, script: str, *args: str, cwd: Path | None = None) -> None:
        got = lv.exec(script, *args, cwd=cwd)
        assert got.returncode == 0, f"{what} failed in the container:\n{got.stdout}{got.stderr}"

    ok("git worktree add", 'git -C "$1" worktree add -q -b agents/sg-live "$2" main',
       root, str(wt))
    ok("a worktree commit", 'printf work > work.txt && git add work.txt && '
       'git commit -q -m "worktree commit"', cwd=wt)
    assert host_git(lv.root, "show", "agents/sg-live:work.txt").stdout == "work", \
        "the worktree commit did not reach the host's repository"
    ok("git worktree lock/unlock",
       'git -C "$1" worktree lock "$2" && git -C "$1" worktree unlock "$2"', root, str(wt))
    ok("git worktree prune", 'rm -rf "$2" && git -C "$1" worktree prune', root, str(wt))
    assert str(wt) not in host_git(lv.root, "worktree", "list", "--porcelain").stdout, \
        "the pruned worktree is still registered on the host"
    ok("deleting the worktree's branch", 'git -C "$1" branch -q -D agents/sg-live', root)
    ok("creating and deleting an agents/ branch",
       'git -C "$1" branch agents/sg-new main && git -C "$1" branch -q -D agents/sg-new', root)
    ok("deleting a packed agents/ branch", 'git -C "$1" branch -q -D agents/sg-packed', root)

    refs = host_git(lv.root, "for-each-ref", "--format=%(refname)", "refs/heads/agents").stdout
    assert not refs.strip(), f"agents/ branches survived their deletion: {refs}"
    assert "agents/sg-packed" not in (lv.root / ".git" / "packed-refs").read_text(), \
        "packed-refs was not rewritten"
    assert host_git(lv.root, "rev-parse", "main").stdout.strip() == head, \
        "the base branch moved"
    assert host_git(lv.root, "fsck", "--no-progress", "--connectivity-only").returncode == 0
