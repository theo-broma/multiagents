"""Durable host-owned operands for operations on agent branches and worktrees."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from .paths import ProjectPaths, state_root
from .tree import Node, Tree


class HostAuthority:
    @staticmethod
    def directory_for(paths: ProjectPaths) -> Path:
        return state_root() / "host-authority" / paths.slug

    def __init__(self, paths: ProjectPaths, tree: Tree):
        self.paths = paths
        self.directory = self.directory_for(paths)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.file = self.directory / "nodes.json"
        with (self.directory / "lock").open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.file.exists():
                # A failed tree read leaves no marker. Retry the whole seed next time.
                nodes = tree.read()["nodes"]
                seeded = {key: self._entry(raw, True) for key, raw in nodes.items()
                          if isinstance(raw, dict) and (raw.get("branch") or raw.get("worktree"))}
                self._write(seeded)

    @staticmethod
    def _entry(node: Node | dict, seeded: bool = False) -> dict:
        get = node.get if isinstance(node, dict) else lambda key: getattr(node, key)
        branch, worktree = get("branch") or "", get("worktree") or ""
        return {key: get(key) for key in ("id", "agent", "parent")} | {
            "branch": branch, "worktree": worktree,
            "branches": [branch] if branch else [],
            "worktrees": [worktree] if worktree else [],
            "seeded": seeded, "completion": None}

    def read(self) -> dict:
        return json.loads(self.file.read_text())

    def _write(self, records: dict) -> None:
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".nodes-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(records, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.file)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def add(self, node: Node) -> None:
        with (self.directory / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.read()
            if node.id in records:
                raise ValueError(f"host authority already records {node.id}")
            records[node.id] = self._entry(node)
            self._write(records)

    def complete(self, node_id: str, status: str, commit: str = "") -> None:
        with (self.directory / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.read()
            if node_id in records:
                from .tree import now
                records[node_id]["completion"] = {"status": status, "time": now(),
                                                   "commit": commit}
                self._write(records)

    def rebind(self, node_id: str, branch: str, worktree: Path) -> None:
        """Record a host-created replacement checkout before exposing it."""
        with (self.directory / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.read()
            record = records[node_id]
            record["branch"] = branch
            record["worktree"] = str(worktree)
            record["completion"] = None
            for key, value in (("branches", branch), ("worktrees", str(worktree))):
                if value and value not in record[key]:
                    record[key].append(value)
            self._write(records)

    def clear(self, node_id: str, *, branch: bool = False,
              worktree: bool = False) -> None:
        """Keep host-authorised deletion reflected in the current operands."""
        with (self.directory / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = self.read()
            if node_id in records:
                if branch:
                    records[node_id]["branch"] = ""
                if worktree:
                    records[node_id]["worktree"] = ""
                self._write(records)

    def get(self, node_id: str) -> dict | None:
        return self.read().get(node_id)

    def owns_branch(self, branch: str) -> bool:
        return any(branch in r.get("branches", [r.get("branch")])
                   for r in self.read().values())

    def safe_unrecorded_branch(self, branch: str) -> bool:
        from . import gitops
        return (branch.startswith("agents/") and
                bool(branch.removeprefix("agents/")) and
                gitops.run(self.paths.root, "check-ref-format",
                           f"refs/heads/{branch}").ok and
                not self.owns_branch(branch))

    def owns_worktree(self, path: Path) -> bool:
        return any(str(path) in r.get("worktrees", [r.get("worktree")])
                   for r in self.read().values())

    def safe_nested_path(self, path: Path) -> bool:
        root = self.paths.worktrees.resolve()
        resolved = path.resolve()
        if root not in resolved.parents:
            return False
        for record in self.read().values():
            for held in record.get("worktrees", [record.get("worktree")]):
                if not held:
                    continue
                recorded = Path(held).resolve()
                if (resolved == recorded or recorded in resolved.parents
                        or resolved in recorded.parents):
                    return False
        return True

    def safe_seeded_path(self, path: Path) -> bool:
        return self.paths.worktrees.resolve() in path.resolve().parents

    @contextlib.contextmanager
    def pinned_worktree(self, path: Path):
        """Keep the exact recorded checkout open across a host git operation."""
        root = self.paths.worktrees.resolve()
        parts = path.absolute().relative_to(self.paths.worktrees.absolute()).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise OSError("worktree is outside the project worktree root")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        opened = []
        try:
            fd = os.open(root, flags)
            opened.append(fd)
            for part in parts:
                fd = os.open(part, flags, dir_fd=fd)
                opened.append(fd)
            yield Path(f"/proc/{os.getpid()}/fd/{fd}")
        finally:
            for fd in reversed(opened):
                os.close(fd)

    @contextlib.contextmanager
    def pinned_parent(self, path: Path):
        """Give a pathname whose parent cannot be swapped during a move."""
        root = self.paths.worktrees.resolve()
        parts = path.absolute().relative_to(self.paths.worktrees.absolute()).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise OSError("worktree is outside the project worktree root")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        opened = []
        try:
            fd = os.open(root, flags)
            opened.append(fd)
            for part in parts[:-1]:
                fd = os.open(part, flags, dir_fd=fd)
                opened.append(fd)
            yield Path(f"/proc/{os.getpid()}/fd/{fd}") / parts[-1]
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def remove_worktree(self, path: Path, *, recorded: bool = False) -> bool:
        """Remove an authorised checkout without resolving an attacker-swapped link.

        Every walk and unlink is relative to an open directory descriptor. A
        symlink in the path is refused; symlinks *inside* the checkout are
        unlinked as entries, never followed. Git's stale registration is pruned
        only after the anchored removal succeeds.
        """
        root = self.paths.worktrees.resolve()
        try:
            parts = path.absolute().relative_to(self.paths.worktrees.absolute()).parts
        except ValueError:
            return False
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return False
        if recorded:
            if not self.owns_worktree(path):
                return False
        elif self.owns_worktree(path):
            return False

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            root_fd = os.open(root, flags)
        except OSError:
            return False
        try:
            parent_fd = root_fd
            opened = []
            try:
                for part in parts[:-1]:
                    child_fd = os.open(part, flags, dir_fd=parent_fd)
                    opened.append(child_fd)
                    parent_fd = child_fd
                target_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
                try:
                    self._remove_contents(target_fd, os.fstat(root_fd).st_dev)
                finally:
                    os.close(target_fd)
                os.rmdir(parts[-1], dir_fd=parent_fd)
            finally:
                for fd in reversed(opened):
                    os.close(fd)
        except OSError:
            return False
        finally:
            os.close(root_fd)

        from . import gitops
        gitops.run(self.paths.root, "worktree", "prune", "--expire", "now")
        return True

    @classmethod
    def _remove_contents(cls, fd: int, device: int) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for name in os.listdir(fd):
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child_fd = os.open(name, flags, dir_fd=fd)
                try:
                    if os.fstat(child_fd).st_dev != device:
                        raise OSError("worktree contains a different filesystem")
                    cls._remove_contents(child_fd, device)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
