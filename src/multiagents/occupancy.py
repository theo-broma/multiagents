"""Which runs are alive in each container — host-owned, shared by every server.

LN-C5 attributes a container-wide OOM kill to a run only when that run was
the container's only occupant while it lived. Every agent's server is its own
process, so "who is in there" cannot live in one Runner's memory: a sibling
started by a second server over the same project is still a sibling, and a
run adopted after a server restart is still an occupant (review finding 1).

The record therefore lives in the protected host-state directory H7's
startup health already uses, under its own lock, and is reconciled against
process liveness so a crashed server leaves no ghost occupant: an entry goes
when both its run's process and the server that recorded it are gone.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from typing import Iterator

from . import procs
from .authority import HostAuthority
from .paths import ProjectPaths
from .tree import now


class ContainerOccupancy:
    def __init__(self, paths: ProjectPaths):
        self.directory = HostAuthority.directory_for(paths)
        self.file = self.directory / "container-runs.json"
        self.lock_path = self.directory / "container-runs.lock"
        self.memory: dict | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
        except OSError:
            self.memory = {}

    # ------------------------------------------------------------------ io --

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict]:
        if self.memory is None:
            try:
                with self.lock_path.open("a+b") as lock:
                    os.fchmod(lock.fileno(), 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield self.read()
                    finally:
                        with contextlib.suppress(OSError):
                            fcntl.flock(lock, fcntl.LOCK_UN)
                return
            except OSError:
                self.memory = {}
        yield self.memory

    def read(self) -> dict:
        if self.memory is not None:
            return self.memory
        try:
            records = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}
        return records if isinstance(records, dict) else {}

    def commit(self, records: dict) -> None:
        if self.memory is not None:
            return
        try:
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=".occupancy-")
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(records, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.file)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
        except OSError:
            self.memory = records

    # ------------------------------------------------------------ records --

    def _reconcile(self, records: dict) -> None:
        """Drop entries neither of whose principals can vouch for them."""
        for container, runs in records.items():
            if not isinstance(runs, dict):
                continue
            for node_id, run in list(runs.items()):
                try:
                    live = (procs.alive(run.get("pid"), run.get("pid_start", ""))
                            or procs.alive(run.get("owner_pid"),
                                           run.get("owner_start", "")))
                except (TypeError, ValueError):
                    live = False
                if not live:
                    del runs[node_id]

    def _entry(self, pid: int, pid_start: str) -> dict:
        return {"pid": pid, "pid_start": pid_start,
                "owner_pid": os.getpid(), "owner_start": procs.start_time(os.getpid()),
                "since": now()}

    def register(self, container: str, node_id: str, pid: int,
                 pid_start: str = "") -> None:
        """Record that `node_id`'s run is live in `container`, owned by this
        server. A host still draining a dead run keeps its entry until it
        finalises, so `others` stays conservative."""
        with self.locked() as records:
            runs = records.setdefault(container, {})
            if not isinstance(runs, dict):
                runs = records[container] = {}
            self._reconcile(records)
            runs[node_id] = self._entry(pid, pid_start)
            self.commit(records)

    def rebind(self, container: str, node_id: str, pid: int,
               pid_start: str = "") -> None:
        """Adoption: the run is still the occupant it was, but this server
        speaks for it now (finding 2)."""
        with self.locked() as records:
            runs = records.get(container)
            if not isinstance(runs, dict) or node_id not in runs:
                runs = records.setdefault(container, {})
            self._reconcile(records)
            runs[node_id] = self._entry(pid, pid_start)
            self.commit(records)

    def forget(self, container: str, node_id: str) -> None:
        """The run ended here: it no longer occupies anything."""
        with self.locked() as records:
            runs = records.get(container)
            if not isinstance(runs, dict) or node_id not in runs:
                return
            self._reconcile(records)
            runs.pop(node_id, None)
            self.commit(records)

    def others(self, container: str, node_id: str) -> bool:
        """Is any run other than `node_id` live in `container`? Fails closed:
        with no usable record the answer is yes, and LN-C5's attribution —
        the one thing this feeds — stays `kill_uncertain`."""
        with self.locked() as records:
            if self.memory is not None:
                return True
            self._reconcile(records)
            return any(other != node_id for other in records.get(container, {}))
