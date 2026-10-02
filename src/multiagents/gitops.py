"""Branch and worktree lifecycle.

A branch alone cannot isolate parallel agents: ``git checkout`` is global to a
working tree, so two agents on two branches in one directory overwrite each
other within seconds. Every writing agent therefore gets a real ``git worktree``
— its own checkout of its own branch, sharing the repository's object store.

Those worktrees live outside the project (see :mod:`multiagents.paths`) so an
agent's file search cannot reach a sibling's checkout. That has one consequence
worth knowing: a linked worktree's ``.git`` file records an **absolute** path
back to the main repository, and the repository records an absolute path back to
the worktree. Any future containerisation must mount both at their exact host
paths or git breaks in confusing ways.

The parent performs every operation here. Subagents only commit.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


_host_context: contextvars.ContextVar[tuple[list[str], dict[str, str]] | None] = contextvars.ContextVar(
    "host_git_context", default=None)


def _host_env(env: dict[str, str] | None) -> dict[str, str]:
    """The environment a host-scoped git call runs with: the caller's, minus
    any git location it inherited, plus `env`."""
    actual = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_EXTERNAL_DIFF"):
        actual.pop(key, None)
    actual.update(env or {})
    return actual


def _content_disable_args(checkout: Path, args: list[str],
                          env: dict[str, str]) -> list[str]:
    """HG-R4's `-c` overrides emptying every content program.

    HG-R10: listed with exactly the git dir, work tree, HEAD, arguments and
    environment of the call it protects, so a conditional include
    (`onbranch:`, `gitdir:`) cannot show that call a program this list missed.
    """
    keys = subprocess.run(
        ["git", "-C", str(checkout), *args, "config", "--name-only", "--get-regexp",
         r"^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver|diff\..*\.textconv|diff\.external)$"],
        capture_output=True, text=True, env=_host_env(env),
    )
    return [item for key in keys.stdout.splitlines()
            for item in ("-c", f"{key}={'false' if key.endswith('.required') else ''}")] + [
        "-c", "diff.external="]


IDENTITY_KEYS = r"^(user|author|committer)\.(name|email)$"


def _identity_args(gitdir: Path, common: Path, checkout: Path,
                   env: dict[str, str]) -> list[str]:
    """HG-R11: the identity plain git resolves in a linked worktree, as `-c`.

    The private git dir a host call runs with is not the worktree's, so an
    `includeIf "gitdir:..."` naming the worktree's git dir no longer matches
    it. The identity keys are read here with the real git dir, file scope by
    file scope, skipping `config.worktree` (agent-written, HG-R1). A key
    the environment sets (`GIT_CONFIG_COUNT`, `-c`) is left to it, as it
    outranks files for plain git too; a key found nowhere is left missing, so
    CI-R1's fallback still applies.
    """
    base = _host_env({"GIT_DIR": str(gitdir), "GIT_COMMON_DIR": str(common),
                      "GIT_WORK_TREE": str(checkout)})
    scopes = ["--global", "--local"]
    if base.get("GIT_CONFIG_NOSYSTEM", "").lower() not in ("1", "true", "yes", "on"):
        scopes.insert(0, "--system")
    found: dict[str, str] = {}
    for scope in scopes:
        result = subprocess.run(
            ["git", "-C", str(checkout), "config", scope, "--includes",
             "--get-regexp", IDENTITY_KEYS],
            capture_output=True, text=True, env=base,
        )
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            found[key] = value
    command = subprocess.run(
        ["git", "-C", str(checkout), "config", "--show-scope", "--get-regexp",
         IDENTITY_KEYS],
        capture_output=True, text=True, env=_host_env(env),
    )
    for line in command.stdout.splitlines():
        scope, _, rest = line.partition("\t")
        if scope == "command":
            found.pop(rest.partition(" ")[0], None)
    return [item for key, value in found.items() for item in ("-c", f"{key}={value}")]


def _plain_branch_tip(common: Path, ref: str) -> str:
    """HG-R8: the commit `ref` names, provided `ref` is a regular branch ref.

    A branch ref lives in the common dir, which a container can write; turned
    into a symbolic ref it would carry a host commit onto whatever it names.
    """
    env = {"GIT_DIR": str(common), "GIT_COMMON_DIR": str(common)}
    if not ref.startswith("refs/heads/"):
        raise GitError(f"host_authority_mismatch: branch {ref} is not a local branch")
    symbolic = subprocess.run(["git", "symbolic-ref", "-q", ref], capture_output=True,
                              text=True, env=_host_env(env))
    if symbolic.returncode != 1:
        raise GitError(f"host_authority_mismatch: branch {ref} is a symbolic ref"
                       if symbolic.returncode == 0 else
                       f"could not read branch {ref}: {symbolic.stderr.strip()}")
    tip = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                         capture_output=True, text=True, env=_host_env(env))
    if tip.returncode != 0 or not tip.stdout.strip():
        raise GitError(f"host_authority_mismatch: branch {ref} does not name a commit")
    return tip.stdout.strip()


@contextlib.contextmanager
def _host_scope(repo: Path, *, root: Path | None = None,
                hooks: bool = False, content: bool = False,
                branch: str = ""):
    """Use trusted repository metadata for a host Git transaction.

    A linked checkout's .git, commondir and config.worktree are all writable
    by its agent. A private gitdir retains the real HEAD and index while
    obtaining config and refs solely from the base repository.

    HG-R8: the branch ref is writable too. So the private HEAD is detached at
    the branch's tip, checked to be a regular ref, and whatever the call
    commits is put on that ref afterwards with `--no-deref`, only if the ref
    still holds the tip it started from. No symbolic ref is ever followed.
    """
    root = Path(root or repo).resolve()
    checkout = Path(repo)
    repo = checkout.resolve()
    common = root / ".git"
    if not common.is_dir():
        raise GitError(f"no trusted git directory at {common}")
    with tempfile.TemporaryDirectory(prefix="multiagents-host-git-") as tmp:
        args = ["-c", "core.fsmonitor=false"]
        if not hooks:
            empty_hooks = Path(tmp) / "hooks"
            empty_hooks.mkdir()
            args += ["-c", f"core.hooksPath={empty_hooks}"]
        env: dict[str, str] = {"GIT_DIR": str(common), "GIT_WORK_TREE": str(root),
                               "GIT_COMMON_DIR": str(common)}
        ref, tip, private = "", "", None
        if repo != root:
            actual = common / "worktrees" / repo.name
            head = _read_regular(actual / "HEAD", 4096)
            if head is None or not actual.is_dir() or actual.is_symlink():
                raise GitError(f"no trusted worktree metadata for {repo}")
            held = head.decode(errors="replace").strip()
            if branch and held != f"ref: refs/heads/{branch}":
                raise GitError("host_authority_mismatch: worktree HEAD differs from host record")
            registered = _registration_branch(root, repo, branch)
            if registered is not None and branch and registered != branch:
                raise GitError("host_authority_mismatch: worktree registration differs from host record")
            if held.startswith("ref:"):
                ref = held.removeprefix("ref:").strip()
                tip = _plain_branch_tip(common, ref)
                head = f"{tip}\n".encode()
            private = Path(tmp) / "gitdir"
            private.mkdir()
            (private / "HEAD").write_bytes(head)
            (private / "commondir").write_text(f"{common}\n")
            index = actual / "index"
            if index.is_symlink() or (index.exists() and not index.is_file()):
                raise GitError(f"unreadable index for {repo}")
            env = {"GIT_DIR": str(private), "GIT_WORK_TREE": str(checkout),
                   "GIT_COMMON_DIR": str(common), "GIT_INDEX_FILE": str(index),
                   "GIT_OPTIONAL_LOCKS": "0"}
            args += _identity_args(actual, common, checkout, env)
        if not content:
            args += _content_disable_args(checkout, args, env)
        token = _host_context.set((args, env))
        try:
            yield
        finally:
            _host_context.reset(token)
        if ref:
            _advance_branch(common, ref, tip, private)


def _advance_branch(common: Path, ref: str, tip: str, private: Path) -> None:
    """HG-R8: move `ref` to what the host call left at its detached HEAD."""
    env = _host_env({"GIT_DIR": str(common), "GIT_COMMON_DIR": str(common)})
    now = _read_regular(private / "HEAD", 4096)
    head = (now or b"").decode(errors="replace").strip()
    if head == tip:
        return
    if len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head):
        raise GitError("host_authority_mismatch: host call left HEAD off its branch")
    moved = subprocess.run(
        ["git", "update-ref", "--no-deref", "-m", "multiagents: host commit", ref, head, tip],
        capture_output=True, text=True, env=env,
    )
    if moved.returncode != 0:
        raise GitError(f"host_authority_mismatch: branch {ref} moved during the host "
                       f"call: {moved.stderr.strip()}")


@dataclass
class GitResult:
    ok: bool
    out: str
    err: str
    code: int
    # CI-R5: set by `commit_all` to the name of the git hook that refused the
    # commit (`pre-commit`, `commit-msg`, ...); empty for any other failure.
    hook: str = ""


def run(repo: Path, *args: str, check: bool = False, timeout: int = 120,
        env: dict[str, str] | None = None, strip: bool = True) -> GitResult:
    host = _host_context.get()
    if host:
        prefix, host_env = host
        args = (*prefix, *args)
        actual_env = _host_env({**host_env, **(env or {})})
    else:
        actual_env = {**os.environ, **env} if env else None
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
        env=actual_env,
    )
    out = proc.stdout.strip() if strip else proc.stdout
    result = GitResult(proc.returncode == 0, out, proc.stderr.strip(), proc.returncode)
    if check and not result.ok:
        raise GitError(f"git {' '.join(args)} failed: {result.err or result.out}")
    return result


class Git:
    """Where an agent's own commits run (SG-R3): here, on the host.

    `commit_all` and `restore_paths` commit on an agent's branch, and a commit
    runs the repository's hooks — code the agent can write. An executor with
    a sandbox hands those functions its own `Git`, which runs each command in
    there instead (`Executor.git`); this one is the local executor's, which
    has no sandbox and keeps today's behaviour.
    """

    def run(self, repo: Path, *args: str, env: dict[str, str] | None = None,
            timeout: int = 120, strip: bool = True) -> GitResult:
        """:func:`run`, wherever this `Git` runs git. `env` is added to the
        environment git runs with there."""
        return run(repo, *args, env=env, timeout=timeout, strip=strip)

    def environ(self) -> dict[str, str]:
        """The environment git runs with, before `env` is added."""
        return dict(os.environ)

    def scratch(self):
        """A context manager yielding a directory git writes to there and this
        process reads here — where a commit's trace goes (CI-R5)."""
        return tempfile.TemporaryDirectory(prefix="multiagents-commit-")


HOST = Git()


def is_repo(path: Path, *, root: Path | None = None) -> bool:
    try:
        return _read(path, root, "rev-parse", "--git-dir").ok
    except GitError:
        return False


def init_repo(path: Path) -> GitResult:
    """``git init`` in an existing directory."""
    return run(path, "init")


def initial_commit(repo: Path, message: str = "initial commit") -> GitResult:
    """The first commit, which every agent branch is cut from.

    ``--allow-empty`` so a brand-new project with no files yet still gets a
    commit: without one there is nothing to branch a worktree from.
    """
    run(repo, "add", "-A")
    # CI-R3: same fallback identity as the other commits multiagents itself
    # makes — a brand-new project has no git identity configured either.
    extra = _identity_fallback_args(repo, "multiagents", "orchestrator@multiagents.invalid")
    return run(repo, *extra, "commit", "--allow-empty", "-m", message)


def _read_regular(path: Path, limit: int) -> bytes | None:
    """`path`'s bytes if it is a regular file of at most `limit` bytes, else None.

    For files a container can write: opened without following a link and
    non-blocking, so a symlink, a FIFO or a device is refused rather than read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return None if size > limit else b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _open_beneath(base: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    """A directory fd for ``base/parts...``, each part opened without following
    a link (SG-R7). `base` is trusted; the parts are where a container writes.

    With `create`, a missing part is made, and one that is not a directory —
    a link, a FIFO, a file an agent planted — is removed and made afresh.
    Otherwise such a part raises `OSError`.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise OSError(f"not a plain path component: {part!r}")
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError:
                if not create:
                    raise
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(part, dir_fd=fd)
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_beneath(base: Path, parts: tuple[str, ...], name: str, data: bytes, *,
                   mode: int = 0o600, create: bool = False) -> None:
    """Replace ``base/parts.../name`` with `data`, never through a link (SG-R7).

    Written to a fresh file beside it and renamed over it, so whatever stood
    at `name` — a symlink to a host file, a FIFO — is replaced, never written
    through or opened. Raises `OSError` when that cannot be done.
    """
    if name in ("", ".", "..") or "/" in name:
        raise OSError(f"not a plain file name: {name!r}")
    dfd = _open_beneath(base, parts, create=create)
    try:
        tmp = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      mode, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as fh:
                fh.write(data)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)


def _read_beneath(base: Path, parts: tuple[str, ...], name: str,
                  limit: int) -> bytes | None:
    """:func:`_read_regular` for ``base/parts.../name``, with no link followed
    anywhere below `base` (SG-R7), not only at the last component."""
    try:
        dfd = _open_beneath(base, parts)
    except OSError:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 20, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return None if size > limit else b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def beneath(directory: Path) -> tuple[Path, tuple[str, ...]]:
    """`directory` as the primitives above take it (SG-R7): its last two
    components beneath the directory that holds them. For a run dir that is
    `.multiagents`, trusted, then `runs/<id>`, which a container can write."""
    directory = Path(directory)
    base = directory.parents[1] if len(directory.parts) > 2 else Path(directory.anchor or ".")
    return base, directory.relative_to(base).parts


def _open_file_beneath(base: Path, parts: tuple[str, ...], name: str, flags: int,
                       mode: int = 0o666, *, create: bool = False,
                       replace: bool = False) -> int:
    """A file descriptor for the regular file ``base/parts.../name`` (SG-R7).

    No link is followed below `base`, and the open never blocks: a symlink, a
    FIFO or a device at `name` is refused with `OSError`. With `replace`, for
    a file the host owns, such an entry is removed first, and the open is
    tried once more (with `flags`, so `O_CREAT` makes it afresh). `create`
    makes missing directories, as :func:`_open_beneath` does.
    """
    if name in ("", ".", "..") or "/" in name:
        raise OSError(f"not a plain file name: {name!r}")
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    dfd = _open_beneath(base, parts, create=create)
    try:
        for last in (False, True):
            try:
                fd = os.open(name, flags, mode, dir_fd=dfd)
            except OSError:
                # ELOOP for a link, ENXIO for a FIFO nobody reads, and so on:
                # whatever is there, it is not a regular file to open.
                if last or not replace or not _drop_irregular(dfd, name):
                    raise
                continue
            if stat.S_ISREG(os.fstat(fd).st_mode):
                return fd
            os.close(fd)
            if last or not replace or not _drop_irregular(dfd, name):
                raise OSError(f"not a regular file: {name!r}")
        raise AssertionError("unreachable")
    finally:
        os.close(dfd)


def _drop_irregular(dfd: int, name: str) -> bool:
    """Remove `name` from `dfd` if it is there and neither a regular file nor
    a directory; whether it is worth trying the open again."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if stat.S_ISREG(st.st_mode):
        return True                     # made by someone else meanwhile
    if stat.S_ISDIR(st.st_mode):
        return False
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dfd)
    return True


def _unlink_beneath(base: Path, parts: tuple[str, ...], name: str) -> None:
    """Remove ``base/parts.../name`` if it is there, through no link (SG-R7)."""
    try:
        dfd = _open_beneath(base, parts)
    except FileNotFoundError:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


# An index bigger than this is not copied for a pinned read (SG-R4): the read
# raises instead, and it is polled every few seconds.
INDEX_MAX_BYTES = 128 * 1024 * 1024


@contextlib.contextmanager
def _pinned(repo: Path, root: Path | None):
    """``(args, env)`` for a host-side read of `repo`, a tree of project `root`.

    SG-R4 (context/specs/sandbox-git.md): with `root`, git resolves only paths
    derived from it — common dir ``<root>/.git``, git dir
    ``<root>/.git/worktrees/<basename>`` (``<root>/.git`` for the root itself),
    work tree `repo` — never the worktree's ``.git`` file nor the ``gitdir``
    and ``commondir`` files an agent can rewrite. Git follows a ``commondir``
    file even with ``GIT_COMMON_DIR`` set, so the git dir handed to git is a
    private one: the derived dir's HEAD and index copied into it, and a
    ``commondir`` of our own writing. No hook and no fsmonitor runs, the index
    is never written back, and no config is written anywhere. An index that
    is over `INDEX_MAX_BYTES`, or is not a regular file (a symlink, a FIFO),
    raises `GitError`; only a missing one is read as git reads it.

    Without `root`, plain git: ``([], None)``.
    """
    if root is None:
        yield [], None
        return
    root = Path(root).resolve()
    tree = Path(repo).resolve()
    common = root / ".git"
    gitdir = common if tree == root else common / "worktrees" / tree.name
    if not gitdir.is_dir() or gitdir.is_symlink():
        raise GitError(f"no git directory for {tree}: {gitdir} does not exist")
    head = _read_regular(gitdir / "HEAD", 4096)
    if head is None:
        raise GitError(f"no readable HEAD for {tree} in {gitdir}")
    with tempfile.TemporaryDirectory(prefix="multiagents-read-") as tmp:
        private = Path(tmp) / "gitdir"
        private.mkdir()
        (private / "HEAD").write_bytes(head)
        (private / "commondir").write_text(f"{common}\n")
        index_path = gitdir / "index"
        index = _read_regular(index_path, INDEX_MAX_BYTES)
        if index is not None:
            (private / "index").write_bytes(index)
        elif index_path.is_symlink() or index_path.exists():
            raise GitError(f"unreadable index for {tree}: {index_path} is not a "
                           f"regular file of at most {INDEX_MAX_BYTES} bytes")
        hooks = Path(tmp) / "hooks"
        hooks.mkdir()
        env = {"GIT_DIR": str(private), "GIT_WORK_TREE": str(tree),
               "GIT_INDEX_FILE": str(private / "index"),
               "GIT_OPTIONAL_LOCKS": "0"}
        args = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={hooks}"]
        args += _content_disable_args(tree, args, env)
        yield args, env


def _read(repo: Path, root: Path | None, *args: str, strip: bool = True,
          check: bool = False) -> GitResult:
    """`run`, pinned to `root`'s trusted paths when `root` is given (SG-R4).

    With `check`, a pinned read that git fails raises `GitError`: a tree that
    could not be read is never reported clean. Unpinned reads never raise.
    """
    with _pinned(repo, root) as (extra, env):
        return run(repo, *extra, *args, env=env, strip=strip,
                   check=check and root is not None)


def uncommitted_entries(repo: Path, *, root: Path | None = None) -> list[str]:
    """Paths ``git status`` reports, directories collapsed to one entry.

    Collapsing matters for the caller: a first commit of a project with
    ``node_modules`` is one line to show the user, not forty thousand.

    With `root`, resolution is pinned to the project's trusted paths (SG-R4),
    and a failure raises `GitError` rather than reading as no entries.
    """
    # Unstripped: a leading space is the first entry's status column.
    result = _read(repo, root, "status", "--porcelain", "-unormal", strip=False,
                   check=True)
    return [line[3:].strip().strip('"') for line in result.out.splitlines() if line[3:].strip()]


def repo_root(path: Path) -> Path | None:
    """The top level of the repository containing `path`, or None.

    Distinct from :func:`is_repo`, which answers "is there a repository above
    me" — inside a monorepo that is true of every subdirectory, and a project
    rooted at one would get worktrees of the whole repository without saying so.
    """
    result = run(path, "rev-parse", "--show-toplevel")
    return Path(result.out) if result.ok and result.out else None


def ensure_repo(path: Path) -> None:
    if not is_repo(path):
        raise GitError(
            f"{path} is not a git repository. Agents work on branches, so this "
            f"project needs one — run `git init` (and make at least one commit)."
        )


def has_commits(repo: Path) -> bool:
    return run(repo, "rev-parse", "--verify", "HEAD").ok


def current_branch(repo: Path) -> str:
    result = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.out if result.ok else ""


def head_sha(repo: Path, *, root: Path | None = None) -> str:
    """With `root`, resolution is pinned to the project's trusted paths (SG-R4)."""
    result = _read(repo, root, "rev-parse", "HEAD")
    return result.out if result.ok else ""


def status(repo: Path, *, root: Path | None = None) -> GitResult:
    """``git status --porcelain``, the result whole: a caller that must tell
    "clean" from "could not tell" reads `ok` as well as the output.

    With `root`, resolution is pinned to the project's trusted paths (SG-R4),
    and a failure raises `GitError` rather than coming back not `ok`.
    """
    return _read(repo, root, "status", "--porcelain", check=True)


def is_dirty(repo: Path, *, root: Path | None = None) -> bool:
    """Whether ``git status`` reports anything. With `root`, pinned (SG-R4),
    and a tree git cannot read raises `GitError`: it is never clean."""
    result = _read(repo, root, "status", "--porcelain", check=True)
    return bool(result.out.strip())


def branch_exists(repo: Path, branch: str) -> bool:
    return run(repo, "rev-parse", "--verify", f"refs/heads/{branch}").ok


def unique_branch(repo: Path, desired: str) -> str:
    """Avoid colliding with a branch left behind by an earlier run."""
    if not branch_exists(repo, desired):
        return desired
    for suffix in range(2, 100):
        candidate = f"{desired}-{suffix}"
        if not branch_exists(repo, candidate):
            return candidate
    raise GitError(f"could not find a free branch name near {desired!r}")


def create_worktree(repo: Path, path: Path, branch: str, base: str = "",
                    unique: bool = True) -> str:
    """Create `path` as a new worktree on a fresh `branch` cut from `base`.

    With `unique=False` exactly `branch`, failing if it exists, rather than a
    suffixed name beside it."""
    ensure_repo(repo)
    if not has_commits(repo):
        raise GitError(
            "This repository has no commits yet. Make an initial commit before "
            "spawning agents — a worktree cannot be branched from nothing."
        )
    if unique:
        branch = unique_branch(repo, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", str(path), "-b", branch]
    if base:
        args.append(base)
    with _host_scope(repo):
        run(repo, *args, check=True, timeout=300)
    return branch


def attach_worktree(repo: Path, path: Path, branch: str) -> None:
    """Check out the EXISTING `branch` at `path` as a worktree, commits intact.

    The counterpart of `create_worktree` for a run that already has a branch
    and lost its checkout: cutting a new one would fork the node's work
    (SP-R4). A registration left behind by a deleted directory is pruned
    first, or git refuses the branch as still checked out there. A branch
    genuinely checked out somewhere else is refused, never forced away.
    """
    ensure_repo(repo)
    if not branch_exists(repo, branch):
        raise GitError(f"branch {branch!r} no longer exists")
    # Git's global prune can act on another agent's registration. Only the
    # branch's own stale entry may be removed during recovery.
    _prune_path(repo, path, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _host_scope(repo):
        run(repo, "worktree", "add", str(path), branch, check=True, timeout=300)
    with _host_scope(path, root=repo):
        filtered = _filtered_paths(path, HOST, all_paths=True)
    if filtered:
        print("Host checkout left filtered paths unconverted: " + ", ".join(filtered[:50]))


def worktree_branch(path: Path, *, root: Path | None = None) -> str | None:
    """The branch checked out in the worktree whose top level is `path`.

    None when `path` is not the top of a git checkout at all — missing, a
    plain directory, or a subdirectory of some other repository, which git
    would happily answer for. "" for a detached HEAD.
    """
    try:
        top = _read(path, root, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    if not top.ok or not top.out:
        return None
    try:
        if Path(top.out).resolve() != Path(path).resolve():
            return None
    except OSError:
        return None
    try:
        head = _read(path, root, "symbolic-ref", "--quiet", "--short", "HEAD")
    except GitError:
        return None
    return head.out if head.ok else ""


def move_aside(repo: Path, path: Path) -> Path:
    """Move whatever occupies `path` out of the way, and return where it went.

    For a checkout that is not the one a run needs there: it is kept, never
    deleted, because it may hold the only copy of something (SP-R4). It goes
    into a sibling `<name>.aside[-N]` directory created for it — created, not
    merely found free, so two callers cannot pick the same one and nothing is
    ever renamed over. A registered worktree of `repo` is moved only by git,
    so the registry follows it; if git cannot, nothing moves and this raises.
    Anything else is renamed.
    """
    path = Path(path)
    for n in range(1, 1000):
        holder = path.with_name(f"{path.name}.aside" + (f"-{n}" if n > 1 else ""))
        try:
            holder.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise GitError(f"could not move {path} aside: {exc}") from exc
        break
    else:
        raise GitError(f"no free name to move {path} aside to")
    target = holder / path.name
    try:
        if not path.is_symlink() and _registered_worktree(repo, path):
            branch = _registration_branch(repo, path)
            if not branch:
                raise GitError("host_authority_mismatch: worktree registration is ambiguous")
            with _host_scope(repo):
                moved = run(repo, "worktree", "move", str(path), str(target), timeout=300)
            if not moved.ok:
                raise GitError(f"git worktree move {path} failed: "
                               f"{moved.err or moved.out}")
        else:
            try:
                path.rename(target)
            except OSError as exc:
                raise GitError(f"could not move {path} aside: {exc}") from exc
    except BaseException:
        try:
            holder.rmdir()
        except OSError:
            pass
        raise
    return target


def _registered_worktree(repo: Path, path: Path) -> bool:
    """Whether `repo`'s worktree registry has an entry at `path`."""
    listed = run(repo, "worktree", "list", "--porcelain")
    if not listed.ok:
        return False
    try:
        want = path.resolve()
    except OSError:
        return False
    for line in listed.out.splitlines():
        if line.startswith("worktree "):
            try:
                if Path(line[len("worktree "):]).resolve() == want:
                    return True
            except OSError:
                continue
    return False


def _prune_path(repo: Path, path: Path, branch: str = "") -> None:
    """Remove only a stale registration for this path and branch."""
    if branch:
        _registration_branch(repo, path, branch)
    if path.exists():
        return
    registrations = Path(repo) / ".git" / "worktrees"
    if not registrations.is_dir():
        return
    matches: list[Path] = []
    for entry in registrations.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        marker = _read_regular(entry / "gitdir", 4096)
        head = _read_regular(entry / "HEAD", 4096)
        if marker is None or head is None:
            continue
        if Path(marker.decode(errors="replace").strip()).resolve() != (path / ".git").resolve():
            continue
        held = head.decode(errors="replace").strip()
        if branch and held != f"ref: refs/heads/{branch}":
            raise GitError("host_authority_mismatch: worktree registration branch differs")
        matches.append(entry)
    if len(matches) > 1:
        raise GitError("host_authority_mismatch: duplicate worktree registration")
    for entry in matches:
        shutil.rmtree(entry)


def _registration_branch(repo: Path, path: Path, expected_branch: str = "") -> str | None:
    registrations = Path(repo) / ".git" / "worktrees"
    matches: list[str] = []
    if not registrations.is_dir():
        return None
    for entry in registrations.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        marker = _read_regular(entry / "gitdir", 4096)
        head = _read_regular(entry / "HEAD", 4096)
        if marker is None or head is None:
            continue
        value = head.decode(errors="replace").strip()
        marker_path = Path(marker.decode(errors="replace").strip()).resolve()
        expected_path = (path / ".git").resolve()
        if (expected_branch and value == f"ref: refs/heads/{expected_branch}"
                and marker_path != expected_path):
            raise GitError("host_authority_mismatch: recorded branch points at another worktree")
        if marker_path == expected_path:
            if not value.startswith("ref: refs/heads/"):
                raise GitError("host_authority_mismatch: detached worktree registration")
            matches.append(value.removeprefix("ref: refs/heads/"))
    if len(matches) > 1:
        raise GitError("host_authority_mismatch: duplicate worktree registration")
    return matches[0] if matches else None


def remove_worktree(repo: Path, path: Path, force: bool = False) -> GitResult:
    _registration_branch(repo, path)
    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    with _host_scope(repo):
        result = run(repo, *args, timeout=180)
    if not result.ok:
        # A directory deleted by hand leaves a stale registration behind.
        _prune_path(repo, path)
    return result


def owning_repo(worktree: Path) -> Path | None:
    """The repository a linked worktree belongs to, read from its `.git` file.

    A linked worktree stores `gitdir: <repo>/.git/worktrees/<id>`. Reading it
    is how a teardown can find every repository it is about to leave a stale
    worktree registration in — the directory has to be inspected *before* it is
    deleted, because afterwards there is nothing left to ask.
    """
    marker = worktree / ".git"
    if not marker.is_file():
        return None
    text = marker.read_text().strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip())
    # <repo>/.git/worktrees/<id> -> <repo>
    if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
        return None
    return gitdir.parent.parent.parent


def prune_worktrees(repo: Path) -> GitResult:
    # No unscoped prune: the registry is agent writable, and a forged gitdir
    # in a sibling entry could cause Git to act outside the requested node.
    return GitResult(True, "", "", 0)


def prune_worktree(repo: Path, path: Path, branch: str) -> GitResult:
    _prune_path(repo, path, branch)
    return GitResult(True, "", "", 0)


def delete_branch(repo: Path, branch: str, force: bool = False) -> GitResult:
    with _host_scope(repo):
        return run(repo, "branch", "-D" if force else "-d", branch)


def refused_by_packed_refs_lock(result: GitResult) -> bool:
    """Whether a ref deletion failed only because `packed-refs.lock` could not
    be created: git takes it in `.git/` for every deletion, and the
    container's `.git` is read-only (SG-R2)."""
    return not result.ok and "packed-refs.lock" in (result.err or result.out)


def commits_on(repo: Path, branch: str, base: str, *,
               root: Path | None = None) -> int:
    """How many commits `branch` has that `base` does not."""
    result = _read(repo, root, "rev-list", "--count", f"{base}..{branch}")
    try:
        return int(result.out) if result.ok else 0
    except ValueError:
        return 0


def is_ancestor(repo: Path, ancestor: str, descendant: str, *,
                root: Path | None = None) -> bool:
    """Whether `ancestor` is one of `descendant`'s; False when git cannot say.

    With `root`, resolution is pinned to the project's trusted paths (SG-R4).
    """
    return _read(repo, root, "merge-base", "--is-ancestor", ancestor, descendant).ok


def resolve_commit(repo: Path, ref: str) -> str:
    """The full sha `ref` names, or "" when it names no commit."""
    if not ref:
        return ""
    result = run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.out if result.ok else ""


def short_sha(repo: Path, ref: str) -> str:
    result = run(repo, "rev-parse", "--short", ref)
    return result.out if result.ok else ""


def holds_unmerged_commits(repo: Path, head: str, base: str, since: str = "", *,
                           root: Path | None = None) -> bool:
    """Whether `head` has commits whose changes `base` does not already hold.

    Absorbed means merging `head` into `base` would change no file, which is
    what a squash merge leaves behind: the branch's own shas never reach base,
    but its content does. A conflicting merge, or a git too old to answer, is
    counted as holding work — the safe side of this question is "yes".

    `since` is where the branch started, when that is known. Only commits
    after it are the branch's own: without it, a base that was amended or
    moved backwards leaves the commit the branch was cut from looking like
    work of its own.

    With `root`, resolution is pinned to the project's trusted paths (SG-R4).
    """
    with _pinned(repo, root) as (extra, env):
        return _holds_unmerged(lambda *args: run(repo, *extra, *args, env=env),
                               head, base, since)


def _holds_unmerged(git, head: str, base: str, since: str) -> bool:
    if git("merge-base", "--is-ancestor", head, base).ok:
        return False
    args = ["merge-tree", "--write-tree"]
    if since:
        own = git("rev-list", "--count", head, "--not", base, since)
        if own.ok and own.out == "0":
            return False
        if git("merge-base", "--is-ancestor", since, head).ok:
            args.append(f"--merge-base={since}")
    merged = git(*args, base, head)
    if not merged.ok or not merged.out:
        return True
    base_tree = git("rev-parse", f"{base}^{{tree}}")
    return not base_tree.ok or merged.out.splitlines()[0] != base_tree.out


def untracked_in_the_way(worktree: Path, head: str, target: str, *,
                         root: Path | None = None) -> str:
    """A path moving `worktree` from `head` to `target` would overwrite, or "".

    Such a path is one `target` tracks and `head` does not, which exists in
    the worktree as something git is not tracking — an ignored file, most
    often — with other content than `target` gives it. `reset --keep` refuses
    an untracked file there but overwrites an ignored one. A file (or a
    directory) standing where `target` needs a directory counts too. When git
    cannot answer, the first path it could not rule out is returned.

    With `root`, resolution is pinned to the project's trusted paths (SG-R4),
    and the worktree's content is hashed with no filter: which filter applies
    is the worktree's `.gitattributes` to say, and the host runs none of its
    choosing. A path a filter would have made equal is then in the way, which
    is the side of the question that loses nothing.
    """
    with _pinned(worktree, root) as (extra, env):
        return _untracked_in_the_way(
            worktree, head, target,
            lambda *args: run(worktree, *extra, *args, env=env),
            ["--no-filters"] if root is not None else [])


def _untracked_in_the_way(worktree: Path, head: str, target: str, git,
                          hash_args: list[str]) -> str:
    diff = git("diff", "--raw", "--no-abbrev", "--no-renames", "-z", head, target)
    if not diff.ok:
        return diff.err or "git diff failed"
    fields = diff.out.split("\0")
    added: dict[str, str] = {}
    removed: set[str] = set()
    for meta, path in zip(fields[0::2], fields[1::2]):
        parts = meta.split()
        if len(parts) < 5:
            continue
        if parts[4] == "A":
            added[path] = parts[3]
        elif parts[4] == "D":
            removed.add(path)
    for path, blob in added.items():
        segments = path.split("/")
        for depth in range(1, len(segments)):
            prefix = "/".join(segments[:depth])
            spot = worktree / prefix
            if prefix not in removed and (spot.is_symlink() or
                                          (spot.exists() and not spot.is_dir())):
                return prefix
        spot = worktree / path
        if not (spot.exists() or spot.is_symlink()):
            continue
        if spot.is_symlink() or not spot.is_file():
            return path
        same = git("hash-object", *hash_args, "--", path)
        if not same.ok or same.out != blob:
            return path
    return ""


def _no_filter_args(git) -> list[str]:
    """``-c`` overrides that empty every filter driver the trusted config
    defines, so that a checkout runs none of them."""
    listed = git("config", "--name-only", "--get-regexp", r"^filter\.")
    args: list[str] = []
    names = {key.rsplit(".", 1)[0] for key in listed.out.splitlines()
             if listed.ok and key.count(".") >= 2}
    for name in sorted(names):
        args += ["-c", f"{name}.clean=", "-c", f"{name}.smudge=",
                 "-c", f"{name}.process=", "-c", f"{name}.required=false"]
    return args


def reset_keep(worktree: Path, target: str, branch: str, *, root: Path) -> GitResult:
    """Move `worktree`, on `branch`, to `target` with ``reset --keep``, from
    the host (SG-R4).

    The command writes an agent's worktree, so it runs pinned as a read is,
    with no hook, no fsmonitor and no filter: nothing the agent wrote runs.
    HEAD is checked again inside that pinning — it must still be `branch` —
    so the branch moved is the one that was checked. The branch ref moves in
    the real repository. The worktree's index is locked as git locks it, by
    creating ``index.lock`` beside it (refused if another git holds it), and
    the index git wrote replaces it by renaming that lock, never through a
    link.
    """
    root = Path(root).resolve()
    tree = Path(worktree).resolve()
    parts = () if tree == root else ("worktrees", tree.name)
    try:
        dfd = _open_beneath(root / ".git", parts)
    except OSError as exc:
        raise GitError(f"no git directory for {tree}: {exc}") from exc
    try:
        try:
            lock = os.open("index.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                           | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        except OSError as exc:
            return GitResult(False, "", f"could not lock the worktree's index "
                             f"(index.lock): {exc}", 128)
        locked = True
        try:
            with _pinned(worktree, root) as (extra, env):
                def git(*args):
                    return run(worktree, *extra, *args, env=env)

                symref = git("symbolic-ref", "-q", "HEAD")
                if not symref.ok or symref.out != f"refs/heads/{branch}":
                    return GitResult(False, "", f"the worktree is not on its branch "
                                     f"{branch!r} (HEAD is {symref.out or 'detached'})",
                                     symref.code or 1)
                moved = git(*_no_filter_args(git), "reset", "--keep", target)
                if not moved.ok:
                    return moved
                with os.fdopen(lock, "wb", closefd=False) as fh:
                    fh.write(Path(env["GIT_INDEX_FILE"]).read_bytes())
                os.replace("index.lock", "index", src_dir_fd=dfd, dst_dir_fd=dfd)
                locked = False
                return moved
        finally:
            os.close(lock)
            if locked:
                with contextlib.suppress(OSError):
                    os.unlink("index.lock", dir_fd=dfd)
    finally:
        os.close(dfd)


def diff_stat(repo: Path, branch: str, base: str, *,
              root: Path | None = None) -> str:
    result = _read(repo, root, "diff", "--stat", f"{base}...{branch}")
    return result.out if result.ok else ""


def changed_paths(repo: Path, branch: str, base: str,
                  filters: str = "MDR", *, root: Path | None = None) -> list[str]:
    """Repo-relative paths `branch` changed, restricted to those change kinds.

    The default excludes additions on purpose. A protected file that an agent
    ADDS cannot weaken anything — a new test is a new test — while modifying,
    deleting or renaming one is how a contract gets quietly edited to fit the
    code. That distinction is the whole reason this is a diff filter and not a
    filesystem permission: `chmod -w` cannot express "you may add but not
    rewrite", and this can.
    """
    result = _read(repo, root, "diff", "--name-only", f"--diff-filter={filters}",
                 f"{base}...{branch}")
    if not result.ok:
        return []
    return [line.strip() for line in result.out.splitlines() if line.strip()]


def restore_paths(worktree: Path, base: str, paths: list[str],
                  message: str, *, git: Git = HOST) -> GitResult:
    """Put these paths back to their `base` content and commit, in a worktree.

    Used to undo an agent's edits to files it was not allowed to modify, before
    its branch is merged. Runs in the agent's own worktree because that is
    where its branch is checked out; the caller has already established that
    the worktree still exists.

    SG-R3: the commit is on the agent's branch and runs its hooks, so it runs
    wherever `git` runs it — the agent's sandbox, given its executor's.
    """
    if not paths:
        return GitResult(True, "nothing to restore", "", 0)
    restore = git.run(worktree, "checkout", base, "--", *paths)
    if not restore.ok:
        return restore
    if git.run(worktree, "diff", "--cached", "--quiet").ok:
        return GitResult(True, "paths already matched base", "", 0)
    # CI-R3: this reverts an agent's edits before its branch merges, so it is
    # the merging side's own commit, not the agent's — same fallback identity
    # as `merge`'s squash commit below.
    extra = _identity_fallback_args(worktree, "multiagents",
                                    "orchestrator@multiagents.invalid", git)
    return git.run(worktree, *extra, "commit", "-m", message)


def _config_missing(worktree: Path, key: str, git: Git = HOST) -> bool:
    return not git.run(worktree, "config", "--get", key).ok


def _identity_fallback_args(worktree: Path, name: str, email: str,
                            git: Git = HOST) -> list[str]:
    """`-c` overrides for one `git commit`, filling only whatever half of the
    identity is missing (CI-R1, CI-R3 — context/specs/commit-identity.md).

    A fresh container HOME has no git identity anywhere — no config, no
    GIT_AUTHOR_*/GIT_COMMITTER_*/EMAIL — and `git commit` refuses to run.
    This scopes a fallback identity to this one invocation, never writing it
    to any config file, and only for whichever half is actually missing. A
    half that IS configured (repo, global, or env) is left alone: `-c`
    outranks config files but env vars (GIT_AUTHOR_*/GIT_COMMITTER_*) outrank
    `-c`, so an env-supplied identity passes through unchanged.
    """
    extra: list[str] = []
    if _config_missing(worktree, "user.name", git):
        extra += ["-c", f"user.name={name}"]
    # EMAIL is git's own last-resort fallback for email before it would guess
    # from passwd+hostname; a bare `-c user.email=` outranks it, so it must
    # be excluded here or we'd clobber an identity the user already has.
    if _config_missing(worktree, "user.email", git) and "EMAIL" not in git.environ():
        extra += ["-c", f"user.email={email}"]
    return extra


def commit_all(worktree: Path, message: str, *,
               role: str | None = None, agent_id: str | None = None,
               git: Git = HOST, root: Path | None = None,
               branch: str = "") -> GitResult:
    """Commit whatever an agent left uncommitted, so no work is stranded.

    CI-R1: falls back to an identity naming the agent when none is
    configured, via :func:`_identity_fallback_args`.

    CI-R6: this is the runner's own commit on an agent's branch, so it is
    never signed — the key is not in the container regardless. `-c` on the
    command line outranks every config source (files, `GIT_CONFIG_*` in the
    environment) and writes nothing to disk, so a user who wants signing
    elsewhere is unaffected.

    SG-R3: every git command here runs wherever `git` runs it. The commit runs
    the agent's hooks, so an executor with a sandbox passes its own `Git` and
    they run in there; the hook trace is written where both sides reach it.
    """
    if git is HOST and root is not None:
        with _host_scope(worktree, root=root, branch=branch):
            return _commit_all(worktree, message, role=role, agent_id=agent_id,
                               git=git, host_bookkeeping=True)
    return _commit_all(worktree, message, role=role, agent_id=agent_id, git=git)


def _filtered_paths(worktree: Path, git: Git, *, all_paths: bool = False) -> list[str]:
    commands = (["ls-files", "--cached", "--others", "--exclude-standard", "-z"]
                if all_paths else None)
    if commands is not None:
        results = [git.run(worktree, *commands, strip=False)]
    else:
        results = [git.run(worktree, "diff", "--name-only", "-z", strip=False),
                   git.run(worktree, "diff", "--cached", "--name-only", "-z", strip=False),
                   git.run(worktree, "ls-files", "--others", "--exclude-standard", "-z",
                           strip=False)]
    if any(not result.ok for result in results):
        raise GitError("could not inspect paths before host commit")
    paths = sorted({name for result in results for name in result.out.split("\0") if name})
    found: list[str] = []
    for path in paths:
        if not (worktree / path).is_file():
            continue
        result = git.run(worktree, "check-attr", "-z", "filter", "--", path,
                         strip=False)
        if not result.ok:
            raise GitError(f"could not inspect filter attribute for {path}")
        parts = result.out.split("\0")
        if len(parts) >= 3 and parts[2] not in ("unspecified", "unset", ""):
            found.append(path)
    return found


def _commit_all(worktree: Path, message: str, *, role: str | None,
                agent_id: str | None, git: Git,
                host_bookkeeping: bool = False) -> GitResult:
    if host_bookkeeping:
        filtered = _filtered_paths(worktree, git)
        if filtered:
            names = ", ".join(filtered[:50])
            return GitResult(False, "", f"refused host commit for filtered paths: {names}", 1)
    # BA-R1 (bug-ba55a9): `git add -A` resolves an unmerged entry by staging
    # the conflicted file as it stands — markers included — so the WIP commit
    # would record the conflict itself. Refuse before touching anything; once
    # the conflicts are resolved and staged, `ls-files -u` is empty and the
    # commit proceeds as usual. A merge or squash awaiting its commit without
    # unmerged entries (BA-R2) is not refused on those markers alone.
    unmerged = git.run(worktree, "ls-files", "--unmerged", "-z", strip=False)
    if not unmerged.ok:
        # A check git could not run is not a clean index: committing on would
        # be the original bug. Refuse before staging; the next call retries.
        return GitResult(False, "", f"refused to commit: the unmerged-entry "
                         f"check failed: {unmerged.err or unmerged.out}",
                         unmerged.code or 1)
    if unmerged.out:
        names = sorted({line.split("\t", 1)[1]
                        for line in unmerged.out.split("\0") if line})
        listed = ", ".join(names[:20]) + (", ..." if len(names) > 20 else "")
        return GitResult(False, "", f"refused to commit: the index has unmerged "
                         f"entries ({listed}); resolve the conflicts and stage "
                         f"them first", 1)
    # CI-R2: a failed `git add` is a failed commit. Ignored, it left the work
    # unstaged and the staged diff empty, which then read as a clean tree.
    added = git.run(worktree, "add", "-A")
    if not added.ok:
        return added
    if git.run(worktree, "diff", "--cached", "--quiet").ok:
        return GitResult(True, "nothing to commit", "", 0)

    extra = _identity_fallback_args(
        worktree,
        f"multiagents {role}" if role else "multiagents",
        f"{agent_id or 'agent'}@multiagents.invalid",
        git,
    )
    # H13: no auto gc/maintenance after the commit — it tries `packed-refs.lock`
    # and prints an error although the commit landed.
    args = ("-c", "commit.gpgsign=false", "-c", "gc.auto=0",
            "-c", "maintenance.auto=false", *extra, "commit", "-m", message)
    hooks = [] if host_bookkeeping else _active_commit_hooks(worktree, git)
    if not hooks:
        return git.run(worktree, *args)
    # CI-R5: whether a hook is what refused it. Git prints nothing of its own
    # when a hook fails, so the evidence is its trace: a hook child that
    # exited non-zero. A git too old to trace falls back to the hook's
    # presence alone.
    with git.scratch() as tmp:
        trace = Path(tmp) / "trace2.json"
        result = git.run(worktree, *args, env={"GIT_TRACE2_EVENT": str(trace)})
        if not result.ok:
            result.hook = _refusing_hook(trace, hooks)
    return result


# The hooks that can refuse a `git commit`. `post-commit` runs after the commit
# exists and cannot.
COMMIT_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg")


def _active_commit_hooks(worktree: Path, git: Git = HOST) -> list[str]:
    """The commit hooks git would run here: executable files in the hooks
    directory (`core.hooksPath` if set, else the repository's own, which a
    linked worktree shares). A `.sample` file, or a hook without its
    executable bit, is ignored by git and so here too."""
    found = git.run(worktree, "rev-parse", "--git-path", "hooks")
    if not found.ok or not found.out:
        return []
    hooks_dir = Path(os.path.expanduser(found.out))
    if not hooks_dir.is_absolute():
        hooks_dir = worktree / hooks_dir
    return [name for name in COMMIT_HOOKS
            if (hooks_dir / name).is_file() and os.access(hooks_dir / name, os.X_OK)]


def _refusing_hook(trace: Path, hooks: list[str]) -> str:
    """The hook a traced `git commit` ran that exited non-zero, or "".

    Only the top-level git's own events count: a hook that runs git itself
    writes its children's events to the same file, under a nested `sid`.
    """
    lines = _read_trace(trace).splitlines()
    started: dict[int, str] = {}
    traced = False
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if "/" in str(event.get("sid", "")):
            continue
        traced = True
        if event.get("event") == "child_start" and event.get("child_class") == "hook":
            argv = event.get("argv") or [""]
            started[event.get("child_id")] = (event.get("hook_name")
                                              or Path(str(argv[0])).name)
        elif event.get("event") == "child_exit" and event.get("child_id") in started \
                and event.get("code") != 0:
            return started[event["child_id"]]
    return "" if traced else hooks[0]


# CI-R7: a trace bigger than this is not read at all. The hook writes into it,
# so its size is the agent's to choose.
TRACE_MAX_BYTES = 4 * 1024 * 1024


def _read_trace(trace: Path) -> str:
    """The trace's contents, or "" for "no trace" (CI-R7).

    The hook ran with this path in its environment, so it may have replaced
    the file with anything: a FIFO (a blocking read never returns), a device,
    a symlink, or a flood. Only a regular file within `TRACE_MAX_BYTES` is
    read, opened non-blocking and without following a link; anything else is
    "no trace", which falls back to the hook's presence.
    """
    try:
        fd = os.open(trace, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        chunks: list[bytes] = []
        size = 0
        while size <= TRACE_MAX_BYTES:
            chunk = os.read(fd, min(1 << 20, TRACE_MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > TRACE_MAX_BYTES:
            return ""
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


@contextlib.contextmanager
def _base_hooks(repo: Path):
    """`-c` args that make git run the hooks directory as it is right now.

    SG-R5: a merge runs the base's hooks, never the branch's. A hooks
    directory inside the working tree (`core.hooksPath=.hooks`) is changed
    by the merge itself before its commit runs them, so it is copied aside
    first — whole, helpers next to a hook included — and git is pointed at
    the copy. The caller has checked the tree is clean, so the copy is the
    base's HEAD content. A hooks directory outside the tree is left alone:
    the merge does not change it. A hook that reaches files outside its own
    directory by a relative path does not find them in the copy.
    """
    found = run(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
    if not found.ok or not found.out:
        yield []
        return
    hooks = Path(os.path.expanduser(found.out))
    if not hooks.is_absolute():
        hooks = Path(repo) / hooks
    top = Path(repo_root(repo) or repo)
    lexical = Path(os.path.abspath(hooks))
    inside = (lexical.is_relative_to(Path(os.path.abspath(top)))
              or hooks.resolve().is_relative_to(top.resolve()))
    if not inside:
        yield []
        return
    with tempfile.TemporaryDirectory(prefix="multiagents-hooks-") as tmp:
        copy = Path(tmp) / "hooks"
        if hooks.is_dir():
            shutil.copytree(hooks, copy, ignore_dangling_symlinks=True)
        else:
            copy.mkdir()
        yield ["-c", f"core.hooksPath={copy}"]


def _undo_merge(repo: Path, hooks: list[str]) -> None:
    """Put a clean base checkout back as it was before a merge that failed:
    no merge in progress, nothing staged, tracked files at HEAD. Only for a
    tree that was clean when the merge started, which `merge` checks."""
    run(repo, *hooks, "merge", "--abort")
    run(repo, *hooks, "reset", "--hard", "--quiet")
    squash_msg = run(repo, "rev-parse", "--path-format=absolute",
                     "--git-path", "SQUASH_MSG")
    if squash_msg.ok and squash_msg.out:
        Path(squash_msg.out).unlink(missing_ok=True)


def merge(repo: Path, branch: str, message: str, style: str = "squash", *,
          root: Path | None = None, host_hooks: bool = False,
          host_content_programs: bool = False,
          target_branch: str = "") -> tuple[str, str]:
    """Merge `branch` into whatever `repo` currently has checked out.

    Returns ``(status, detail)`` where status is ``merged``, ``empty``,
    ``conflict`` or ``failed``. A conflict is aborted cleanly and reported —
    the branch survives so the caller can decide what to do with it.

    SG-R5: the hooks git runs are the base's, taken before the merge (see
    :func:`_base_hooks`). A merge a hook refuses leaves the base checkout as
    it was: HEAD unchanged, nothing staged, the working tree restored.
    """
    try:
        with _host_scope(repo, root=root, hooks=host_hooks,
                         content=host_content_programs, branch=target_branch):
            if is_dirty(repo):
                return "failed", "target worktree has uncommitted changes; commit or stash first"
            if host_hooks:
                with _base_hooks(repo) as hooks:
                    return _merge(repo, branch, message, style, hooks)
            return _merge(repo, branch, message, style, [])
    except (OSError, GitError) as exc:
        return "failed", f"could not set the base's hooks aside: {exc}"


def _merge(repo: Path, branch: str, message: str, style: str,
           hooks: list[str]) -> tuple[str, str]:
    if style == "squash":
        result = run(repo, *hooks, "merge", "--squash", branch, timeout=300)
        if not result.ok:
            _undo_merge(repo, hooks)
            return "conflict", result.err or result.out
        if run(repo, "diff", "--cached", "--quiet").ok:
            return "empty", "branch introduced no changes"
        # CI-R3: same fallback as commit_all, but named for the merging side
        # rather than the agent — this commit is multiagents', not theirs.
        extra = _identity_fallback_args(repo, "multiagents", "orchestrator@multiagents.invalid")
        commit = run(repo, *hooks, *extra, "commit", "-m", message, timeout=120)
        if not commit.ok:
            _undo_merge(repo, hooks)
            return "failed", commit.err or commit.out
        return "merged", commit.out

    extra = _identity_fallback_args(repo, "multiagents", "orchestrator@multiagents.invalid")
    result = run(repo, *hooks, *extra, "merge", "--no-ff", "-m", message, branch, timeout=300)
    if result.ok:
        return "merged", result.out
    _undo_merge(repo, hooks)
    return "conflict", result.err or result.out


def push(repo: Path, remote: str, branch: str) -> GitResult:
    """Push a branch. Only ever called explicitly — never as a side effect of
    finishing a run, because publishing is not reversible."""
    if not remote:
        return GitResult(False, "", "no remote configured (git.remote is empty)", 1)
    with _host_scope(repo, content=True):
        return run(repo, "push", "-u", remote, branch, timeout=600)
