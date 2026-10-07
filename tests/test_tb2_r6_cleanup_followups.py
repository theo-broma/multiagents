"""TB2-R6 guards for tracked cleanup and internal Git diffs."""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tb2_r6_alias_cleanup import (  # noqa: E402
    GITIGNORE, TRACKED, UNTRACKED, approve, archive_holds, cleaner,
    cleanup_started, commit_entry, dirtying, finding, finish, start,
    verdict_entry, w, worker_two_rounds,
)
from multiagents import gitops  # noqa: E402
from multiagents.scheduler import sessions  # noqa: E402
from multiagents.scheduler.model import Refused  # noqa: E402


def test_tb2_r6_reverting_tracked_dirt_allows_the_waiting_node_to_launch(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(tracked="# " + TRACKED + "\n"),
                cleaner(write={".gitignore": GITIGNORE}), approve())
    top, _, rv = start(w)
    finish(w, top, rv)
    prompt = w.fxr.calls()[1]["prompt"]
    assert TRACKED in prompt and UNTRACKED not in prompt
    assert archive_holds(w, TRACKED)
    assert len(w.fxr.calls()) == 3


def test_tb2_r6_large_tracked_diff_has_a_bounded_cleanup_message(w):
    worker_two_rounds(w)
    w.fxr.queue(dirtying(tracked="# " + "large tracked edit\n" * 20_000),
                cleaner(write={".gitignore": GITIGNORE}), approve())
    top, _, rv = start(w)
    finish(w, top, rv)
    prompt = w.fxr.calls()[1]["prompt"]
    assert "large tracked edit" in prompt
    assert len(prompt) < 100_000


def test_tb2_r6_staged_only_edit_is_archived_and_requires_cleanup(w):
    w.fxw.queue(commit_entry(".gitignore", GITIGNORE, "r1"),
                commit_entry("b.txt", "two\n", "r2", gate="w2"))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")]),
                {"gate": "c1"}, approve())
    top, _, rv = start(w)
    w.until(lambda: w.fxw.spawns() == 2, what="second worker")
    checkout = Path(w.fxr.calls()[0]["cwd"])
    (checkout / ".gitignore").write_text(GITIGNORE + "# " + TRACKED + "\n")
    subprocess.run(["git", "-C", str(checkout), "add", ".gitignore"], check=True)
    (checkout / ".gitignore").write_text(GITIGNORE)
    w.gate("w2", w.fxw)
    cleanup = cleanup_started(w, rv)
    assert TRACKED in cleanup["prompt"] and archive_holds(w, TRACKED)
    subprocess.run(["git", "-C", str(checkout), "reset", "HEAD"], check=True,
                   capture_output=True)
    w.gate("c1", w.fxr)
    finish(w, top, rv)


def test_tb2_r6_unavailable_previous_provider_holds_dirty_with_an_archive(w):
    w.fxw.queue(commit_entry(".gitignore", GITIGNORE, "r1"),
                commit_entry("b.txt", "two\n", "r2", gate="w2"))
    w.fxr.queue(dirtying(), cleaner(), approve())
    _, _, rv = start(w)
    w.until(lambda: w.fxw.spawns() == 2, what="second worker")
    w.fxr.entry["enabled"] = False
    w.write_config()
    w.gate("w2", w.fxw)
    held = w.wait_held(rv, "dirty_worktree", timeout=20)
    assert Path(held["hold"]["detail"]).exists()
    assert archive_holds(w, UNTRACKED)
    assert w.fxr.spawns() == 1


def test_tb2_r6_internal_diff_does_not_execute_a_configured_external_program(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout
    git("init")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (repo / "file").write_text("original\n")
    git("add", "file")
    git("commit", "-m", "initial")
    marker = tmp_path / "executed"
    program = tmp_path / "external"
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    program.chmod(0o700)
    git("config", "diff.external", str(program))
    (repo / "file").write_text("edited\n")
    with gitops._host_scope(repo):
        patch = gitops.run(repo, "diff", "HEAD", check=True).out
    assert "+edited" in patch
    assert not marker.exists()


def test_tb2_r6_archive_preserves_nested_files_and_symlinks_without_reading_targets(tmp_path):
    checkout = tmp_path / "checkout"
    nested = checkout / "nested"
    nested.mkdir(parents=True)
    (nested / "file").write_bytes(b"\x00binary content\xff")
    outside = tmp_path / "outside"
    outside.write_text("outside content")
    (nested / "link").symlink_to(outside)
    target = tmp_path / "archive.tar"
    sessions.archive_untracked(checkout, ["nested/"], target)
    with tarfile.open(target) as archive:
        assert archive.extractfile("nested/file").read() == b"\x00binary content\xff"
        assert archive.getmember("nested/link").issym()
        assert archive.getmember("nested/link").linkname == str(outside)
    assert b"outside content" not in target.read_bytes()


def test_tb2_r6_ignored_cleanup_protects_head_files_and_staged_additions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git(repo, "init")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("kept\nadded\n.venv/\n")
    (repo / "kept").write_text("original\n")
    git(repo, "add", "-f", ".gitignore", "kept")
    git(repo, "commit", "-m", "initial")
    commit = git(repo, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    git(repo, "clone", "--shared", str(repo), str(checkout))
    git(checkout, "rm", "--cached", "kept")
    (checkout / "kept").write_text("tracked edit\n")
    (checkout / "added").write_text("staged addition\n")
    git(checkout, "add", "-f", "added")
    (checkout / ".venv").mkdir()
    (checkout / ".venv" / "ignored").write_text("disposable\n")
    host = tmp_path / "host"
    host.mkdir()
    class Authority:
        @contextmanager
        def pinned_worktree(self, path):
            yield path
    binding = {"commit": commit, "worktree": str(checkout), "id": "alias"}
    with pytest.raises(Refused) as refusal:
        sessions.check_clean(SimpleNamespace(root=repo, scheduler=host), binding, Authority())
    assert refusal.value.result["error"] == "dirty_worktree"
    assert (checkout / "kept").read_text() == "tracked edit\n"
    assert (checkout / "added").read_text() == "staged addition\n"
    assert not (checkout / ".venv").exists()
    archive = Path(refusal.value.result["detail"]).parent
    assert "tracked edit" in (archive / "tracked.diff").read_text()
    assert "staged addition" in (archive / "staged.diff").read_text()
