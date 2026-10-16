"""Host-owned isolated conversation payloads (IC-C22..IC-C24).

Only scheduler-issued handles address these stores. An attempt HOME is a
projection of the declared payload, never a provider profile or a store handle.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .. import gitops


class ConversationError(RuntimeError):
    def __init__(self, reason, outcome="failed"):
        super().__init__(reason)
        self.reason, self.outcome = reason, outcome


def _owner(attempt):
    if attempt.get("alias_id"):
        return "alias", attempt["alias_id"]
    return "node", attempt["node_id"]


def _key(kind, identity):
    return hashlib.sha256((kind + ":" + identity).encode()).hexdigest()


def _directory(paths, attempt):
    return paths.scheduler / "conversations" / _key(*_owner(attempt))


@contextmanager
def _locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(directory, name):
    raw = gitops._read_beneath(directory, (), name, 4 * 1024 * 1024)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ConversationError("corrupt conversation metadata") from exc
    if not isinstance(result, dict):
        raise ConversationError("corrupt conversation metadata")
    return result


def _write(directory, name, value):
    gitops._write_beneath(directory, (), name,
                         json.dumps(value, sort_keys=True).encode(), create=True)


def _binding(attempt, provider, config=None):
    bound = attempt.get("binding") or {}
    declaration = config.providers.get(provider.name, {}) if config else {}
    family = (declaration.get("family") or declaration.get("extends")
              or getattr(provider, "family", "") or provider.name)
    return {"provider": provider.name, "family": family,
            "account": provider.container_account or provider.name,
            "model": bound.get("model") or attempt.get("model", "")}


def _changes(old, new):
    return next((field + " changed" for field in
                 ("provider", "family", "account", "model")
                 if old.get(field, new[field]) != new[field]), "")


def incompatibility(paths, node, config):
    """Compare a node's requested route to its ordinary conversation binding."""
    directory = _directory(paths, {"node_id": node["id"]})
    if not directory.is_dir():
        return ""
    current = _read(directory, "active.json")
    if not current or not current.get("session_id"):
        return ""
    spec = config.agents.get(node.get("agent"))
    pins = node.get("pins") or {}
    old = current["binding"]
    provider = pins.get("provider") or (spec.provider if spec else old["provider"])
    declaration = config.providers.get(provider, {})
    requested = {"provider": provider,
                 "family": declaration.get("family") or declaration.get("extends") or provider,
                 "account": declaration.get("container_account") or provider,
                 "model": pins.get("model") or (spec.model if spec else old["model"])}
    return _changes(old, requested)


def _patterns(provider, session_id):
    store = provider.session_store or {}
    glob = store.get("glob") or "**"
    glob = glob.replace("{session_id}", session_id or "*")
    forbidden = store.get("forbidden_profile") or []
    if isinstance(forbidden, str):
        forbidden = [forbidden]
    return glob, forbidden


def _forbidden(relative, root, patterns):
    names = (relative, root + "/" + relative)
    # These are profile/endpoint material, never conversation payloads.
    if any(part in {".ssh", ".aws", ".docker", "rpc.sock", "auth.json",
                    "credentials.json", ".env"} for part in Path(relative).parts):
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns for name in names)


def _selected(relative, pattern):
    return (fnmatch.fnmatchcase(relative, pattern)
            or (pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:])))


def _copy(stream, output):
    """Keep sparse guest files sparse, with bounded buffers for data extents."""
    size = os.fstat(stream.fileno()).st_size
    position = 0
    while position < size:
        try:
            start = os.lseek(stream.fileno(), position, os.SEEK_DATA)
            if start >= size:
                break
            end = min(os.lseek(stream.fileno(), start, os.SEEK_HOLE), size)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break  # The rest of the file is a hole.
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
            # Filesystems without extent queries still use bounded reads.
            stream.seek(0)
            output.seek(0)
            shutil.copyfileobj(stream, output, length=1024 * 1024)
            output.truncate()
            return
        output.seek(start)
        remaining = end - start
        while remaining:
            chunk = os.pread(stream.fileno(), min(remaining, 1024 * 1024), end - remaining)
            if not chunk:
                raise ConversationError("conversation payload changed during copy")
            output.write(chunk)
            remaining -= len(chunk)
        position = end
    output.truncate(size)


@contextmanager
def _payload(source, path, provider, session_id, *, scratch):
    """Spool regular files on disk; guest sizes and depth do not grow the stack
    or require keeping file contents in scheduler memory.
    """
    pattern, forbidden = _patterns(provider, session_id)
    copied, refused = {}, []
    base, parts = gitops.beneath(Path(source))
    parts += Path(path).parts
    try:
        fd = gitops._open_beneath(base, parts)
    except OSError:
        yield copied, refused
        return
    try:
        # Flat, host-owned scratch files also keep cleanup independent of the
        # guest's directory depth. Scratch is on the conversation store's disk.
        with tempfile.TemporaryDirectory(prefix=".payload-", dir=scratch) as temporary:
            pending = [((), os.dup(fd))]
            try:
                while pending:
                    prefix, directory = pending.pop()
                    if directory is None:
                        directory = gitops._open_beneath(Path(f"/proc/self/fd/{fd}"), prefix)
                    try:
                        children = []
                        for name in sorted(os.listdir(directory)):
                            relative = "/".join((*prefix, name))
                            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                            if _forbidden(relative, path, forbidden):
                                refused.append(path + "/" + relative)
                            elif stat.S_ISDIR(info.st_mode):
                                children.append(name)
                            elif not stat.S_ISREG(info.st_mode):
                                refused.append(path + "/" + relative)
                            elif _selected(relative, pattern):
                                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                                dir_fd=directory)
                                with os.fdopen(child, "rb") as stream:
                                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                        raise ConversationError("unsafe conversation payload: " + relative)
                                    target = Path(temporary) / str(len(copied))
                                    with target.open("wb") as output:
                                        _copy(stream, output)
                                    copied[relative] = target
                        # Descend directly, keeping only the root and current
                        # descriptors. Siblings are reopened through the root;
                        # a deep chain needs neither recursion nor depth fds.
                        for name in reversed(children[1:]):
                            pending.append(((*prefix, name), None))
                        if children:
                            child = os.open(children[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                            dir_fd=directory)
                            pending.append(((*prefix, children[0]), child))
                    finally:
                        os.close(directory)
            except OSError as exc:
                raise ConversationError("unreadable conversation payload: " + str(exc)) from exc
            finally:
                for _, directory in pending:
                    if directory is not None:
                        os.close(directory)
            yield copied, refused
    finally:
        os.close(fd)


def _install(destination, path, files):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    base, parts = gitops.beneath(destination)
    for relative, source in files.items():
        entry = Path(path) / relative
        directory = gitops._open_beneath(base, parts + entry.parts[:-1], create=True)
        temporary = ".payload-" + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as output, source.open("rb") as stream:
                _copy(stream, output)
            os.replace(temporary, entry.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


def _generation(directory, handle):
    if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{32}", handle):
        raise ConversationError("invalid conversation handle")
    return directory / handle


def prepare(paths, attempt, provider, *, home, config=None):
    """Project declared payload into a fresh HOME, returning resume intent."""
    declaration = provider.session
    if declaration["mode"] == "none":
        return {"session_id": "", "outcome": "unsupported", "reason": "session mode none",
                "handle": ""}
    directory = _directory(paths, attempt)
    binding = _binding(attempt, provider, config)
    with _locked(directory):
        current = _read(directory, "active.json")
        if attempt.get("new_session") and (not current or current.get("attempt_id") != attempt["attempt_id"]):
            current = None  # Existing generation remains an archive until disposal.
        requested = "" if attempt.get("new_session") else attempt.get("session_id") or ""
        if current:
            reason = _changes(current["binding"], binding)
            if reason:
                raise ConversationError(reason, "unsupported")
            requested = current.get("session_id") or requested
        if not current:
            current = {"handle": uuid.uuid4().hex, "binding": binding,
                       "owner": list(_owner(attempt)), "session_id": requested,
                       "attempt_id": attempt["attempt_id"], "declaration": declaration,
                       "status": "fresh"}
            generation = _generation(directory, current["handle"])
            generation.mkdir(mode=0o700)
            if requested and attempt.get("legacy_home"):
                with _payload(attempt["legacy_home"], declaration["path"], provider, requested,
                              scratch=directory) as (files, _):
                    _install(generation, declaration["path"], files)
                    current["status"] = "exported" if files else "missing"
            _write(directory, "active.json", current)
            _write(generation, "metadata.json", current)
        handle = current["handle"]
        attempt["conversation_handle"] = handle
        generation = _generation(directory, handle)
        if requested:
            if current.get("status") == "quarantined":
                raise ConversationError("quarantined conversation export: " + ", ".join(current.get("forbidden", [])))
            payload = (_generation(generation / ".exports", current["payload_handle"])
                       if current.get("payload_handle") else generation)
            with _payload(payload, declaration["path"], provider, requested,
                          scratch=directory) as (files, forbidden):
                if forbidden:
                    raise ConversationError("forbidden conversation payload: " + ", ".join(forbidden))
                if not files:
                    raise ConversationError("missing conversation payload")
                _install(home, declaration["path"], files)
        return {"session_id": requested, "outcome": None if requested else "fresh",
                "reason": "", "handle": handle}


def finish(paths, attempt, provider, session_id, *, home):
    """Export before resource removal; quarantine forbidden content by name."""
    handle = attempt.get("conversation_handle")
    if not handle or provider.session["mode"] == "none":
        return {"status": "unsupported", "files": [], "forbidden": [], "handle": ""}
    directory = _directory(paths, attempt)
    with _locked(directory):
        generation = _generation(directory, handle)
        metadata = _read(generation, "metadata.json")
        if not metadata or metadata.get("owner") != list(_owner(attempt)):
            raise ConversationError("conversation handle does not belong to attempt")
        session_id = session_id or metadata.get("session_id") or ""
        path = provider.session["path"]
        exports = generation / ".exports"
        exports.mkdir(mode=0o700, exist_ok=True)
        payload_handle = uuid.uuid4().hex
        payload = _generation(exports, payload_handle)
        # Neither manifest names this snapshot until every file is installed.
        # An interrupted copy leaves the previous snapshot and metadata intact.
        with tempfile.TemporaryDirectory(prefix=".export-", dir=exports) as temporary:
            with _payload(home, path, provider, session_id, scratch=directory) as (files, forbidden):
                _install(temporary, path, files)
            status = "quarantined" if forbidden else "exported" if files else "missing"
            metadata.update(session_id=session_id, status=status, forbidden=forbidden,
                            attempt_id=attempt["attempt_id"], files=sorted(files),
                            payload_handle=payload_handle)
            os.replace(temporary, payload)
        # Each atomic manifest selects an immutable, complete snapshot. If the
        # second write is interrupted, active.json still selects the old one;
        # the retained attempt can safely export again before removal.
        _write(generation, "metadata.json", metadata)
        active = _read(directory, "active.json")
        if active and active.get("handle") == handle:
            _write(directory, "active.json", metadata)
        return {"status": status, "files": sorted(files), "forbidden": forbidden, "handle": handle}


def dispose(paths, *, node_ids=(), alias_ids=()):
    """Remove current and archived payloads with their conversation owner."""
    root = paths.scheduler / "conversations"
    for kind, identities in (("node", node_ids), ("alias", alias_ids)):
        for identity in identities:
            directory = root / _key(kind, identity)
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)
