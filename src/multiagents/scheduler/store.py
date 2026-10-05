"""Host-owned transactional plan, capabilities, replies and notifications."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from .. import gitops
from ..paths import ProjectPaths
from ..tree import now


def directory(project_root: Path) -> Path:
    return ProjectPaths(project_root).scheduler


def transport_directory(project_root: Path) -> Path:
    return ProjectPaths(project_root).scheduler_rpc


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, project_root: Path):
        self.paths = ProjectPaths(project_root)
        self.directory = directory(project_root)
        self.file = self.directory / "plan.sqlite3"

    def initialize(self) -> None:
        private_directory(self.directory)
        fd = os.open(self.file, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.file.chmod(0o600)
        with self.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS capabilities
                    (hash TEXT PRIMARY KEY, subject TEXT NOT NULL, node_id TEXT NOT NULL,
                     permissions TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS requests
                    (subject TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                     reply TEXT NOT NULL, PRIMARY KEY(subject, request_id));
                CREATE TABLE IF NOT EXISTS attempts
                    (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS templates
                    (name TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases
                    (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications
                    (seq INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('root_token', ?)",
                       (secrets.token_hex(32),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('plan_revision', '0')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('ack', '0')")

    @contextlib.contextmanager
    def transaction(self, *, write=True):
        # DELETE journals and FULL sync make the effect durable before reply;
        # BEGIN IMMEDIATE serialises host seams as well as RPC worker threads.
        db = sqlite3.connect(self.file if write else f"{self.file.as_uri()}?mode=ro",
                             uri=not write, timeout=30, isolation_level=None)
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def meta(db, key):
        return db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    @staticmethod
    def nodes(db) -> dict[str, dict]:
        # Creation order: save_node keeps a node's rowid across updates.
        return {id: json.loads(record) for id, record in db.execute("SELECT id, record FROM nodes ORDER BY rowid")}

    @staticmethod
    def save_node(db, node):
        db.execute("INSERT INTO nodes VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                   (node["id"], encode(node)))

    @staticmethod
    def transition(db, kind, node_id=None, detail=None):
        record = {"kind": kind, "node_id": node_id, "at": now(), "detail": detail or {}}
        seq = db.execute("INSERT INTO notifications(record) VALUES (?)", (encode(record),)).lastrowid
        record["seq"] = seq
        db.execute("UPDATE notifications SET record=? WHERE seq=?", (encode(record), seq))
        return record

    def pending(self) -> list[str]:
        if not self.file.is_file():
            return []
        db = sqlite3.connect(f"{self.file.as_uri()}?mode=ro", uri=True)
        try:
            return [id for id, record in db.execute("SELECT id, record FROM nodes")
                    if json.loads(record)["state"] not in {"done", "cancelled"}]
        finally:
            db.close()

    def mirror(self) -> None:
        """Append the host outbox without reading the container-writable log.

        Progress and the last destination identity live in the host database.
        A replaced/truncated destination gets the complete host history. A
        crash between append and checkpoint may duplicate a diagnostic event;
        the authoritative transitions and RPC effects remain exactly once.
        """
        try:
            with self.transaction(write=False) as db:
                pending = db.execute("""SELECT 1 FROM notifications WHERE seq >
                    coalesce((SELECT CAST(value AS INTEGER) FROM meta WHERE key='mirror_seq'), 0)
                    LIMIT 1""").fetchone()
                if pending is None:
                    return
            with self.transaction() as db:
                rows = dict(db.execute("SELECT key, value FROM meta WHERE key LIKE 'mirror_%'"))
                after = int(rows.get("mirror_seq", "0"))
                records = list(db.execute("SELECT seq, record FROM notifications WHERE seq>? ORDER BY seq", (after,)))
                if not records:
                    return
                self.paths.data.mkdir(parents=True, exist_ok=True)
                fd = gitops._open_file_beneath(
                    self.paths.data, (), self.paths.events_file.name,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT, replace=True)
                with os.fdopen(fd, "ab") as events:
                    stat = os.fstat(events.fileno())
                    identity = encode([stat.st_dev, stat.st_ino])
                    if (identity != rows.get("mirror_file")
                            or stat.st_size < int(rows.get("mirror_size", "0"))):
                        records = list(db.execute("SELECT seq, record FROM notifications ORDER BY seq"))
                    # A hostile writer can leave a partial line. Separate our
                    # append without interpreting any bytes supplied by it.
                    for seq, raw in records:
                        record = json.loads(raw)
                        record.update(kind="node." + record["kind"], t=record["at"],
                                      agent=record["node_id"] or "root", scheduler_seq=seq,
                                      scheduler_project=self.paths.slug)
                        # Each line is one O_APPEND write. Advisory locks on
                        # this container-writable file cannot suppress events.
                        line = ("\n" + encode(record) + "\n").encode()
                        if os.write(events.fileno(), line) != len(line):
                            raise OSError("short scheduler event append")
                    os.fsync(events.fileno())
                    self.set_meta(db, "mirror_seq", records[-1][0])
                    self.set_meta(db, "mirror_file", identity)
                    self.set_meta(db, "mirror_size", os.fstat(events.fileno()).st_size)
        except Exception:
            # The public log is diagnostic. Its failure must never change a
            # committed reply; the next append or daemon retry repairs it.
            logging.getLogger(__name__).exception("scheduler event mirror append failed; will retry")


def root_capability(project_root: Path) -> str:
    store = Store(project_root)
    if not store.file.is_file():
        raise FileNotFoundError(store.file)
    try:
        with store.transaction(write=False) as db:
            row = db.execute("SELECT value FROM meta WHERE key='root_token'").fetchone()
            if row is None:
                raise FileNotFoundError("scheduler capability is not ready")
            return row[0]
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError("scheduler store is not ready") from exc


def issue_run_capability(project_root: Path, run_id: str, node_id: str,
                         permissions: set[str]) -> str:
    if not isinstance(run_id, str) or not run_id or run_id == "root":
        raise ValueError("run_id is required")
    if not isinstance(permissions, set) or not permissions <= {"read", "delegate", "verdict"}:
        raise ValueError("unknown capability permission")
    token = secrets.token_hex(32)
    store = Store(project_root)
    with store.transaction() as db:
        if not isinstance(node_id, str) or db.execute(
                "SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() is None:
            raise ValueError("unknown node_id")
        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run_id,))
        db.execute("INSERT INTO capabilities(hash, subject, node_id, permissions) VALUES (?, ?, ?, ?)",
                   (token_hash(token), run_id, node_id, encode(sorted(permissions | {"read"}))))
    return token


def revoke_run_capability(project_root: Path, run_id: str) -> None:
    store = Store(project_root)
    if not store.file.is_file():
        return
    with store.transaction() as db:
        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run_id,))
