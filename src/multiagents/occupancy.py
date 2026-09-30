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
        """Keep every entry a live run could have overlapped, drop the rest.

        An entry is not deleted the moment its principals die: it is stamped
        `ended` and kept, because LN-C5 asks whether any sibling was alive
        *during* a run's life, not only at the moment of its kill (adversary
        finding 1). It goes once no live run started before it ended — from
        then on no run, live or future, can ever have shared the container
        with it. An entry whose shape is not a mapping stays untouched: it
        cannot be proven dead, and dropping it would answer "alone" for a
        record nobody vouches for (adversary finding 2).
        """
        stamp = now()
        for runs in records.values():
            if not isinstance(runs, dict):
                continue
            live_since: list[float] = []
            for run in runs.values():
                if not isinstance(run, dict):
                    continue
                if run.get("ended") is None:
                    try:
                        live = (procs.alive(run.get("pid"), run.get("pid_start", ""))
                                or procs.alive(run.get("owner_pid"),
                                               run.get("owner_start", "")))
                    except (TypeError, ValueError):
                        live = False
                    if not live:
                        run["ended"] = stamp
                if run.get("ended") is None:
                    since = run.get("since")
                    live_since.append(since if isinstance(since, (int, float))
                                      and not isinstance(since, bool) else 0.0)
            floor = min(live_since) if live_since else None
            for node_id, run in list(runs.items()):
                if not isinstance(run, dict) or run.get("ended") is None:
                    continue
                ended = run.get("ended")
                ended_at = ended if isinstance(ended, (int, float)) \
                    and not isinstance(ended, bool) else None
                if floor is None or (ended_at is not None and ended_at < floor):
                    del runs[node_id]

    def _entry(self, pid: int, pid_start: str) -> dict:
        return {"pid": pid, "pid_start": pid_start,
                "owner_pid": os.getpid(), "owner_start": procs.start_time(os.getpid()),
                "since": now()}

    def register(self, container: str, node_id: str, pid: int,
                 pid_start: str = "") -> dict:
        """Record that `node_id`'s run is live in `container`, owned by this
        server. A host still draining a dead run keeps its entry until it
        finalises, so `others` stays conservative. Returns the entry, so the
        caller can name the interval it was live for."""
        with self.locked() as records:
            runs = records.setdefault(container, {})
            if not isinstance(runs, dict):
                runs = records[container] = {}
            self._reconcile(records)
            entry = self._entry(pid, pid_start)
            runs[node_id] = entry
            self.commit(records)
            return dict(entry)

    def rebind(self, container: str, node_id: str, pid: int,
               pid_start: str = "") -> dict:
        """Adoption: the run is still the occupant it was, but this server
        speaks for it now (finding 2)."""
        with self.locked() as records:
            runs = records.get(container)
            if not isinstance(runs, dict) or node_id not in runs:
                runs = records.setdefault(container, {})
            self._reconcile(records)
            entry = self._entry(pid, pid_start)
            runs[node_id] = entry
            self.commit(records)
            return dict(entry)

    def forget(self, container: str, node_id: str) -> None:
        """The run ended here. Its entry is stamped `ended` rather than
        dropped, so a sibling killed later can still see that it overlapped
        this one while both lived (adversary finding 1); `_reconcile` prunes
        it once no live run can have overlapped it."""
        with self.locked() as records:
            runs = records.get(container)
            if not isinstance(runs, dict) or node_id not in runs:
                return
            run = runs[node_id]
            if isinstance(run, dict):
                run["ended"] = now()
            else:
                runs.pop(node_id, None)   # garbage: nothing to keep
            self._reconcile(records)
            self.commit(records)

    def others(self, container: str, node_id: str,
               since: float | None = None) -> bool:
        """Did any run other than `node_id` overlap `node_id`'s life in
        `container`? With `since` (when this run began, adversary finding 1)
        an entry that already ended counts for as long as it was alive then;
        without it, only entries live now do. Fails closed: with no usable
        record — a malformed entry, an unreadable container block, or the
        degraded in-memory mode — the answer is yes, and LN-C5's attribution,
        the one thing this feeds, stays `kill_uncertain`."""
        with self.locked() as records:
            if self.memory is not None:
                return True
            self._reconcile(records)
            runs = records.get(container)
            if not isinstance(runs, dict):
                return True          # no usable block for this container
            for other, run in runs.items():
                if other == node_id:
                    continue
                if not isinstance(run, dict):
                    return True      # unknown record: cannot prove it dead
                if run.get("ended") is None:
                    return True      # live now: overlapped by definition
                if since is None:
                    continue
                ended = run.get("ended")
                if not isinstance(ended, (int, float)) or isinstance(ended, bool):
                    return True
                if ended >= since:
                    return True      # it was still alive after we began
            return False
