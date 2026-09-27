"""SG-R4 — the server's `git_status` tool reports a failed pinned read.

Contract: context/specs/sandbox-git.md, Decisions "after implementers ag-3c75c1
and ag-b65538":

- A `GitError` from a pinned read is reported in the tool's result, as an
  error field, and never raised as a tool failure. It is never reported as
  `dirty: false`.

And from "after the stopped adversarial reading ag-044d7b": a failed pinned
read is never "clean"; an index that is a FIFO or a symlink, or is over the
128 MiB copy cap, or that git cannot read, raises `GitError`.

`git_status` reads the project root (the main checkout), pinned. Each case
below spoils the root's `.git/index` the way the decisions name, and calls
the tool as the server's own tests do: the function itself. The registered
tool turns whatever the function raises into a tool failure, so a result
returned here is a result the caller gets, and an exception here is the
tool failure the contract rules out.

What is not asserted: the error's wording, and which other fields the result
carries next to it. Only that an error field is there, non-empty, and that
`dirty` is not `False`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c3_harness as h
from multiagents import budget as budget_mod
from multiagents import server

JOIN = 20.0


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    root = h.make_git_repo(tmp_path.resolve() / "proj")
    (root / "tracked").write_text("t\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
    for args in (["add", "tracked"], ["commit", "-qm", "tracked"]):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, env=env)
    # Ignored, as a project's `.multiagents` is.
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text(".multiagents/\n")
    (root / ".multiagents" / "config").mkdir(parents=True)
    (root / ".multiagents" / "config" / "project.yaml").write_text("team: ''\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    h.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.chdir(root)
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **k: {})
    server._reset()
    yield root
    server._reset()


def call_git_status() -> dict:
    """`git_status()` in a thread: a FIFO must not hang it."""
    box: dict = {}

    def body():
        try:
            box["result"] = server.git_status()
        except BaseException as exc:          # noqa: BLE001 — reported below
            box["error"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    thread.join(JOIN)
    assert not thread.is_alive(), "git_status hung"
    assert "error" not in box, \
        f"git_status raised, which the tool reports as a failure: {box['error']!r}"
    result = box["result"]
    assert isinstance(result, dict), result
    return result


def assert_reported_as_error(result: dict) -> None:
    assert result.get("error"), f"no error field in the result: {result}"
    assert isinstance(result["error"], str), result
    assert result.get("dirty") is not False, \
        f"an unreadable tree reported as dirty: false: {result}"


def index(root: Path) -> Path:
    return root / ".git" / "index"


# --- controls -------------------------------------------------------------------

def test_control_sg_r4_git_status_reports_a_clean_root(project):
    result = call_git_status()
    assert "error" not in result, result
    assert result["dirty"] is False


def test_control_sg_r4_git_status_reports_a_dirty_root(project):
    (project / "tracked").write_text("changed\n")
    result = call_git_status()
    assert "error" not in result, result
    assert result["dirty"] is True


# --- a pinned read that fails ---------------------------------------------------

def test_sg_r4_git_status_reports_a_fifo_index_as_an_error(project):
    path = index(project)
    path.unlink()
    os.mkfifo(path)
    try:
        assert_reported_as_error(call_git_status())
    finally:
        # Unblock anything stuck opening it for reading.
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass


def test_sg_r4_git_status_reports_a_symlinked_index_as_an_error(project, tmp_path):
    """The index is a link to a valid index elsewhere, so git itself would
    read it happily: the refusal is the pinned read's."""
    path = index(project)
    elsewhere = tmp_path / "outside-index"
    elsewhere.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(elsewhere)
    assert_reported_as_error(call_git_status())


def test_sg_r4_git_status_reports_a_corrupt_index_as_an_error(project):
    index(project).write_bytes(b"DIRC" + b"\xff" * 64)
    assert_reported_as_error(call_git_status())


def test_sg_r4_git_status_reports_an_index_over_the_cap_as_an_error(project):
    """Sparse: 128 MiB + 1 byte on paper, nothing on disk."""
    with open(index(project), "r+b") as fh:
        fh.truncate(128 * 1024 * 1024 + 1)
    assert_reported_as_error(call_git_status())


def test_sg_r4_git_status_recovers_once_the_index_is_readable_again(project, tmp_path):
    """Nothing about the failure is remembered: the next call, on a
    repaired index, is an ordinary answer."""
    path = index(project)
    good = path.read_bytes()
    path.unlink()
    path.symlink_to(tmp_path / "nowhere")
    assert_reported_as_error(call_git_status())
    path.unlink()
    path.write_bytes(good)
    result = call_git_status()
    assert "error" not in result, result
    assert result["dirty"] is False
