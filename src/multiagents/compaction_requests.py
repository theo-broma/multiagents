"""OC: durable requests and driver bindings in host-owned project state.

The request journal survives driver replacement; unlike the safe-point barrier,
it does not use the driver's ephemeral signing key. Agent containers never
receive this directory. Every update is locked, atomic and durable.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid

from . import procs
from .paths import state_root
from .tree import Tree

ACTIVE = {"pending", "executing", "compacted"}
MODES = ("restart", "in_session")


def mode(provider):
    value = (getattr(provider, "compaction", {}) or {}).get("mode")
    return value if isinstance(value, str) and value in MODES else None


class Requests:
    def __init__(self, paths):
        self.paths = paths
        self.directory = state_root() / "compaction-requests" / paths.slug

    @contextlib.contextmanager
    def _journal(self):
        base = state_root()
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in ("compaction-requests", self.paths.slug):
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    source = os.open("requests.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    data = {"drivers": {}, "requests": []}
                else:
                    with os.fdopen(source) as stream:
                        data = json.load(stream)
                before = json.dumps(data)
                yield data
                if json.dumps(data) != before:
                    name = ".requests-" + uuid.uuid4().hex
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                    try:
                        with os.fdopen(out, "w") as stream:
                            json.dump(data, stream)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.rename(name, "requests.json", src_dir_fd=fd, dst_dir_fd=fd)
                        os.fsync(fd)
                    finally:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(name, dir_fd=fd)
            finally:
                os.close(lock)
        finally:
            os.close(fd)

    def bind(self, role, pid, *, session="", supervised=True):
        with self._journal() as data:
            data["drivers"][role] = {"pid": pid, "start": procs.start_time(pid),
                                      "session": session, "supervised": supervised}

    def unbind(self, role):
        with self._journal() as data:
            data["drivers"].pop(role, None)

    def supported(self, role, session):
        with self._journal() as data:
            binding = data["drivers"].get(role)
        if not binding or not binding.get("supervised"):
            return False
        if binding.get("session") and binding["session"] != session:
            return False
        return bool(binding.get("start") and procs.living(binding["pid"], binding["start"]))

    def status(self, role, session, request_id=""):
        with self._journal() as data:
            found = next((r.copy() for r in reversed(data["requests"])
                          if r["role"] == role and r["session"] == session
                          and (not request_id or r["request_id"] == request_id)), None)
        return found or {"status": "none"}

    def request(self, role, session, compaction_mode, reason, boundary):
        with self._journal() as data:
            found = next((r for r in reversed(data["requests"])
                          if r["role"] == role and r["session"] == session
                          and r["status"] in ACTIVE), None)
            if found:
                return found.copy()
            found = {"request_id": uuid.uuid4().hex, "role": role, "session": session,
                     "mode": compaction_mode, "reason": reason[:500], "status": "pending",
                     "requested_at": time.time(), "boundary": boundary}
            data["requests"].append(found)
            self._event(found, "requested")
            return found.copy()

    def update(self, request_id, status, **fields):
        with self._journal() as data:
            record = next(r for r in data["requests"] if r["request_id"] == request_id)
            changed = record["status"] != status
            record.update(status=status, **fields)
            if changed:
                self._event(record, {"executing": "started"}.get(status, status))
            return record.copy()

    def _event(self, record, phase):
        Tree(self.paths.tree_file, self.paths.events_file).emit(
            record["role"], "compaction_" + phase, request_id=record["request_id"],
            session=record["session"], mode=record["mode"],
            reason=str(record.get("failure_detail") or record["reason"])[:500])
