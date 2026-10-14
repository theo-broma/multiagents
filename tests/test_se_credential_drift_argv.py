"""SE3 (F63): credential_drift never puts a path inside a command string.

Contract: context/specs/findings-c1-c2-part2.md, Part 2b, group SE.

A fake `docker` executable first on PATH records every argv and plays the
container: `inspect` answers "running", `exec` runs the command on this host,
with each host path that has a container view replaced by that view. The
container keeps an old inode by holding a hard link to the file the host has
since replaced by rename, which is exactly how drift arises. No real Docker
daemon is reached.

The fake replaces a path wherever it occurs in an argv element, so today's
`sh -c` script also sees the container view: the result tests are about what
is reported, which SE3 keeps, and pass on main for plain paths.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from multiagents.executor.docker import DockerExecutor
from multiagents.paths import ProjectPaths
from multiagents.providers import Provider


FAKE_DOCKER = """#!{python}
import json, os, re, subprocess, sys
state = os.environ["SE3_FAKE_DOCKER_DIR"]
argv = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as log:
    log.write(json.dumps(argv) + "\\n")
with open(os.path.join(state, "config.json")) as handle:
    config = json.load(handle)
if argv[:1] == ["inspect"]:
    print(config.get("status", "running"))
    sys.exit(0)
if argv[:1] != ["exec"]:
    sys.stderr.write("fake docker: unsupported " + repr(argv) + "\\n")
    sys.exit(1)
rest, valued = argv[1:], {{"-u", "--user", "-e", "--env", "-w", "--workdir", "--env-file"}}
while rest and rest[0].startswith("-"):
    rest = rest[2:] if rest[0] in valued else rest[1:]
command = rest[1:]
for fragment in config.get("fail_on", []):
    if any(fragment in element for element in command):
        sys.stderr.write("fake docker: exec failed\\n")
        sys.exit(1)
views = config.get("map", {{}})
if views:
    pattern = re.compile("|".join(re.escape(k) for k in sorted(views, key=len, reverse=True)))
    command = [pattern.sub(lambda m: views[m.group(0)], element) for element in command]
done = subprocess.run(command, cwd=os.path.join(state, "cwd"))
sys.exit(done.returncode)
"""


class Container:
    """The fake docker's state, and the files it shows inside the container."""

    def __init__(self, root: Path):
        self.root = root
        self.views = root / "views"
        self.views.mkdir(parents=True)
        (root / "cwd").mkdir()
        self.map: dict[str, str] = {}
        self.fail_on: list[str] = []
        self.status = "running"
        self._n = 0
        self.save()

    def save(self):
        (self.root / "config.json").write_text(json.dumps(
            {"map": self.map, "fail_on": self.fail_on, "status": self.status}))

    def _view(self) -> Path:
        self._n += 1
        return self.views / f"v{self._n}"

    def keep_old_inode(self, path: Path) -> str:
        """The container holds the current file; the host then replaces it by
        rename, so the host path gets a new inode. Returns the old inode."""
        view = self._view()
        os.link(path, view)
        replacement = path.with_name(path.name + ".new")
        replacement.write_text("{\"refreshed\": true}")
        os.replace(replacement, path)
        self.map[str(path)] = str(view)
        self.save()
        return str(view.stat().st_ino)

    def missing(self, path: Path):
        self.map[str(path)] = str(self._view())
        self.save()

    def symlink_to_host_file(self, path: Path) -> str:
        """In the container the path is a symlink whose target is the very
        file the host has; returns the link's own inode."""
        view = self._view()
        view.symlink_to(path)
        self.map[str(path)] = str(view)
        self.save()
        return str(os.lstat(view).st_ino)

    def fail_exec_mentioning(self, fragment: str):
        self.fail_on.append(fragment)
        self.save()

    def calls(self) -> list[list[str]]:
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def execs(self) -> list[list[str]]:
        return [argv for argv in self.calls() if argv[:1] == ["exec"]]

    def shell_side_effects(self) -> list[str]:
        return sorted(os.listdir(self.root / "cwd"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(tmp_path / "state"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER.format(python=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    container = Container(tmp_path / "fake-docker")
    monkeypatch.setenv("SE3_FAKE_DOCKER_DIR", str(container.root))
    return home, container, tmp_path / "project"


def executor_for(project: Path, home: Path, links: list[str]) -> tuple[DockerExecutor, list[Path]]:
    paths = []
    for relative in links:
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        paths.append(path)
    project.mkdir(exist_ok=True)
    provider = Provider.from_dict("claude", {"bin": "claude", "home_links": links})
    executor = DockerExecutor({"image": "i"}, ProjectPaths(project), {"claude": provider}, project)
    return executor, paths


def drift(path: Path, container_inode: str) -> dict:
    return {"path": str(path), "host_inode": str(path.stat().st_ino),
            "container_inode": container_inode}


# Each a directory under the home, so the path is still a file path there.
HOSTILE = [
    "with space/cred.json",
    "semi;colon/cred.json",
    "$(touch PWNED)/cred.json",
    "`touch PWNED2`/cred.json",
    "quote'single/cred.json",
    'quote"double/cred.json',
    "-rf/cred.json",
    "-/cred.json",
]


# ---------------------------------------------------------------------------
# SE3 — paths reach the container only as whole argv elements
# ---------------------------------------------------------------------------

def test_se3_every_path_is_one_argv_element_and_in_no_command_string(world):
    home, container, project = world
    executor, paths = executor_for(project, home, HOSTILE)
    for path in paths:
        container.keep_old_inode(path)
    executor.credential_drift()

    execs = container.execs()
    assert execs, "credential_drift ran no docker exec"
    for path in map(str, paths):
        assert any(path in argv for argv in execs), (
            f"{path!r} never reached docker exec as one whole argument: {execs}")
        for argv in execs:
            for element in argv:
                assert element == path or path not in element, (
                    f"{path!r} sits inside the command string {element!r}")
    assert container.shell_side_effects() == [], "a shell parsed a path"


def test_se3_hostile_paths_report_drift_correctly_and_in_path_order(world):
    home, container, project = world
    executor, paths = executor_for(project, home, HOSTILE)
    expected = []
    for index, path in enumerate(paths):
        if index == 2:
            container.missing(path)       # not drift
        elif index == 5:
            pass                          # unchanged: not drift
        else:
            expected.append((path, container.keep_old_inode(path)))
    assert executor.credential_drift() == [drift(p, inode) for p, inode in expected]
    assert container.shell_side_effects() == []


# ---------------------------------------------------------------------------
# SE3 — what is reported is as today (green on main)
# ---------------------------------------------------------------------------

def test_se3_existing_missing_and_changed_files_report_as_today(world):
    home, container, project = world
    executor, (unchanged, missing, changed) = executor_for(
        project, home, [".a/one.json", ".b/two.json", ".c/three.json"])
    container.missing(missing)
    old = container.keep_old_inode(changed)
    assert executor.credential_drift() == [drift(changed, old)]


def test_se3_results_follow_path_order(world):
    home, container, project = world
    executor, paths = executor_for(
        project, home, [".z/last.json", ".a/first.json", ".m/middle.json"])
    olds = [container.keep_old_inode(path) for path in paths]
    assert executor.credential_drift() == [drift(p, o) for p, o in zip(paths, olds)]


def test_se3_a_symlink_in_the_container_is_not_followed(world):
    """`stat` without `-L`: the link's own inode is compared, so a symlink to
    the very file the host has still counts as drift."""
    home, container, project = world
    executor, (path,) = executor_for(project, home, [".claude/.credentials.json"])
    link_inode = container.symlink_to_host_file(path)
    assert link_inode != str(path.stat().st_ino)
    assert executor.credential_drift() == [drift(path, link_inode)]


def test_se3_nothing_is_reported_when_every_inode_matches(world):
    home, container, project = world
    executor, _ = executor_for(project, home, [".a/one.json", ".b/two.json"])
    assert executor.credential_drift() == []


def test_se3_a_container_not_running_reports_nothing(world):
    home, container, project = world
    executor, (path,) = executor_for(project, home, [".a/one.json"])
    container.keep_old_inode(path)
    container.status = "exited"
    container.save()
    assert executor.credential_drift() == []
    assert container.execs() == []


# ---------------------------------------------------------------------------
# SE3 — failures are as today
# ---------------------------------------------------------------------------

def test_se3_a_nonzero_exec_on_the_second_of_three_paths_gives_nothing(world):
    """All three have drifted, so a result keeping the first one read before
    the failure would be non-empty."""
    home, container, project = world
    executor, paths = executor_for(
        project, home, [".a/one.json", ".b/two.json", ".c/three.json"])
    for path in paths:
        container.keep_old_inode(path)
    container.fail_exec_mentioning(str(paths[1]))
    assert executor.credential_drift() == []


def test_se3_a_nonzero_exec_on_a_hostile_path_gives_nothing(world):
    home, container, project = world
    executor, paths = executor_for(project, home, HOSTILE[:3])
    for path in paths:
        container.keep_old_inode(path)
    container.fail_exec_mentioning(str(paths[1]))
    assert executor.credential_drift() == []
    assert container.shell_side_effects() == []


def test_se3_docker_missing_propagates(world, monkeypatch, tmp_path):
    home, container, project = world
    executor, (path,) = executor_for(project, home, [".a/one.json"])
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(FileNotFoundError):
        executor.credential_drift()


def _failing_exec(monkeypatch, error):
    """docker answers `inspect`, and its `exec` raises `error`. The one seam
    that is not a process: a timeout cannot be produced by a child without
    waiting the real timeout out."""
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if list(argv[:2]) == ["docker", "exec"]:
            raise error
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("error", [
    pytest.param(FileNotFoundError(2, "No such file or directory", "docker"), id="docker-missing"),
    pytest.param(subprocess.TimeoutExpired(["docker", "exec"], 120), id="timeout"),
])
def test_se3_an_exec_that_cannot_run_propagates(world, monkeypatch, error):
    home, container, project = world
    executor, (path,) = executor_for(project, home, [".a/one.json"])
    container.keep_old_inode(path)
    _failing_exec(monkeypatch, error)
    with pytest.raises(type(error)):
        executor.credential_drift()
