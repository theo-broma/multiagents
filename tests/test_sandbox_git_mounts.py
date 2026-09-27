"""Sandbox and git — SG-R2 (static part) and SG-R6
(`context/specs/sandbox-git.md`, contract of 2026-09-27 and its Decisions).

Black box over `DockerExecutor`'s public surface: `run_args()`,
`ensure_running()`, `start()` and `mount_drift()`. No docker daemon: the
docker CLI is faked at the subprocess boundary, as in
`test_phase0_versioned_mount.py`.

SG-R2 is checked statically, as the Decisions ask: the `-v` / `--volume` /
`--mount` flags `run_args()` emits are parsed, and what the container sees at
a path is decided the way docker decides it, by the deepest mount whose
destination contains the path. The opt-in real-docker half of SG-R2 (writing
and renaming protected paths from inside, a worktree commit, `worktree
add`/`prune`, a nested spawn and merge) is not in this file.

Each protected or writable path must also be seen in the container AT ITS
OWN HOST PATH (the mount's source, followed to the path, is the path itself):
the container has to see the real repository, and its writes have to reach
the host. That is the module's standing rule ("every bind mount here uses
<host path>:<same path>"), and a scratch copy mounted over `.git` would
satisfy the read-only/writable split while breaking everything else.

The last Decisions section ("after testers ag-2e9add … and ag-4c3dd4",
5b9e633) adds protected paths: `.git/config.worktree`, `.git/modules`,
`.git/refs/heads` and `.git/refs/tags` (with `.git/refs/heads/agents`, the
agent-branch namespace, writable inside them), and the base branch — the one
checked out in the main checkout — as a loose ref file, unpacked from
`packed-refs` by the host before the container starts, so that a rewritten
`packed-refs` cannot move it (a loose ref wins over a packed one). A project
whose `.git` is a file is refused with a clear message.

SG-R6 uses a fake daemon holding one running container created with a given
mount list. The old layout is today's: project root writable, the worktrees
and homes directories writable, `.multiagents/config` read-only.

Controls: `test_sg_r2_control_*` check the parser and the resolution model
against hand-written argv, including today's layout, which leaves every
protected path writable. `test_sg_r6_control_*` shows a container created by
the current `run_args()` is accepted. Controls pass today and must stay
green; everything else is red until SG-R2/SG-R6 are implemented.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import multiagents.executor.docker as docker_mod

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h  # noqa: E402

RECREATE = "multiagents docker rm && multiagents docker up"

# Relative to the project root.
PROTECTED_GIT = [".git/config", ".git/hooks", ".git/info", ".git/HEAD", ".git/index",
                 ".git/config.worktree", ".git/modules",
                 ".git/refs/heads", ".git/refs/tags"]
PROTECTED = PROTECTED_GIT + [".multiagents/config"]
# Protected paths a fresh repository does not have: created before the
# container starts (SG-R2 "Missing paths"), absent from the fixture.
NOT_IN_A_FRESH_REPO = {".git/config.worktree", ".git/modules"}
# The agent-branch namespace, writable inside the read-only `.git/refs/heads`.
AGENTS_NS = ".git/refs/heads/agents"


# ---------------------------------------------------------------------------
# parsing run_args() and resolving a path the way docker does
# ---------------------------------------------------------------------------

def parse_mounts(argv: list[str]) -> list[tuple[str | None, str, bool]]:
    """(source, destination, read_only) for every -v/--volume/--mount flag.
    A non-bind --mount (tmpfs, volume) has source None."""
    out = []
    i = 0
    while i < len(argv):
        token = str(argv[i])
        value = None
        kind = None
        for flag, k in (("-v", "v"), ("--volume", "v"), ("--mount", "m")):
            if token == flag:
                value, kind = str(argv[i + 1]), k
                i += 1
            elif token.startswith(flag + "="):
                value, kind = token[len(flag) + 1:], k
        i += 1
        if value is None:
            continue
        if kind == "v":
            parts = value.split(":")
            src, dst = parts[0], parts[1]
            opts = parts[2].split(",") if len(parts) > 2 else []
            out.append((src, dst, "ro" in opts or "readonly" in opts))
        else:
            fields = {}
            for item in value.split(","):
                key, _, val = item.partition("=")
                fields[key.strip()] = val.strip()
            src = fields.get("source") or fields.get("src")
            dst = fields.get("target") or fields.get("destination") or fields.get("dst")
            ro = any(k in fields and fields[k] in ("", "true", "1")
                     for k in ("readonly", "ro"))
            if fields.get("type", "volume") != "bind":
                src = None
            out.append((src, dst, ro))
    return out


def covering(mounts, path: Path):
    """The mount the container sees `path` through: the deepest destination
    equal to or containing it. None if nothing covers it."""
    best = None
    for src, dst, ro in mounts:
        d = Path(dst)
        if path == d or d in path.parents:
            if best is None or len(d.parts) > len(Path(best[1]).parts):
                best = (src, dst, ro)
    return best


def seen_as(mounts, path: Path) -> tuple[str, Path | None]:
    """('ro' | 'rw' | 'unmounted', host path the container sees there)."""
    m = covering(mounts, path)
    if m is None:
        return "unmounted", None
    src, dst, ro = m
    host = None if src is None else Path(src) / path.relative_to(dst)
    return ("ro" if ro else "rw"), host


# ---------------------------------------------------------------------------
# a project: a real git repository with a commit, and multiagents state
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True)


@pytest.fixture
def project(tmp_path, monkeypatch, tmp_path_factory):
    root = tmp_path / "proj"
    root.mkdir()
    assert _git(root, "init", "-q", "-b", "main").returncode == 0
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "BRIEF.md").write_text("brief\n")
    (root / ".husky").mkdir()
    (root / ".husky" / "pre-commit").write_text("#!/bin/sh\n")
    assert _git(root, "add", "-A").returncode == 0
    assert _git(root, "commit", "-q", "-m", "init").returncode == 0
    _git(root, "pack-refs", "--all")        # packed-refs exists
    (root / ".git" / "worktrees" / "ag-000001").mkdir(parents=True)

    data = root / ".multiagents"
    (data / "config").mkdir(parents=True)
    (data / "config" / "project.yaml").write_text("executor: docker\n")
    (data / "runs").mkdir()
    (data / "tree.json").write_text('{"nodes": {}}\n')
    (data / "events.jsonl").write_text("")

    ex = h.make_docker_executor(root, network="bridge", mount_cli_from_host=False)
    for d in (ex.paths.worktrees / "ag-000001", ex.paths.homes / "ag-000001"):
        d.mkdir(parents=True, exist_ok=True)

    # `docker_available()` needs a docker on PATH; every call it would get is
    # intercepted at subprocess.run anyway.
    dockerbin = tmp_path_factory.mktemp("dockerbin")
    (dockerbin / "docker").write_text("#!/bin/sh\nexit 0\n")
    (dockerbin / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{dockerbin}{os.pathsep}{os.environ['PATH']}")
    return ex


# ---------------------------------------------------------------------------
# a fake docker daemon
# ---------------------------------------------------------------------------

class FakeDocker:
    """A daemon holding at most one container, created with `created`
    ((source, destination, read_only) triples) or absent (None). Records
    every docker argv, and the protected paths' existence at `docker run`."""

    def __init__(self, ex, created):
        self.ex = ex
        self.mounts = None if created is None else [
            (str(s), str(d), not ro) for s, d, ro in created]
        self.calls: list[list[str]] = []
        self.at_run: dict[str, bool] = {}
        self.loose_refs: dict[str, str] = {}

    def run(self, argv, *a, **kw):
        argv = [str(x) for x in argv]
        self.calls.append(argv)
        out, rc = "", 0
        fmt = " ".join(argv)
        if argv[:2] == ["docker", "inspect"]:
            if self.mounts is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such object")
            if ".State.Status" in fmt:
                out = "running\n"
            elif ".Destination}}:{{.RW" in fmt:
                out = "".join(f"{d}:{'true' if rw else 'false'}\n" for _, d, rw in self.mounts)
            elif ".Source}}>{{.Destination" in fmt:
                out = "".join(f"{s}>{d}\n" for s, d, _ in self.mounts)
            elif ".State.StartedAt" in fmt:
                out = "2026-09-27T10:00:00.000000000Z\n"
        elif argv[:2] == ["docker", "run"]:
            root = self.ex.paths.root
            self.at_run = {rel: (root / rel).exists() for rel in PROTECTED + [AGENTS_NS]}
            refs = root / ".git" / "refs"
            self.loose_refs = {str(f.relative_to(root / ".git")): f.read_text().strip()
                               for f in refs.rglob("*") if f.is_file()}
            self.run_argv = argv
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def lifecycle_calls(self):
        return [c for c in self.calls
                if c[:2] in (["docker", "run"], ["docker", "rm"], ["docker", "create"])
                or c[:3] in (["docker", "container", "rm"], ["docker", "container", "create"])]


@pytest.fixture
def fake_docker(monkeypatch):
    real_run = subprocess.run

    def install(ex, created):
        fake = FakeDocker(ex, created)

        def run(argv, *a, **kw):
            if argv and str(argv[0]) == "docker":
                return fake.run(argv, *a, **kw)
            return real_run(argv, *a, **kw)

        monkeypatch.setattr(docker_mod.subprocess, "run", run)

        async def no_exec(*command, **kw):
            fake.calls.append([str(c) for c in command])
            raise AssertionError(f"an agent process was started: {command}")

        monkeypatch.setattr(docker_mod.asyncio, "create_subprocess_exec", no_exec)
        return fake

    return install


def _old_layout(ex) -> list[tuple[str, str, bool]]:
    """What a container created before SG-R2 has."""
    p = ex.paths
    return [(str(p.root), str(p.root), False),
            (str(p.worktrees), str(p.worktrees), False),
            (str(p.homes), str(p.homes), False),
            (str(p.config), str(p.config), True)]


# ===========================================================================
# controls — the harness itself (green now and after)
# ===========================================================================

def test_sg_r2_control_todays_layout_reads_as_every_protected_path_writable(tmp_path):
    root = tmp_path / "proj"
    argv = ["docker", "run", "-v", f"{root}:{root}",
            "-v", f"{root}/.multiagents/config:{root}/.multiagents/config:ro",
            "img", "sleep", "infinity"]
    mounts = parse_mounts(argv)
    for rel in PROTECTED_GIT + ["src/app.py"]:
        assert seen_as(mounts, root / rel) == ("rw", root / rel), rel
    assert seen_as(mounts, root / ".multiagents/config/project.yaml")[0] == "ro"
    assert seen_as(mounts, tmp_path / "elsewhere")[0] == "unmounted"


def test_sg_r2_control_parser_reads_mount_syntax_and_depth_not_order(tmp_path):
    root = tmp_path / "proj"
    argv = ["--mount", f"type=bind,source={root}/.git/config,target={root}/.git/config,readonly",
            "--mount", f"type=bind,src={root}/.git,dst={root}/.git",
            f"--volume={root}:{root}:ro",
            "--mount", f"type=tmpfs,destination={root}/.git/hooks"]
    mounts = parse_mounts(argv)
    assert seen_as(mounts, root / ".git/config") == ("ro", root / ".git/config")
    assert seen_as(mounts, root / ".git/objects") == ("rw", root / ".git/objects")
    assert seen_as(mounts, root / "BRIEF.md") == ("ro", root / "BRIEF.md")
    assert seen_as(mounts, root / ".git/hooks/pre-commit") == ("rw", None)


def test_sg_r2_control_run_args_parse_to_the_projects_own_paths(project):
    """The helpers read the real `run_args()`: the project root is covered,
    by a mount at its own host path, today and after SG-R2."""
    mounts = parse_mounts(project.run_args())
    mode, host = seen_as(mounts, project.paths.root / ".git/config")
    assert mode in ("ro", "rw") and host == project.paths.root / ".git/config"


# ===========================================================================
# SG-R2 — the container cannot change what the host trusts (static)
# ===========================================================================

@pytest.mark.parametrize("rel", PROTECTED + [".git/hooks/pre-commit.sample",
                                             ".git/info/exclude",
                                             ".git/modules/sub/config",
                                             ".git/refs/heads/main",
                                             ".git/refs/heads/feature/x",
                                             ".git/refs/tags/v1",
                                             ".multiagents/config/project.yaml"])
def test_sg_r2_protected_path_is_read_only_at_its_own_path(project, rel):
    """Paths below a protected directory need not exist: a branch or tag
    created later, or a submodule's config, is read-only all the same."""
    root = project.paths.root
    if rel in PROTECTED and rel not in NOT_IN_A_FRESH_REPO:
        assert (root / rel).exists(), rel
    mounts = parse_mounts(project.run_args())
    mode, host = seen_as(mounts, root / rel)
    assert mode == "ro", f"{rel} is {mode} from the container (SG-R2)"
    assert host == root / rel, f"{rel} is backed by {host}, not the host's own path"


def test_sg_r2_main_checkout_working_files_are_read_only(project):
    root = project.paths.root
    tracked = _git(root, "ls-files").stdout.split()
    assert "src/app.py" in tracked and ".husky/pre-commit" in tracked
    mounts = parse_mounts(project.run_args())
    # The root itself (a new file there), the tracked files, a new file in a
    # tracked directory, and an in-tree hooks directory (core.hooksPath=.husky).
    for path in [root, root / "NEW", root / "src" / "new.py", root / ".husky",
                 *[root / t for t in tracked]]:
        mode, host = seen_as(mounts, path)
        assert mode == "ro", f"{path} is {mode} from the container (SG-R2)"
        assert host == path


@pytest.mark.parametrize("rel", [
    ".git",                     # packed-refs and loose refs use lock-and-rename in it
    ".git/objects", ".git/objects/pack",
    ".git/refs", ".git/refs/remotes",
    ".git/refs/heads/agents", ".git/refs/heads/agents/tester/5e7a91",
    ".git/packed-refs", ".git/packed-refs.lock",
    ".git/logs",
    ".git/worktrees", ".git/worktrees/ag-000001",
    ".multiagents", ".multiagents/tree.json", ".multiagents/events.jsonl",
    ".multiagents/runs",
])
def test_sg_r2_repository_data_and_runtime_state_stay_writable(project, rel):
    root = project.paths.root
    mounts = parse_mounts(project.run_args())
    mode, host = seen_as(mounts, root / rel)
    assert mode == "rw", f"{rel} must stay writable (SG-R2), is {mode}"
    assert host == root / rel, f"{rel} is backed by {host}, not the host's own path"


def test_sg_r2_worktrees_and_homes_outside_the_root_stay_writable(project):
    mounts = parse_mounts(project.run_args())
    for path in (project.paths.worktrees / "ag-000001", project.paths.homes / "ag-000001"):
        assert seen_as(mounts, path) == ("rw", path), path


def test_sg_r2_protected_and_writable_together(project):
    """The split has to hold at once: an implementation that protects
    everything (root read-only, nothing reopened) or nothing fails here."""
    root = project.paths.root
    mounts = parse_mounts(project.run_args())
    assert seen_as(mounts, root / ".git/config")[0] == "ro"
    assert seen_as(mounts, root / ".git")[0] == "rw"
    assert seen_as(mounts, root / "BRIEF.md")[0] == "ro"
    assert seen_as(mounts, root / ".multiagents")[0] == "rw"
    assert seen_as(mounts, root / ".git/refs/heads/main")[0] == "ro"
    assert seen_as(mounts, root / ".git/refs/heads/agents/x/1")[0] == "rw"


def _strip_protected(ex) -> None:
    root = ex.paths.root
    for rel in (".git/hooks", ".git/info", ".multiagents/config"):
        subprocess.run(["rm", "-rf", str(root / rel)], check=True)
    (root / ".git" / "index").unlink()


def test_sg_r2_missing_protected_paths_exist_when_the_container_is_created(
        project, fake_docker):
    root = project.paths.root
    _strip_protected(project)
    fake = fake_docker(project, None)               # no container yet

    result = project.ensure_running()
    assert result.get("ok") is True, result
    assert fake.at_run, "no `docker run` was issued for an absent container"
    missing = [rel for rel, there in fake.at_run.items() if not there]
    assert missing == [], f"not created before the container started: {missing}"

    mounts = parse_mounts(fake.run_argv)
    for rel in PROTECTED:
        assert seen_as(mounts, root / rel) == ("ro", root / rel), rel
    for rel in (".git/hooks", ".git/info", ".git/modules", ".multiagents/config"):
        assert (root / rel).is_dir(), f"{rel} was created, but not as a directory"
    assert (root / ".git/index").is_file()
    assert (root / ".git/config.worktree").is_file()


def test_sg_r2_missing_protected_paths_are_mounted_by_run_args(project):
    """The static form of the same: whichever call creates them, the argv a
    container is created with protects them."""
    root = project.paths.root
    _strip_protected(project)
    mounts = parse_mounts(project.run_args())
    for rel in PROTECTED:
        assert seen_as(mounts, root / rel) == ("ro", root / rel), rel


def test_sg_r2_creating_a_missing_index_leaves_host_git_working(project, fake_docker):
    """Contract silence, flagged: SG-R2 says a missing path is created "as an
    empty directory or file". A zero-byte `.git/index` makes every host git
    command fail ("index file smaller than expected"), so what is created
    must leave the main checkout's `git status` as it was."""
    root = project.paths.root
    (root / ".git" / "index").unlink()
    before = _git(root, "status", "--porcelain")
    assert before.returncode == 0
    fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    assert (root / ".git/index").exists(), "the missing index was not created"
    after = _git(root, "status", "--porcelain")
    assert after.returncode == 0, f"host git broken: {after.stderr}"
    assert after.stdout == before.stdout


# ---------------------------------------------------------------------------
# SG-R2 — refs: the base branch cannot be moved from the container
# ---------------------------------------------------------------------------

def test_sg_r2_agent_branch_namespace_exists_when_the_container_is_created(
        project, fake_docker):
    """`.git/refs/heads` is read-only, so an agent branch can only be created
    if `refs/heads/agents` already exists on the host, as its own writable
    mount. The fixture's refs are all packed: nothing is there beforehand."""
    root = project.paths.root
    assert not (root / AGENTS_NS).exists()
    fake = fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    assert fake.at_run.get(AGENTS_NS) is True, "refs/heads/agents missing at `docker run`"
    assert (root / AGENTS_NS).is_dir()
    mounts = parse_mounts(fake.run_argv)
    assert seen_as(mounts, root / AGENTS_NS / "tester" / "1") == (
        "rw", root / AGENTS_NS / "tester" / "1")


def _check_out(root: Path, branch: str) -> str:
    """Make `branch` the main checkout's branch, with its ref only in
    packed-refs. Returns its sha."""
    if branch != "main":
        assert _git(root, "checkout", "-q", "-b", branch).returncode == 0
    assert _git(root, "pack-refs", "--all").returncode == 0
    assert not (root / ".git" / "refs" / "heads" / branch).exists()
    sha = _git(root, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    assert len(sha) == 40
    return sha


@pytest.mark.parametrize("base", ["main", "trunk", "feature/x"])
def test_sg_r2_base_branch_is_unpacked_before_the_container_starts(
        project, fake_docker, base):
    """The base is the branch checked out in the main checkout, whatever its
    name. At `docker run` it has a loose ref file holding its sha, and that
    file is read-only from the container."""
    root = project.paths.root
    sha = _check_out(root, base)
    fake = fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    assert fake.loose_refs.get(f"refs/heads/{base}") == sha, (
        f"no loose ref for the base {base} at `docker run`: {fake.loose_refs}")
    mounts = parse_mounts(fake.run_argv)
    ref = root / ".git" / "refs" / "heads" / base
    assert seen_as(mounts, ref) == ("ro", ref)


@pytest.mark.parametrize("base", ["main", "feature/x"])
def test_sg_r2_unpacking_the_base_leaves_host_git_unchanged(project, fake_docker, base):
    """A guard, green today (nothing unpacks yet): the unpacking step changes
    no ref's value, the checked-out branch or `git status`."""
    root = project.paths.root
    sha = _check_out(root, base)
    before = {
        "status": _git(root, "status", "--porcelain").stdout,
        "branches": _git(root, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
        "head": _git(root, "symbolic-ref", "HEAD").stdout,
    }
    fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    after = {
        "status": _git(root, "status", "--porcelain").stdout,
        "branches": _git(root, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
        "head": _git(root, "symbolic-ref", "HEAD").stdout,
    }
    assert after == before
    assert _git(root, "rev-parse", base).stdout.strip() == sha


def test_sg_r2_rewriting_packed_refs_cannot_move_the_unpacked_base(project, fake_docker):
    """What the loose ref is for: `packed-refs` stays writable, and an agent
    that rewrites the base's line in it must not move the base, because the
    loose file (read-only from the container) wins."""
    root = project.paths.root
    sha = _check_out(root, "main")
    fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    # The agent's side: a commit object of its own (written with plumbing, so
    # no ref moves), and packed-refs pointing main at it.
    tree = _git(root, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
    agent_sha = _git(root, "commit-tree", tree, "-p", sha, "-m", "agent").stdout.strip()
    assert len(agent_sha) == 40
    packed = root / ".git" / "packed-refs"
    packed.write_text(packed.read_text().replace(f"{sha} refs/heads/main",
                                                 f"{agent_sha} refs/heads/main"))
    assert f"{agent_sha} refs/heads/main" in packed.read_text()
    assert _git(root, "rev-parse", "refs/heads/main").stdout.strip() == sha


def test_sg_r2_an_already_loose_base_is_left_as_it_is(project, fake_docker):
    """A guard, green today: unpacking must not rewrite a loose base."""
    root = project.paths.root
    assert _git(root, "commit", "-q", "--allow-empty", "-m", "loose").returncode == 0
    ref = root / ".git" / "refs" / "heads" / "main"
    assert ref.is_file()
    sha = ref.read_text().strip()
    fake = fake_docker(project, None)
    assert project.ensure_running().get("ok") is True
    assert fake.loose_refs.get("refs/heads/main") == sha
    assert ref.read_text().strip() == sha


# ---------------------------------------------------------------------------
# SG-R2 — a project whose `.git` is a file is refused
# ---------------------------------------------------------------------------

@pytest.fixture
def gitfile_project(tmp_path, monkeypatch, tmp_path_factory):
    """A project that is itself a linked worktree: its `.git` is a file."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    assert _git(upstream, "init", "-q", "-b", "main").returncode == 0
    (upstream / "BRIEF.md").write_text("brief\n")
    assert _git(upstream, "add", "-A").returncode == 0
    assert _git(upstream, "commit", "-q", "-m", "init").returncode == 0
    root = tmp_path / "proj"
    assert _git(upstream, "worktree", "add", "-q", "-b", "proj", str(root)).returncode == 0
    assert (root / ".git").is_file()
    data = root / ".multiagents"
    (data / "config").mkdir(parents=True)
    (data / "config" / "project.yaml").write_text("executor: docker\n")
    ex = h.make_docker_executor(root, network="bridge", mount_cli_from_host=False)
    dockerbin = tmp_path_factory.mktemp("dockerbin")
    (dockerbin / "docker").write_text("#!/bin/sh\nexit 0\n")
    (dockerbin / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{dockerbin}{os.pathsep}{os.environ['PATH']}")
    return ex


def test_sg_r2_project_whose_git_is_a_file_is_refused(gitfile_project, fake_docker):
    """Out of scope for SG-R2's mounts, so the executor refuses to start
    rather than start unprotected. The message names `.git` and says what is
    wrong with it; no container is created."""
    root = gitfile_project.paths.root
    gitfile = (root / ".git").read_text()
    fake = fake_docker(gitfile_project, None)
    result = gitfile_project.ensure_running()
    assert result.get("ok") is False, f"a project with a .git file was started: {result}"
    error = result.get("error", "")
    assert ".git" in error and "file" in error.lower(), (
        f"the refusal does not say that .git is a file: {error!r}")
    assert fake.lifecycle_calls() == [], "a container was created anyway"
    assert (root / ".git").read_text() == gitfile, "the .git file was changed"


# ===========================================================================
# SG-R6 — existing containers are brought in line safely
# ===========================================================================

def _current_layout(ex) -> list[tuple[str, str, bool]]:
    return parse_mounts(ex.run_args())


def _assert_refused(result: dict, fake) -> None:
    assert result.get("ok") is False, f"an old-layout container was accepted: {result}"
    assert RECREATE in result.get("error", ""), result
    assert fake.lifecycle_calls() == [], (
        f"the container was replaced without being asked: {fake.lifecycle_calls()}")


def test_sg_r6_control_container_from_current_run_args_is_accepted(project, fake_docker):
    fake = fake_docker(project, _current_layout(project))
    result = project.ensure_running()
    assert result.get("ok") is True, result
    assert fake.lifecycle_calls() == []


def test_sg_r6_old_layout_container_is_refused_by_ensure_running(project, fake_docker):
    fake = fake_docker(project, _old_layout(project))
    _assert_refused(project.ensure_running(), fake)


def test_sg_r6_old_layout_container_refuses_delegation(project, fake_docker):
    fake = fake_docker(project, _old_layout(project))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(project.start(["fakecli", "-p", "task"], project.paths.worktrees / "ag-000001",
                                  {"MULTIAGENTS_AGENT_ID": "ag-000001"}))
    assert RECREATE in str(exc.value), str(exc.value)
    assert fake.lifecycle_calls() == []
    assert not any(c[:2] == ["docker", "exec"] for c in fake.calls), (
        "an agent was started in the old-layout container")


def test_sg_r6_old_layout_container_is_reported_as_drift(project, fake_docker):
    fake_docker(project, _old_layout(project))
    assert project.mount_drift() != [], (
        "the mount-drift check does not see an old-layout container")


@pytest.mark.parametrize("rel", [".git/config", ".git/index", ".multiagents/config",
                                 ".git/refs/heads", ".git/modules"])
def test_sg_r6_a_container_missing_one_protection_is_refused(project, fake_docker, rel):
    """Not only the whole old layout: a container whose mounts leave any one
    protected path writable is refused as well."""
    root = project.paths.root
    current = _current_layout(project)
    guard = covering(current, root / rel)
    assert guard is not None
    weakened = [(s, d, False) if (s, d, ro) == guard else (s, d, ro)
                for s, d, ro in current]
    fake = fake_docker(project, weakened)
    _assert_refused(project.ensure_running(), fake)


def test_sg_r6_refusal_is_repeatable_and_never_recreates(project, fake_docker):
    fake = fake_docker(project, _old_layout(project))
    for _ in range(3):
        _assert_refused(project.ensure_running(), fake)
