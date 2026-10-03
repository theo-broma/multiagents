"""The limits each run was launched under — host-owned, keyed by run id.

`tree.json` is container-writable, and the node records in it carry the
limits of the run that filled them. Reading provenance back from there
(adversary findings 3 and 8 on D1) lets a running agent forge the file and
line a later trip names, or the wall-clock timeout its own next launch runs
under. So the launch-time limits — `{name: {value, source, source_detail}}`
— are recorded here, in the protected host-state directory H7's startup
health already uses, at every launch, and read back by adoption, by every
relaunch that must keep a `call`-sourced timeout (LM-R1b), and by any trip
that fires without a Run of its own.

When the record for a run is missing — deleted, or written by a server from
before this record existed — callers resolve the limits from the current
config instead, and the provenance they report is that resolution's, honestly
pointing at the config as it is now rather than at a launch nobody recorded.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import hashlib
import tempfile
from typing import Iterator
from pathlib import Path

from .authority import HostAuthority
from .paths import ProjectPaths


class LaunchLimits:
    def __init__(self, paths: ProjectPaths):
        self.directory = HostAuthority.directory_for(paths)
        self.file = self.directory / "launch-limits.json"
        self.lock_path = self.directory / "launch-limits.lock"
        self.memory: dict | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
        except OSError:
            self.memory = {}

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
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=".launch-")
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

    # Enough adopted-run lookups to survive a restart of a busy project, small
    # enough that the file stays trivial: the newest 256 launches. Records are
    # keyed by node id and overwritten by every relaunch of the same node, so
    # this bounds distinct nodes, not turns. A record evicted by volume leaves
    # its caller the honest current-config fallback, never `tree.json`.
    KEEP = 256

    def record(self, node_id: str, limits: dict, launched_at: float) -> None:
        """Note the limits of `node_id`'s latest launch. Every launch writes,
        so a relaunch re-resolves and replaces — the run in flight is always
        under the newest record. Pruning is by recency, not node lifecycle: a
        steer, a retry and an adoption all outlive one turn, and which nodes
        may still relaunch is exactly what a terminal status cannot say."""
        if not isinstance(limits, dict):
            return
        with self.locked() as records:
            records.pop(node_id, None)
            records[node_id] = {"limits": limits, "launched_at": launched_at}
            while len(records) > self.KEEP:
                records.pop(next(iter(records)))
            self.commit(records)

    def lookup(self, node_id: str) -> dict:
        """The limits of `node_id`'s latest launch, or `{}` when nothing
        host-owned vouches for one."""
        found = self.read().get(node_id)
        if not isinstance(found, dict):
            return {}
        limits = found.get("limits", found)  # records written before launch times
        return limits if isinstance(limits, dict) else {}

    def launch_time(self, node_id: str) -> float | None:
        """The conservative launch clock vouched for by host-owned records.

        The per-node route record retains this clock if the bounded limits
        ledger evicts the node. It never retains any limit values.
        """
        found = self.read().get(node_id)
        records = [found, self._spec_record(node_id)]
        clocks = []
        for record in records:
            value = record.get("launched_at") if isinstance(record, dict) else None
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value > 0):
                clocks.append(float(value))
        return min(clocks) if clocks else None

    def _spec_path(self, node_id: str) -> Path:
        return self.directory / ("spec-" + hashlib.sha256(node_id.encode()).hexdigest() + ".json")

    def record_spec(self, node_id: str, spec: dict, launched_at: float) -> None:
        """SR-R3/R4: retain route and clock, never another limit authority."""
        path = self._spec_path(node_id)
        spec = dict(spec)
        limits = {"timeout", "silence_timeout", "max_steps"}
        for key in limits:
            spec.pop(key, None)
        spec["set_fields"] = sorted(set(spec.get("set_fields") or ()) - limits)
        record = {"spec": spec, "launched_at": launched_at}
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".spec-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(record, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def _spec_record(self, node_id: str) -> dict:
        path = self._spec_path(node_id)
        try:
            found = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def spec(self, node_id: str) -> dict:
        return self._spec_record(node_id).get("spec", {})
