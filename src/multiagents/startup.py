"""Host-owned provider startup health and exclusive half-open probes."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid

from . import procs
from .authority import HostAuthority
from .paths import ProjectPaths


class StartupUnavailable(RuntimeError):
    def __init__(self, provider: str, retry_after: float | None = None):
        self.reason = "startup_down"
        self.retry_after = retry_after
        super().__init__(f"Provider {provider!r} is startup_down")


class StartupHealth:
    def __init__(self, paths: ProjectPaths):
        self.directory = HostAuthority.directory_for(paths)
        self.file = self.directory / "startup.json"
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            with self._lock():
                if not self.file.exists():
                    self._write({})
        except OSError:
            # Admission will fail closed, including for a new Runner which
            # cannot know the last state of this provider.
            pass

    @contextlib.contextmanager
    def _lock(self):
        with (self.directory / "startup.lock").open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self) -> dict:
        records = json.loads(self.file.read_text())
        if not isinstance(records, dict):
            raise ValueError("invalid startup health state")
        for record in records.values():
            if (not isinstance(record, dict)
                    or not isinstance(record.get("generation"), str)
                    or not isinstance(record.get("runs"), dict)
                    or not isinstance(record.get("count"), int)
                    or not isinstance(record.get("down"), bool)
                    or not isinstance(record.get("until"), (int, float))):
                raise ValueError("invalid startup health record")
        return records

    def _write(self, records: dict) -> None:
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=".startup-")
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

    @staticmethod
    def _new() -> dict:
        return {"generation": uuid.uuid4().hex, "count": 0,
                "down": False, "until": 0, "probe": None, "runs": {}}

    @staticmethod
    def _reconcile(record: dict) -> None:
        for run_id, run in list(record["runs"].items()):
            # The host still has to drain output and classify an exited run.
            # Conversely, a detached provider can outlive that host.
            if (not procs.alive(run["pid"], run["pid_start"])
                    and not procs.alive(run.get("owner_pid"),
                                        run.get("owner_start", ""))):
                del record["runs"][run_id]
                if record["probe"] == run["token"]:
                    record["probe"] = None

    @staticmethod
    def _blocked(record: dict) -> dict | None:
        if record["down"]:
            remaining = max(0.0, record["until"] - time.time())
            if record["probe"] or remaining:
                result = {"reason": "startup_down"}
                if remaining:
                    result["retry_after"] = record["until"]
                return result
        return None

    def availability(self, provider: str) -> dict | None:
        try:
            with self._lock():
                records = self._read()
                if provider not in records:
                    return None
                record = records[provider]
                self._reconcile(record)
                self._write(records)
                return self._blocked(record)
        except (OSError, ValueError, KeyError, TypeError):
            return {"reason": "startup_down"}

    def claim(self, provider: str, run_id: str) -> str:
        try:
            with self._lock():
                records = self._read()
                record = records.setdefault(provider, self._new())
                self._reconcile(record)
                blocked = self._blocked(record)
                if blocked:
                    raise StartupUnavailable(provider, blocked.get("retry_after"))
                token = uuid.uuid4().hex
                owner_pid = os.getpid()
                owner_start = procs.start_time(owner_pid)
                record["runs"][run_id] = {
                    "token": token, "generation": record["generation"],
                    "pid": owner_pid, "pid_start": owner_start,
                    "owner_pid": owner_pid, "owner_start": owner_start,
                }
                if record["down"]:
                    record["probe"] = token
                self._write(records)
                return token
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StartupUnavailable(provider) from exc

    @staticmethod
    def _current(record: dict, run_id: str, token: str) -> bool:
        run = record["runs"].get(run_id)
        return bool(run and run["token"] == token
                    and run["generation"] == record["generation"])

    def bind(self, provider: str, run_id: str, token: str,
             pid: int, pid_start: str = "") -> bool:
        """Record the live run's identity alongside its supervising host."""
        try:
            with self._lock():
                records = self._read()
                record = records.get(provider)
                if not record or not self._current(record, run_id, token):
                    return False
                record["runs"][run_id].update(
                    pid=pid, pid_start=pid_start or procs.start_time(pid))
                self._write(records)
                return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StartupUnavailable(provider) from exc

    def holds(self, provider: str, run_id: str, token: str) -> bool | None:
        """Does `token` still hold its claim? None when that cannot be read.

        A release (`finish`) reports nothing when its write fails; this is
        how a caller that must not move on before the claim is really gone
        confirms it (RM-R1c, review ag-598c45)."""
        try:
            with self._lock():
                record = self._read().get(provider)
                return bool(record) and self._current(record, run_id, token)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def token_for(self, provider: str, run_id: str, strict: bool = False) -> str:
        """Recover host authority for an adopted run, never from tree.json.
        "" when the run holds no claim. A storage failure is "" too —
        unless `strict` (RM-R1e), when it raises: a read that failed is
        unknown, never "no claim"."""
        try:
            with self._lock():
                records = self._read()
                record = records.get(provider)
                if not record:
                    return ""
                run = record["runs"].get(run_id)
                if not run or not self._current(record, run_id, run["token"]):
                    return ""
                owner_pid = os.getpid()
                run.update(owner_pid=owner_pid,
                           owner_start=procs.start_time(owner_pid))
                self._write(records)
                return run["token"]
        except (OSError, ValueError, KeyError, TypeError):
            if strict:
                raise
            return ""

    def progress(self, provider: str, run_id: str, token: str) -> bool:
        try:
            with self._lock():
                records = self._read()
                record = records.get(provider)
                if not record or not self._current(record, run_id, token):
                    return False
                recovered = record["down"] and record["probe"] == token
                if record["down"] and not recovered:
                    return False
                record["count"] = 0
                if recovered:
                    record.update(down=False, until=0, probe=None,
                                  generation=uuid.uuid4().hex, runs={})
                self._write(records)
                return recovered
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def finish(self, provider: str, run_id: str, token: str, failed: bool,
               error: str, threshold: int, cooldown: float,
               resolved: bool = False) -> dict | None:
        try:
            with self._lock():
                records = self._read()
                record = records.get(provider)
                # A pre-mark run, a replaced probe or a previous launch of
                # this run id has no authority over the current generation.
                if not record or not self._current(record, run_id, token):
                    return None
                del record["runs"][run_id]
                probe = record["probe"] == token
                event = None
                if failed:
                    record["count"] += 1
                    if record["count"] >= max(1, threshold):
                        record.update(down=True, until=time.time() + max(0, cooldown),
                                      probe=None, generation=uuid.uuid4().hex, runs={})
                        event = {"provider": provider, "count": record["count"],
                                 "error": next(iter(error.splitlines()), "")}
                if probe:
                    record["probe"] = None
                    if resolved and not failed and record["down"]:
                        # RC-R3 (bug-1213a0): a refusal is the provider
                        # answering — the binary started, authenticated and
                        # got a verdict. It resolves the outage it probed,
                        # progress or no progress: what the prompt contained
                        # says nothing about startup health.
                        record.update(down=False, until=0, count=0,
                                      generation=uuid.uuid4().hex, runs={})
                    else:
                        record["until"] = time.time() + max(0, cooldown)
                self._write(records)
                return event
        except (OSError, ValueError, KeyError, TypeError):
            return None
