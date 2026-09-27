"""Sandbox and git — SG-R2, `protect_project` never writes through a symlink
nor blocks on a FIFO (`context/specs/sandbox-git.md`, Decisions "after the
stopped adversarial reading ag-044d7b").

`DockerExecutor.protect_project()` runs on the host before docker starts, in
a project the container could write while the old layout was in force. So an
agent may have left, at a path it is about to create, unpack or protect, a
symlink to something outside the project, or a FIFO. The protected paths:
`.git/hooks`, `.git/info`, `.git/modules`, `.git/config.worktree`,
`.git/index`, `.git/refs/heads/<base>` and `.multiagents/config`.

For each, and for each plant — a symlink to an outside file, to an outside
directory, to an outside path that does not exist yet, or a FIFO —
`protect_project`:

- refuses: it returns a non-empty error naming the path;
- returns promptly;
- leaves the outside target as it was (content, mtime, and for a dangling
  link, nonexistence).

A clean project is accepted, as a control. `protect_project` is called
directly, on the public executor; nothing else is asserted about how it
detects the plant.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c1_harness as h  # noqa: E402

BASE = "main"
PROTECTED = [".git/hooks", ".git/info", ".git/modules", ".git/config.worktree",
             ".git/index", f".git/refs/heads/{BASE}", ".multiagents/config"]
PLANTS = ["symlink-to-file", "symlink-to-dir", "symlink-dangling", "fifo"]
PROMPT = 15.0


def _git(root: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(root), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def project(tmp_path):
    """A main checkout on BASE with a commit and a loose base ref, every
    protected path present, and multiagents' config directory."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", BASE)
    (root / "a").write_text("a\n")
    _git(root, "add", "a")
    _git(root, "commit", "-q", "--no-verify", "-m", "init")
    git = root / ".git"
    (git / "modules").mkdir(exist_ok=True)
    (git / "config.worktree").touch()
    (root / ".multiagents" / "config").mkdir(parents=True)
    (root / ".multiagents" / "config" / "project.yaml").write_text("executor: docker\n")
    for rel in PROTECTED:
        assert (root / rel).exists() and not (root / rel).is_symlink(), rel
    return h.make_docker_executor(root, network="bridge", mount_cli_from_host=False)


def snapshot(path: Path):
    """What must not change about an outside target."""
    if not os.path.lexists(path):
        return ("absent",)
    st = os.lstat(path)
    if path.is_dir():
        entries = sorted((str(p.relative_to(path)), p.read_bytes() if p.is_file() else None)
                         for p in path.rglob("*"))
        return ("dir", st.st_mtime_ns, st.st_mode, entries)
    return ("file", st.st_mtime_ns, st.st_mode, path.read_bytes())


def plant(root: Path, rel: str, kind: str, outside: Path) -> Path | None:
    """Replace `root/rel` by the plant; the outside target, if any."""
    target = root / rel
    if target.is_dir() and not target.is_symlink():
        for p in sorted(target.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
        target.rmdir()
    else:
        target.unlink()
    outside.mkdir(exist_ok=True)
    if kind == "fifo":
        os.mkfifo(target)
        return None
    if kind == "symlink-to-file":
        dest = outside / "host-file"
        dest.write_text("the user's file\n")
    elif kind == "symlink-to-dir":
        dest = outside / "host-dir"
        dest.mkdir()
        (dest / "keep").write_text("the user's data\n")
    else:
        dest = outside / "not-yet"
    os.utime(outside, ns=(1_000_000_000, 1_000_000_000))
    if dest.exists():
        os.utime(dest, ns=(1_000_000_000, 1_000_000_000))
    target.symlink_to(dest)
    return dest


def unblock_fifo(path: Path) -> None:
    try:
        os.close(os.open(path, os.O_RDWR | os.O_NONBLOCK))
    except OSError:
        pass


def protect_promptly(ex, fifo: Path | None = None) -> dict:
    box: dict = {}

    def target():
        try:
            box["value"] = ex.protect_project()
        except BaseException as e:      # noqa: BLE001 — handed to the test
            box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(PROMPT)
    if thread.is_alive():
        if fifo is not None:
            unblock_fifo(fifo)
        thread.join(5)
        pytest.fail(f"protect_project did not return within {PROMPT}s")
    if fifo is not None:
        unblock_fifo(fifo)
    return box


def test_sg_r2_control_protect_project_accepts_a_clean_project(project):
    box = protect_promptly(project)
    assert box == {"value": ""}, box


@pytest.mark.parametrize("kind", PLANTS)
@pytest.mark.parametrize("rel", PROTECTED)
def test_sg_r2_protect_project_refuses_a_planted_protected_path(project, tmp_path, rel, kind):
    root = project.paths.root
    outside = tmp_path / "outside"
    dest = plant(root, rel, kind, outside)
    before = snapshot(dest) if dest is not None else None
    outside_before = snapshot(outside)

    box = protect_promptly(project, fifo=root / rel if kind == "fifo" else None)

    assert "error" not in box, f"protect_project raised {box['error']!r}"
    error = box["value"]
    assert error, f"protect_project accepted {rel} as a {kind}"
    assert rel in error, f"the refusal does not name {rel}: {error!r}"
    if dest is not None:
        assert snapshot(dest) == before, f"the outside target of {rel} was changed"
        assert snapshot(outside) == outside_before, (
            f"something was written next to the outside target of {rel}")
