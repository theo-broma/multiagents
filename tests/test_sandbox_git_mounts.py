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
PROTECTED_GIT = [".git/config", ".git/hooks", ".git/info", ".git/HEAD", ".git/index"]
PROTECTED = PROTECTED_GIT + [".multiagents/config"]


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
            self.at_run = {rel: (root / rel).exists() for rel in PROTECTED}
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
                                             ".multiagents/config/project.yaml"])
def test_sg_r2_protected_path_is_read_only_at_its_own_path(project, rel):
    root = project.paths.root
    assert (root / rel).exists() or rel.endswith(".sample"), rel
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
    ".git/refs", ".git/refs/heads/main",
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
    for rel in (".git/hooks", ".git/info", ".multiagents/config"):
        assert (root / rel).is_dir(), f"{rel} was created, but not as a directory"
    assert (root / ".git/index").is_file()


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


@pytest.mark.parametrize("rel", [".git/config", ".git/index", ".multiagents/config"])
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
