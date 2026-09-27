"""Sandbox and git — SG-R2, `.git/objects/info` is read-only from the container
(`context/specs/sandbox-git.md`, Decisions "after the stopped adversarial
reading ag-044d7b": "SG-R2 addition").

`objects/info/alternates` (and `http-alternates`) tells every git process
where else to look for objects. Written from the container, an entry pointing
to a FIFO would hang every host-side git read, and one pointing elsewhere
would feed it foreign objects. So `.git/objects/info` is read-only, while
`.git/objects` itself, where a worktree commit writes loose objects and packs,
stays writable.

Checked statically on `DockerExecutor.run_args()`, parsed and resolved exactly
as `tests/test_sandbox_git_mounts.py` does (the deepest mount whose
destination contains a path decides what the container sees there), with that
file's `project` fixture. Every mount is at its own host path.

"Missing paths" (SG-R2): a protected path that does not exist is created by
`protect_project` before the container starts, so it can be protected, and
it is listed among the mounts whether or not it exists yet.
"""

from __future__ import annotations

import shutil

import pytest

from test_sandbox_git_mounts import parse_mounts, project, seen_as  # noqa: F401


@pytest.mark.parametrize("rel", [
    ".git/objects/info",
    ".git/objects/info/alternates",
    ".git/objects/info/http-alternates",
])
def test_sg_r2_objects_info_is_read_only_at_its_own_path(project, rel):
    root = project.paths.root
    mounts = parse_mounts(project.run_args())
    mode, host = seen_as(mounts, root / rel)
    assert mode == "ro", f"{rel} is {mode} from the container (SG-R2)"
    assert host == root / rel, f"{rel} is backed by {host}, not the host's own path"


@pytest.mark.parametrize("rel", [
    ".git/objects",
    ".git/objects/pack",
    ".git/objects/pack/pack-0123.pack",
    ".git/objects/ab",
    ".git/objects/ab/cdef0123456789",
    ".git/objects/infox",            # a sibling whose name merely starts with "info"
])
def test_sg_r2_objects_outside_info_stay_writable(project, rel):
    root = project.paths.root
    mounts = parse_mounts(project.run_args())
    mode, host = seen_as(mounts, root / rel)
    assert mode == "rw", f"{rel} must stay writable (SG-R2), is {mode}"
    assert host == root / rel, f"{rel} is backed by {host}, not the host's own path"


def test_sg_r2_objects_info_is_protected_even_when_missing(project):
    root = project.paths.root
    info = root / ".git" / "objects" / "info"
    shutil.rmtree(info)
    mounts = parse_mounts(project.run_args())
    assert seen_as(mounts, info) == ("ro", info), (
        "a missing .git/objects/info dropped out of the protected mounts")


def test_sg_r2_protect_project_creates_missing_objects_info(project):
    root = project.paths.root
    info = root / ".git" / "objects" / "info"
    shutil.rmtree(info)
    assert project.protect_project() == ""
    assert info.is_dir() and not info.is_symlink(), (
        "protect_project did not create .git/objects/info as a directory")


def test_sg_r2_protect_project_keeps_an_existing_objects_info(project):
    root = project.paths.root
    packs = root / ".git" / "objects" / "info" / "packs"
    packs.write_text("P pack-0123.pack\n")
    assert project.protect_project() == ""
    assert packs.read_text() == "P pack-0123.pack\n"
