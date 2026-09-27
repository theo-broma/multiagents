"""SG-R4 — host-side git reads of an agent tree resolve only trusted paths.

Contract: context/specs/sandbox-git.md, SG-R4, and its Decisions sections of
2026-09-27 (the last one, "after testers ag-2e9add … and ag-4c3dd4", renames
the keyword and adds the root, missing-dir and no-config rules), in
particular:

- the trusted paths are *derived*: git dir ``<root>/.git/worktrees/<basename
  of the worktree path>``, common dir ``<root>/.git``, work tree the path
  given. Nothing is read from the worktree's ``.git`` file or from
  ``.git/worktrees/<id>/{gitdir,commondir}`` to find them;
- the gitops reads ``is_dirty``, ``uncommitted_entries``, ``commits_on``,
  ``diff_stat`` and ``changed_paths`` take a keyword ``root=<project root>``
  (their first parameter keeps its name, ``repo``); when it is given,
  resolution is pinned as above. Without it, behaviour is as today;
- when the path given IS the root (the main checkout), git dir = common dir =
  ``<root>/.git`` and work tree = root, with the same neutralisation;
- a derived git dir that does not exist raises ``gitops.GitError`` naming the
  path, and never falls back to unpinned resolution;
- pinned reads write no git config;
- pinned reads run with ``core.fsmonitor`` off and with every hook disabled,
  ``post-index-change`` included;
- when the agent's files try to redirect the repository, the read returns what
  the real repository says.

Black box: only the return values of those five functions, the
``gitops.GitError`` they are contracted to raise, the bytes of the git config
files on disk, and whether a marker program appends to a log file. Every marker here is a script whose
whole effect is one appended line.

Vectors, set up after the worktree exists (the agent's side of the world):

(a)  a ``post-index-change`` hook in the repository's hooks dir;
(a2) ``core.hooksPath`` pointing to a directory inside the agent's worktree;
(b)  ``core.fsmonitor`` set to a script in the repository config;
(b2) ``core.fsmonitor`` in ``.git/worktrees/<id>/config.worktree``, which is
     writable from the container, with ``extensions.worktreeConfig`` on;
(c)  the worktree's ``.git`` file pointing to another git dir, whose config
     sets ``core.fsmonitor`` and whose HEAD/index/refs disagree with the real
     repository;
(c2) the worktree's ``.git`` replaced by such a git *directory*;
(c3) the worktree's ``.git`` file deleted;
(d)  ``.git/worktrees/<id>/commondir`` pointing to such a directory.

Controls: each vector is shown to fire (log written) or to change the answer
under plain git, or under the same function called without ``root=``, in a
``test_control_*`` test. Those pass today; they exist so an SG-R4 test that
goes green cannot be green because its vector was inert.

Deliberately not tested, per the "Vacuous controls" decision:

- ``commits_on`` (``git rev-list``) and ``diff_stat`` / ``changed_paths``
  (``git diff`` between two commits) touch neither the index nor the work
  tree, so vectors (a), (a2), (b) and (b2) fire nothing even under plain git.
  For those three reads only the redirection vectors (c), (c2) and (d) are
  tested, and there on the *answer*: the other git dir's refs must not decide
  it. (The log is still asserted empty; that part is vacuous there.)
- a rewritten ``.git/worktrees/<id>/gitdir``: plain git does not follow it for
  any of these reads (it is used by ``worktree prune``), so it has no control.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from multiagents import gitops

IDENT = ["-c", "user.name=seed", "-c", "user.email=seed@example.invalid"]
NODE_ID = "ag-5e7a91"
BRANCH = "agents/tester/5e7a91"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def commit(cwd: Path, message: str) -> None:
    git(cwd, *IDENT, "commit", "-q", "--no-verify", "-m", message)


@dataclass
class World:
    root: Path          # the project root (main checkout)
    wt: Path            # the agent's worktree, basename == node id
    meta: Path          # <root>/.git/worktrees/<node id>
    log: Path           # every marker appends one line here
    marker: Path        # the marker program
    fake: Path          # an agent-written git dir (for c, c2, d)

    def fired(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def reset_log(self) -> None:
        self.log.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path, monkeypatch):
    """No developer global/system config (an fsmonitor there would confound)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A repo, and a worktree on BRANCH with exactly one commit modifying `a`.

    Real answers, clean worktree: not dirty, no entries, commits_on == 1,
    changed_paths == ["a"], diff_stat names `a` and one file changed.
    """
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a").write_text("a\n")
    (root / "b").write_text("b\n")
    git(root, "add", "a", "b")
    commit(root, "seed")

    wt = tmp_path / "worktrees" / NODE_ID
    gitops.create_worktree(root, wt, BRANCH, base="main", unique=False)
    (wt / "a").write_text("a changed by the agent\n")
    git(wt, "add", "a")
    commit(wt, "agent work")

    log = tmp_path / "fired.log"
    marker = tmp_path / "marker.sh"
    marker.write_text(f'#!/bin/sh\necho "$0 $*" >> "{log}"\nexit 1\n')
    marker.chmod(0o755)

    # The agent's own git dir: a clone whose BRANCH has three commits that
    # modify `b` (never `a`), whose HEAD is BRANCH, and whose config and hooks
    # execute the marker.
    fake_top = tmp_path / "agent-written"
    subprocess.run(["git", "clone", "-q", str(root), str(fake_top)],
                   check=True, capture_output=True)
    git(fake_top, "checkout", "-q", "-b", BRANCH, "main")
    for n in range(3):
        (fake_top / "b").write_text(f"fake {n}\n")
        git(fake_top, "add", "b")
        commit(fake_top, f"fake {n}")
    (fake_top / "z-only-in-fake").write_text("z\n")
    git(fake_top, "add", "z-only-in-fake")
    commit(fake_top, "fake z")
    fake = fake_top / ".git"
    git(fake_top, "config", "core.fsmonitor", str(marker))
    hook = fake / "hooks" / "post-index-change"
    hook.write_text(marker.read_text())
    hook.chmod(0o755)

    w = World(root=root, wt=wt, meta=root / ".git" / "worktrees" / NODE_ID,
              log=log, marker=marker, fake=fake)
    assert w.meta.is_dir(), "fixture: worktree metadata dir named after node id"
    assert gitops.is_dirty(wt) is False, "fixture: worktree starts clean"
    w.reset_log()
    return w


# --- vectors -----------------------------------------------------------------

def vec_a(w: World) -> None:
    hook = w.root / ".git" / "hooks" / "post-index-change"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(w.marker.read_text())
    hook.chmod(0o755)


def vec_a2(w: World) -> None:
    hooks = w.wt / ".agent-hooks"
    hooks.mkdir()
    hook = hooks / "post-index-change"
    hook.write_text(w.marker.read_text())
    hook.chmod(0o755)
    # Excluded from status so the vector does not also change the answer.
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    with open(w.root / ".git" / "info" / "exclude", "a") as fh:
        fh.write(".agent-hooks/\n")
    git(w.root, "config", "core.hooksPath", str(hooks))


def vec_b(w: World) -> None:
    git(w.root, "config", "core.fsmonitor", str(w.marker))


def vec_b2(w: World) -> None:
    git(w.root, "config", "extensions.worktreeConfig", "true")
    (w.meta / "config.worktree").write_text(
        f"[core]\n\tfsmonitor = {w.marker}\n")


def vec_c(w: World) -> None:
    (w.wt / ".git").write_text(f"gitdir: {w.fake}\n")


def vec_c2(w: World) -> None:
    (w.wt / ".git").unlink()
    subprocess.run(["cp", "-a", str(w.fake), str(w.wt / ".git")], check=True)


def vec_c3(w: World) -> None:
    (w.wt / ".git").unlink()


def vec_d(w: World) -> None:
    (w.meta / "commondir").write_text(f"{w.fake}\n")


EXEC_VECTORS = {"a": vec_a, "a2": vec_a2, "b": vec_b, "b2": vec_b2,
                "c": vec_c, "c2": vec_c2, "d": vec_d}
REDIRECT_VECTORS = {"c": vec_c, "c2": vec_c2, "d": vec_d}
ANSWER_VECTORS = {"c": vec_c, "c2": vec_c2, "c3": vec_c3, "d": vec_d}


# --- controls: each vector is live under plain git -----------------------------

def touch_wt(w: World) -> None:
    """Stat-dirty a tracked file so git refreshes, and so writes, the index.

    post-index-change fires only on an index write, which plain git does on
    its own only when entries are racily clean: without this, the controls
    for (a) and (a2) pass or fail depending on timestamps."""
    a = w.wt / "a"
    a.write_text(a.read_text())


@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_control_sg_r4_vector_fires_under_plain_git_status(world, name):
    EXEC_VECTORS[name](world)
    touch_wt(world)
    subprocess.run(["git", "-C", str(world.wt), "status", "--porcelain"],
                   capture_output=True, text=True)
    assert world.fired(), f"vector {name} is inert under plain git status"


@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_control_sg_r4_vector_fires_without_root_keyword(world, name):
    EXEC_VECTORS[name](world)
    touch_wt(world)
    gitops.is_dirty(world.wt)
    assert world.fired(), f"vector {name} is inert for is_dirty without root="


@pytest.mark.parametrize("name", sorted(REDIRECT_VECTORS))
def test_control_sg_r4_redirect_changes_answers_without_root_keyword(world, name):
    REDIRECT_VECTORS[name](world)
    assert gitops.is_dirty(world.wt) is True
    assert gitops.commits_on(world.wt, BRANCH, "main") != 1
    assert gitops.changed_paths(world.wt, BRANCH, "main") != ["a"]
    assert gitops.diff_stat(world.wt, BRANCH, "main") != git(
        world.root, "diff", "--stat", f"main...{BRANCH}")


def test_control_sg_r4_deleted_git_file_breaks_plain_reads(world):
    vec_c3(world)
    # Without a .git file the worktree is an ordinary directory inside no repo
    # (tmp_path is not a repo), so plain git has nothing to answer from.
    assert gitops.commits_on(world.wt, BRANCH, "main") != 1


# --- SG-R4: nothing executes ---------------------------------------------------

@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_sg_r4_is_dirty_with_root_executes_nothing(world, name):
    EXEC_VECTORS[name](world)
    result = gitops.is_dirty(world.wt, root=world.root)
    assert world.fired() == "", f"vector {name} executed under is_dirty(root=)"
    assert result is False


@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_sg_r4_uncommitted_entries_with_root_executes_nothing(world, name):
    EXEC_VECTORS[name](world)
    result = gitops.uncommitted_entries(world.wt, root=world.root)
    assert world.fired() == "", (
        f"vector {name} executed under uncommitted_entries(root=)")
    assert result == []


@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_sg_r4_status_reads_with_root_on_a_touched_tree_execute_nothing(world, name):
    """A stat-dirty tracked file forces an index refresh (and so an index
    write, which is what fires post-index-change) under plain git."""
    EXEC_VECTORS[name](world)
    a = world.wt / "a"
    a.write_text(a.read_text())
    dirty = gitops.is_dirty(world.wt, root=world.root)
    entries = gitops.uncommitted_entries(world.wt, root=world.root)
    assert world.fired() == ""
    assert dirty is False
    assert entries == []


# --- SG-R4: the answer is the real repository's -------------------------------

@pytest.mark.parametrize("name", sorted(EXEC_VECTORS))
def test_sg_r4_status_reads_with_root_still_see_real_changes(world, name):
    """Pinned reads are not simply 'always clean': a real untracked file and a
    real modification are still reported, under every vector.

    Note: today's ``uncommitted_entries`` drops the first ``git status`` line
    when it starts with a space (`` M b``), because the output is stripped
    before it is sliced. This test expects ``b`` all the same: it is what
    ``git status`` reports, and the function's own docstring promises it."""
    EXEC_VECTORS[name](world)
    (world.wt / "new-file").write_text("n\n")
    (world.wt / "b").write_text("b modified in worktree\n")
    dirty = gitops.is_dirty(world.wt, root=world.root)
    entries = gitops.uncommitted_entries(world.wt, root=world.root)
    assert world.fired() == ""
    assert dirty is True
    assert sorted(entries) == ["b", "new-file"]


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_commits_on_with_root_counts_the_real_branch(world, name):
    ANSWER_VECTORS[name](world)
    assert gitops.commits_on(world.wt, BRANCH, "main", root=world.root) == 1
    assert world.fired() == ""


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_changed_paths_with_root_reports_the_real_diff(world, name):
    ANSWER_VECTORS[name](world)
    assert gitops.changed_paths(world.wt, BRANCH, "main", root=world.root) == ["a"]
    assert world.fired() == ""


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_diff_stat_with_root_reports_the_real_diff(world, name):
    expected = git(world.root, "diff", "--stat", f"main...{BRANCH}")
    assert "1 file changed" in expected and expected.startswith("a ")  # fixture sanity
    ANSWER_VECTORS[name](world)
    assert gitops.diff_stat(world.wt, BRANCH, "main", root=world.root) == expected
    assert world.fired() == ""


@pytest.mark.parametrize("name", sorted(ANSWER_VECTORS))
def test_sg_r4_status_reads_with_root_ignore_the_redirected_index_and_head(world, name):
    """The other git dir's HEAD (four commits ahead, touching `b` and adding
    `z-only-in-fake`) and index must not decide the answer: the worktree
    matches the real branch, so it is clean."""
    ANSWER_VECTORS[name](world)
    assert gitops.is_dirty(world.wt, root=world.root) is False
    assert gitops.uncommitted_entries(world.wt, root=world.root) == []
    assert world.fired() == ""


def test_sg_r4_redirect_and_exec_vectors_combined(world):
    """Vectors that can coexist, at once: a hook, fsmonitor in both the repo
    config and config.worktree, and commondir redirected — still nothing fires, and the
    answers are the real repository's."""
    for vec in (vec_a, vec_b, vec_b2, vec_d):
        vec(world)
    assert gitops.is_dirty(world.wt, root=world.root) is False
    assert gitops.uncommitted_entries(world.wt, root=world.root) == []
    assert gitops.commits_on(world.wt, BRANCH, "main", root=world.root) == 1
    assert gitops.changed_paths(world.wt, BRANCH, "main", root=world.root) == ["a"]
    assert world.fired() == ""



# --- SG-R4: the path given is the root itself (the main checkout) ---------------
#
# Pinned resolution is git dir = common dir = <root>/.git, work tree = root.
# The vectors live in the main checkout's own repository: a post-index-change
# hook, core.fsmonitor in <root>/.git/config and in <root>/.git/config.worktree,
# and core.hooksPath into a directory of the main checkout.

def rvec_hook(w: World) -> None:
    vec_a(w)


def rvec_hooks_path(w: World) -> None:
    hooks = w.root / ".root-hooks"
    hooks.mkdir()
    hook = hooks / "post-index-change"
    hook.write_text(w.marker.read_text())
    hook.chmod(0o755)
    (w.root / ".git" / "info").mkdir(exist_ok=True)
    with open(w.root / ".git" / "info" / "exclude", "a") as fh:
        fh.write(".root-hooks/\n")
    git(w.root, "config", "core.hooksPath", str(hooks))


def rvec_fsmonitor(w: World) -> None:
    vec_b(w)


def rvec_fsmonitor_config_worktree(w: World) -> None:
    git(w.root, "config", "extensions.worktreeConfig", "true")
    (w.root / ".git" / "config.worktree").write_text(
        f"[core]\n\tfsmonitor = {w.marker}\n")


ROOT_VECTORS = {"hook": rvec_hook, "hooksPath": rvec_hooks_path,
                "fsmonitor": rvec_fsmonitor,
                "fsmonitor-config.worktree": rvec_fsmonitor_config_worktree}


def touch_root(w: World) -> None:
    """Stat-dirty a tracked file of the main checkout, so plain git refreshes
    (and writes) the index, which is what fires post-index-change."""
    a = w.root / "a"
    a.write_text(a.read_text())


@pytest.mark.parametrize("name", sorted(ROOT_VECTORS))
def test_control_sg_r4_root_vector_fires_under_plain_git_status(world, name):
    ROOT_VECTORS[name](world)
    touch_root(world)
    subprocess.run(["git", "-C", str(world.root), "status", "--porcelain"],
                   capture_output=True, text=True)
    assert world.fired(), f"root vector {name} is inert under plain git status"


@pytest.mark.parametrize("name", sorted(ROOT_VECTORS))
def test_control_sg_r4_root_vector_fires_without_root_keyword(world, name):
    ROOT_VECTORS[name](world)
    touch_root(world)
    gitops.is_dirty(world.root)
    assert world.fired(), f"root vector {name} is inert for is_dirty without root="


@pytest.mark.parametrize("name", sorted(ROOT_VECTORS))
def test_sg_r4_root_path_status_reads_execute_nothing(world, name):
    ROOT_VECTORS[name](world)
    touch_root(world)
    dirty = gitops.is_dirty(world.root, root=world.root)
    entries = gitops.uncommitted_entries(world.root, root=world.root)
    assert world.fired() == "", f"root vector {name} executed under a pinned read"
    assert dirty is False
    assert entries == []


@pytest.mark.parametrize("name", sorted(ROOT_VECTORS))
def test_sg_r4_root_path_status_reads_still_see_real_changes(world, name):
    ROOT_VECTORS[name](world)
    (world.root / "new-file").write_text("n\n")
    (world.root / "b").write_text("b modified in the main checkout\n")
    dirty = gitops.is_dirty(world.root, root=world.root)
    entries = gitops.uncommitted_entries(world.root, root=world.root)
    assert world.fired() == ""
    assert dirty is True
    assert sorted(entries) == ["b", "new-file"]


def test_sg_r4_root_path_branch_reads_answer_from_the_real_repository(world):
    expected = git(world.root, "diff", "--stat", f"main...{BRANCH}")
    for vec in ROOT_VECTORS.values():
        vec(world)
    assert gitops.commits_on(world.root, BRANCH, "main", root=world.root) == 1
    assert gitops.changed_paths(world.root, BRANCH, "main", root=world.root) == ["a"]
    assert gitops.diff_stat(world.root, BRANCH, "main", root=world.root) == expected
    assert world.fired() == ""


def test_sg_r4_root_path_ignores_a_worktree_dir_named_after_the_root(world):
    """`.git/worktrees/` is writable from the container. An agent that plants
    `<root>/.git/worktrees/<basename of root>` (HEAD on its own branch, a
    commondir to its own git dir, fsmonitor in its config) must not become
    the main checkout's git dir: for the root, the git dir is <root>/.git."""
    planted = world.root / ".git" / "worktrees" / world.root.name
    shutil.copytree(world.fake, planted)
    (planted / "commondir").write_text(f"{world.fake}\n")
    (planted / "gitdir").write_text(f"{world.root / '.git'}\n")
    (planted / "config").write_text(
        f"[core]\n\tfsmonitor = {world.marker}\n")
    touch_root(world)
    assert gitops.is_dirty(world.root, root=world.root) is False
    assert gitops.uncommitted_entries(world.root, root=world.root) == []
    assert gitops.commits_on(world.root, BRANCH, "main", root=world.root) == 1
    assert gitops.changed_paths(world.root, BRANCH, "main", root=world.root) == ["a"]
    assert world.fired() == ""


# --- SG-R4: a missing derived git dir is an error, never a fallback ------------

PINNED_READS = {
    "is_dirty": lambda path, root: gitops.is_dirty(path, root=root),
    "uncommitted_entries": lambda path, root: gitops.uncommitted_entries(path, root=root),
    "commits_on": lambda path, root: gitops.commits_on(path, BRANCH, "main", root=root),
    "diff_stat": lambda path, root: gitops.diff_stat(path, BRANCH, "main", root=root),
    "changed_paths": lambda path, root: gitops.changed_paths(path, BRANCH, "main", root=root),
}


def assert_names_the_path(w: World, message: str) -> None:
    # "naming the path": the path given, or the derived git dir that is
    # missing — either tells the reader which worktree broke.
    assert str(w.wt) in message or str(w.meta) in message, (
        f"GitError does not name the path: {message!r}")


@pytest.mark.parametrize("fn", sorted(PINNED_READS))
def test_sg_r4_missing_derived_git_dir_raises_git_error(world, fn):
    shutil.rmtree(world.meta)
    with pytest.raises(gitops.GitError) as exc:
        PINNED_READS[fn](world.wt, world.root)
    assert_names_the_path(world, str(exc.value))


@pytest.mark.parametrize("fn", sorted(PINNED_READS))
def test_sg_r4_missing_derived_git_dir_never_falls_back_to_the_git_file(world, fn):
    """With the metadata dir gone, the worktree's `.git` file still points to
    a git dir that would answer (the agent's). Following it would be the
    fallback the contract forbids: it must raise, and execute nothing."""
    shutil.rmtree(world.meta)
    vec_c(world)
    with pytest.raises(gitops.GitError) as exc:
        PINNED_READS[fn](world.wt, world.root)
    assert_names_the_path(world, str(exc.value))
    assert world.fired() == ""


@pytest.mark.parametrize("fn", sorted(PINNED_READS))
def test_sg_r4_a_path_with_no_derived_git_dir_raises_git_error(world, tmp_path, fn):
    """A directory whose basename names no worktree of the root, even one
    carrying a `.git` file to the real repository's metadata, is refused."""
    stray = tmp_path / "worktrees" / "ag-000000"
    stray.mkdir()
    (stray / ".git").write_text(f"gitdir: {world.meta}\n")
    with pytest.raises(gitops.GitError) as exc:
        PINNED_READS[fn](stray, world.root)
    assert str(stray) in str(exc.value) or str(
        world.root / ".git" / "worktrees" / "ag-000000") in str(exc.value), (
        f"GitError does not name the path: {exc.value!s}")


def test_control_sg_r4_missing_git_dir_with_redirect_answers_without_root_keyword(world):
    """The fallback is live: unpinned, the `.git` file alone answers."""
    shutil.rmtree(world.meta)
    vec_c(world)
    assert gitops.commits_on(world.wt, BRANCH, "main") == 4


# --- SG-R4: pinned reads write no git config ----------------------------------

def config_snapshot(w: World, tmp_path: Path) -> dict[str, bytes | None]:
    """Every file git could take configuration from, and the redirection
    files next to them: its bytes, or None when it does not exist."""
    files = [p for p in (w.root / ".git").rglob("*")
             if p.is_file() and (p.name.startswith("config")
                                 or p.name in ("commondir", "gitdir"))]
    files += [w.root / ".git" / "config.worktree",
              w.meta / "config.worktree",
              w.wt / ".git",
              tmp_path / "empty-gitconfig",
              Path.home() / ".gitconfig"]
    return {str(p): (p.read_bytes() if p.is_file() else None)
            for p in sorted(set(files))}


@pytest.mark.parametrize("name", ["none", "b", "b2"])
def test_sg_r4_pinned_reads_write_no_git_config(world, tmp_path, name):
    """Neutralising fsmonitor or hooks by writing config (`git config
    core.fsmonitor false`, a config.worktree) is not allowed: nothing on disk
    that git reads configuration from changes, for the worktree or the root,
    with or without a vector already in the config."""
    if name != "none":
        EXEC_VECTORS[name](world)
    before = config_snapshot(world, tmp_path)
    for path in (world.wt, world.root):
        for read in PINNED_READS.values():
            read(path, world.root)
    assert config_snapshot(world, tmp_path) == before
