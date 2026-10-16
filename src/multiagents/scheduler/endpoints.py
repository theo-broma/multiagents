"""Private listeners whose authority is the attempt's recorded capability."""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import threading
from pathlib import Path

from ..paths import ProjectPaths
from .model import Refused
from .store import private_directory


ENDPOINT_ID = re.compile(r"(?:att_)?[0-9a-f]{32}")


def handle(root, attempt_id):
    if not isinstance(attempt_id, str) or not ENDPOINT_ID.fullmatch(attempt_id):
        raise ValueError("an exact attempt id is required")
    directory = ProjectPaths(root).scheduler / "endpoints" / attempt_id
    if directory.is_symlink():
        raise ValueError("endpoint directories cannot be symlinks")
    private_directory(directory)
    return {"directory": str(directory), "attempt_id": attempt_id}


class Listeners:
    def __init__(self, service):
        self.service = service
        self.listeners = {}
        self.lock = threading.RLock()

    def authenticate(self, db, attempt_id):
        row = db.execute("SELECT c.subject,c.node_id,c.permissions FROM endpoint_authority e "
                         "JOIN capabilities c ON c.hash=e.capability "
                         "WHERE e.attempt_id=? AND e.revoked=0 AND c.revoked=0", (attempt_id,)).fetchone()
        if row is None:
            raise Refused("unauthenticated")
        return {"subject": row[0], "node_id": row[1], "permissions": json.loads(row[2]), "root": False}

    def record(self, db, attempt_id, run_id):
        row = db.execute("SELECT record FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        attempt = json.loads(row[0]) if row else {}
        if (attempt.get("run_id") != run_id or attempt.get("state") not in {"claimed", "launched"}
                or attempt.get("cancel_requested") or attempt.get("ended_at")):
            raise Refused("forbidden")
        existing = db.execute("SELECT capability,revoked FROM endpoint_authority WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            principal = self.authenticate(db, attempt_id)
            if principal["subject"] != run_id:
                raise Refused("forbidden")
            return
        cap = db.execute("SELECT hash FROM capabilities WHERE subject=? AND revoked=0", (run_id,)).fetchall()
        if len(cap) != 1:
            raise Refused("unauthenticated")
        db.execute("INSERT INTO endpoint_authority(attempt_id,capability) VALUES (?,?)", (attempt_id, cap[0][0]))

    def revoke(self, db, attempt_id):
        db.execute("UPDATE capabilities SET revoked=1 WHERE hash IN "
                   "(SELECT capability FROM endpoint_authority WHERE attempt_id=?)", (attempt_id,))
        db.execute("UPDATE endpoint_authority SET revoked=1 WHERE attempt_id=?", (attempt_id,))

    def start(self, attempt_id):
        from .rpc import Handler, Server
        with self.lock:
            if attempt_id in self.listeners:
                return
            with self.service.store.transaction(write=False) as db:
                self.authenticate(db, attempt_id)
            directory = Path(handle(self.service.paths.root, attempt_id)["directory"])
            path = directory / "rpc.sock"
            path.unlink(missing_ok=True)
            # Resolve the directory through a descriptor so sun_path never
            # contains the (possibly arbitrarily long) state-root prefix.
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                server = Server(f"/proc/self/fd/{directory_fd}/rpc.sock", Handler)
            finally:
                os.close(directory_fd)
            server.service = self.service
            server.endpoint_id = attempt_id
            server.connections = set()
            server.connections_lock = threading.RLock()
            path.chmod(0o600)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            self.listeners[attempt_id] = (server, thread, path)
            thread.start()

    def close(self, attempt_id):
        with self.lock:
            item = self.listeners.pop(attempt_id, None)
            if item is None:
                path = self.service.paths.scheduler / "endpoints" / attempt_id / "rpc.sock"
                path.unlink(missing_ok=True)
                return
            server, thread, path = item
            with server.connections_lock:
                for connection in list(server.connections):
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RDWR)
                        connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
            path.unlink(missing_ok=True)

    def recover(self):
        """Rebind only recorded principals whose invocation is confirmed live."""
        from ..executor.lifecycle import ResourceIdentity, Running, Unknown
        from ..executor.isolated import outcome
        from .orphans import backend, _run

        with self.service.store.transaction(write=False) as db:
            rows = db.execute("SELECT e.attempt_id,a.record FROM endpoint_authority e "
                              "LEFT JOIN attempts a ON a.id=e.attempt_id").fetchall()
        config = self.service.configuration()
        for attempt_id, raw in rows:
            attempt = json.loads(raw) if raw else {}
            evidence = attempt.get("launch_evidence", {}).get("executor", {})
            confirmed = False
            problem = None
            if (attempt.get("state") in {"claimed", "launched"} and not attempt.get("ended_at")
                    and not attempt.get("cancel_requested") and evidence.get("identity")):
                try:
                    target = backend(self.service.paths, config, evidence)
                    async def inspect():
                        return await outcome(target.inspect, ResourceIdentity(evidence["identity"]))
                    state = _run(inspect())
                    confirmed = isinstance(state, Running)
                    if isinstance(state, Unknown):
                        problem = state.reason
                except Exception as exc:
                    problem = str(exc)
            if confirmed:
                try:
                    self.start(attempt_id)
                    continue
                except Refused as exc:
                    problem = "authority cannot be confirmed: " + exc.result["error"]
                except Exception as exc:
                    problem = str(exc)
            # A stale socket inode must never imply authority after restart.
            self.close(attempt_id)
            if problem:
                from ..executor import get_executor
                runner = self.service.engine.runner
                executor = get_executor("isolated", paths=self.service.paths, providers=runner.providers)
                executor.restore(evidence)
                runner._hold_isolated_predecessor(attempt["run_id"],
                    f"endpoint {attempt_id}: {problem}", executor, adoption=True)

    def close_all(self):
        for attempt_id in list(self.listeners):
            self.close(attempt_id)
