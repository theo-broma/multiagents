"""Host-owned transactional plan, capabilities, replies and notifications."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from reprlib import recursive_repr
from collections.abc import Mapping, MutableMapping

from .. import gitops
from ..paths import ProjectPaths
from ..tree import now
from .snapshots import SnapshotMap


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


class TransactionSnapshot:
    """Transaction-local snapshots; never publish uncommitted records."""

    def __init__(self, store, writable):
        self.store, self.writable, self.snapshots = store, writable, {}


_transactions = {}


class RecordView(MutableMapping):
    """Copy records on access, with cached selections for scheduler scans."""

    def __init__(self, rows, indexes):
        self.rows, self.indexes = rows, indexes
        self.local = {}
        self.deleted = set()

    def __getitem__(self, id):
        if id in self.deleted:
            raise KeyError(id)
        if id not in self.local:
            self.local[id] = copy.deepcopy(self.rows[id][1])
        return self.local[id]

    def __setitem__(self, id, value):
        self.deleted.discard(id)
        self.local[id] = value

    def __delitem__(self, id):
        if id not in self:
            raise KeyError(id)
        self.local.pop(id, None)
        self.deleted.add(id)

    def __iter__(self):
        yield from (id for id in sorted(self.rows, key=lambda id: self.rows[id][0])
                    if id not in self.deleted)
        yield from (id for id in self.local if id not in self.rows and id not in self.deleted)

    def __len__(self):
        return (len(self.rows) - sum(id in self.rows for id in self.deleted)
                + sum(id not in self.rows for id in self.local))

    def __contains__(self, id):
        return id not in self.deleted and (id in self.rows or id in self.local)

    @property
    def added_ids(self):
        return [id for id in self.local if id not in self.rows and id not in self.deleted]

    def _peek(self, id, missing):
        if id in self.deleted:
            return missing
        if id in self.local:
            return self.local[id]
        return self.rows[id][1] if id in self.rows else missing

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        if len(self) != len(other):
            return False
        missing = object()
        if not isinstance(other, RecordView):
            return all(self._peek(id, missing) == value for id, value in other.items())
        changed = self.local.keys() | other.local.keys() | self.deleted | other.deleted
        if any(self._peek(id, missing) != other._peek(id, missing) for id in changed):
            return False
        return self.rows.equivalent(other.rows, equal=lambda left, right: left[1] == right[1], ignore=changed)

    @recursive_repr("{...}")
    def __repr__(self):
        return "{" + ", ".join(f"{id!r}: {self._peek(id, None)!r}" for id in self) + "}"

    def __deepcopy__(self, memo):
        # Cached rows are immutable to callers: __getitem__ copies a row
        # before exposing it. A speculative edit needs its own local records,
        # not a copy of every untouched record in the shared snapshot.
        view = RecordView(self.rows, self.indexes)
        memo[id(self)] = view
        view.local = copy.deepcopy(self.local, memo)
        view.deleted = self.deleted.copy()
        return view

    def selected(self, *keys):
        ids = set().union(*(self.indexes.get(key, ()) for key in keys))
        ids.update(self.local)
        for id in sorted(ids, key=lambda id: self.rows.get(id, (float("inf"),))[0]):
            if id not in self.deleted:
                value = self.local.get(id, self.rows[id][1] if id in self.rows else {})
                if any(key == value.get("state") or
                       (key.startswith("field:") and value.get(key[6:])) for key in keys):
                    yield self[id]

    def matching(self, field, expected):
        members = self.indexes["by:" + field].get(expected, ())
        ids = set(members)
        ids.update(self.local)
        for id in sorted(ids, key=lambda id: self.rows.get(id, (float("inf"),))[0]):
            if id not in self.deleted:
                value = self.local.get(id, self.rows[id][1] if id in self.rows else {})
                if value.get(field) == expected:
                    yield self[id]

    def state_counts(self, states):
        counts = {state: len(self.indexes.get(state, ())) for state in states}
        for id in self.local.keys() | self.deleted:
            if id in self.rows and (state := self.rows[id][1].get("state")) in counts:
                counts[state] -= 1
            if id not in self.deleted and (state := self.local[id].get("state")) in counts:
                counts[state] += 1
        return counts

    @property
    def run_ids(self):
        return self.indexes["run_ids"].keys()


def select_records(records, *states):
    if isinstance(records, RecordView):
        return records.selected(*states)
    return (value for value in records.values() if value.get("state") in states)


def matching_records(records, field, expected):
    if isinstance(records, RecordView):
        return records.matching(field, expected)
    return (value for value in records.values() if value.get(field) == expected)


def record_timezones(value):
    names, pending = set(), [value]
    while pending:
        part = pending.pop()
        if isinstance(part, dict):
            if isinstance(part.get("timezone"), str):
                names.add(part["timezone"])
            pending.extend(part.values())
        elif isinstance(part, (list, tuple)):
            pending.extend(part)
    return names


def update_indexes(indexes, id, value, *, remove=False):
    """Replace only index entries belonging to a changed record."""
    def member(key, item):
        nonlocal indexes
        members = indexes.get(key, SnapshotMap())
        members = members.discard(item) if remove else members.set(item, None)
        indexes = indexes.set(key, members)

    def counted(key, item):
        nonlocal indexes
        members = indexes[key]
        count = members.get(item, 0) + (-1 if remove else 1)
        members = members.set(item, count) if count else members.discard(item)
        indexes = indexes.set(key, members)

    if value.get("state") is not None:
        member(value["state"], id)
    for field in ("completion_pending", "git_operation", "window_stop"):
        if value.get(field):
            member("field:" + field, id)
    if value.get("run_id"):
        counted("run_ids", value["run_id"])
    for field in ("run_id", "node_id", "alias_id"):
        if value.get(field):
            by_field = indexes["by:" + field]
            members = by_field.get(value[field], SnapshotMap())
            members = members.discard(id) if remove else members.set(id, None)
            by_field = by_field.set(value[field], members) if members else by_field.discard(value[field])
            indexes = indexes.set("by:" + field, by_field)
    if value.get("runs") and (run_id := value["runs"][-1].get("run_id")):
        last_runs = indexes["last_runs"]
        members = last_runs.get(run_id, SnapshotMap())
        members = members.discard(id) if remove else members.set(id, None)
        last_runs = last_runs.set(run_id, members) if members else last_runs.discard(run_id)
        indexes = indexes.set("last_runs", last_runs)
    for name in record_timezones(value):
        counted("timezones", name)
    if value.get("node_id") and "seq" in value:
        nid, seq = value["node_id"], value["seq"]
        notifications = indexes["notifications"]
        entries = notifications.get(nid, SnapshotMap())
        entries = entries.discard(seq) if remove else entries.set(seq, value.get("at", 0))
        notifications = notifications.set(nid, entries) if entries else notifications.discard(nid)
        latest = indexes["latest"]
        latest = latest.set(nid, entries.last()) if entries else latest.discard(nid)
        indexes = indexes.set("notifications", notifications).set("latest", latest)
    return indexes


def record_indexes(rows):
    indexes = SnapshotMap()
    for key in ("latest", "notifications", "last_runs", "run_ids", "timezones",
                "by:run_id", "by:node_id", "by:alias_id"):
        indexes = indexes.set(key, SnapshotMap())
    for id, (_, value) in rows.items():
        indexes = update_indexes(indexes, id, value)
    return indexes.set("ids", rows.keys())


def records(db, table, *, lazy=False):
    """Read a mutable snapshot, decoding only records changed since last read.

    SQLite triggers maintain the cursor even for writes from another process
    or direct SQL. The cursor and rows are read in the same transaction.
    """
    key = "seq" if table == "notifications" else "id"
    transaction = _transactions.get(db)
    if transaction is None:
        return {id: json.loads(raw) for id, raw in db.execute(
            f"SELECT {key}, record FROM {table} ORDER BY rowid")}
    store = transaction.store
    cursor = int(Store.meta(db, "records_revision"))
    with store.snapshot_lock:
        previous = transaction.snapshots.get(table, store.snapshots.get(table))
    # Snapshot maps are immutable, so refresh our own version without holding
    # the cache lock. SQLite may wait for a writer that also needs this lock.
    if previous is None or previous[0] > cursor:
        rows = SnapshotMap()
        for rowid, id, raw in db.execute(
                f"SELECT rowid, {key}, record FROM {table} ORDER BY rowid"):
            rows = rows.set(id, (rowid, json.loads(raw)))
        indexes = record_indexes(rows)
    else:
        rows, indexes = previous[1:]
        if previous[0] != cursor:
            changed = db.execute(
                "SELECT id FROM record_changes WHERE table_name=? AND seq>? AND seq<=?",
                (table, previous[0], cursor))
            for (id,) in changed:
                if table == "notifications":
                    id = int(id)
                if id in rows:
                    indexes = update_indexes(indexes, id, rows[id][1], remove=True)
                row = db.execute(f"SELECT rowid, record FROM {table} WHERE {key}=?", (id,)).fetchone()
                if row is None:
                    rows = rows.discard(id)
                else:
                    value = json.loads(row[1])
                    rows = rows.set(id, (row[0], value))
                    indexes = update_indexes(indexes, id, value)
            indexes = indexes.set("ids", rows.keys())
    snapshot = (cursor, rows, indexes)
    transaction.snapshots[table] = snapshot
    if not transaction.writable:
        with store.snapshot_lock:
            stored = store.snapshots.get(table)
            if stored is None or stored[0] <= cursor:
                store.snapshots[table] = snapshot
    # Callers freely mutate records before saving. Those edits must not leak
    # to another transaction, including one that rolls back.
    view = RecordView(rows, indexes)
    return view if lazy else dict(view)


class Store:
    def __init__(self, project_root: Path):
        self.paths = ProjectPaths(project_root)
        self.directory = directory(project_root)
        self.file = self.directory / "plan.sqlite3"
        self.snapshots = {}
        self.snapshot_lock = threading.RLock()

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
                CREATE TABLE IF NOT EXISTS endpoint_authority
                    (attempt_id TEXT PRIMARY KEY, capability TEXT NOT NULL,
                     revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS requests
                    (subject TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                     reply TEXT NOT NULL, PRIMARY KEY(subject, request_id));
                CREATE TABLE IF NOT EXISTS consults
                    (run_id TEXT PRIMARY KEY, caller TEXT NOT NULL, agent TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts
                    (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_active ON attempts
                    (json_extract(record, '$.state'), json_extract(record, '$.run_id'));
                CREATE INDEX IF NOT EXISTS attempts_executor_kind ON attempts
                    (json_extract(record, '$.launch_evidence.executor.kind'));
                CREATE INDEX IF NOT EXISTS nodes_state ON nodes
                    (json_extract(record, '$.state'));
                CREATE TABLE IF NOT EXISTS record_changes
                    (table_name TEXT NOT NULL, id TEXT NOT NULL, seq INTEGER NOT NULL,
                     PRIMARY KEY(table_name, id));
                CREATE INDEX IF NOT EXISTS record_changes_seq ON record_changes(table_name, seq);
                CREATE TABLE IF NOT EXISTS templates
                    (name TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases
                    (id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications
                    (seq INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL);
                -- NT-R3: every hold writes a `held` transition whose seq is that
                -- hold's mark, so the mark of a node is looked up by node id and
                -- kind rather than by walking the log.
                CREATE INDEX IF NOT EXISTS notifications_by_node ON notifications
                    (json_extract(record, '$.node_id'), json_extract(record, '$.kind'), seq);
                CREATE TABLE IF NOT EXISTS notify_outbox
                    (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                     ref TEXT NOT NULL, data TEXT NOT NULL, enqueued_at REAL NOT NULL);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('root_token', ?)",
                       (secrets.token_hex(32),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('plan_revision', '0')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('ack', '0')")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('records_revision', '0')")
            for table in ("nodes", "attempts", "notifications"):
                key = "seq" if table == "notifications" else "id"
                for action, source in (("INSERT", "new"), ("UPDATE", "new"), ("DELETE", "old")):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_changed_{action.lower()}
                        AFTER {action} ON {table} BEGIN
                        UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='records_revision';
                        INSERT INTO record_changes VALUES ('{table}', {source}.{key},
                            (SELECT value FROM meta WHERE key='records_revision'))
                        ON CONFLICT(table_name, id) DO UPDATE SET seq=excluded.seq;
                        END""")

    @contextlib.contextmanager
    def transaction(self, *, write=True):
        # DELETE journals and FULL sync make the effect durable before reply;
        # BEGIN IMMEDIATE serialises host seams as well as RPC worker threads.
        # Never nest one transaction inside another: an inner read blocks
        # behind a commit-waiting writer while holding the outer read, which
        # is a 30 s deadlock by construction. Pass the open connection down.
        db = sqlite3.connect(self.file if write else f"{self.file.as_uri()}?mode=ro",
                             uri=not write, timeout=30, isolation_level=None)
        transaction = TransactionSnapshot(self, write)
        _transactions[db] = transaction
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
            if write:
                with self.snapshot_lock:
                    for table, snapshot in transaction.snapshots.items():
                        previous = self.snapshots.get(table)
                        if previous is None or previous[0] <= snapshot[0]:
                            self.snapshots[table] = snapshot
        except BaseException:
            db.rollback()
            raise
        finally:
            _transactions.pop(db, None)
            db.close()

    @staticmethod
    def meta(db, key):
        return db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    @staticmethod
    def set_meta(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    @staticmethod
    def nodes(db) -> MutableMapping[str, dict]:
        # Creation order: save_node keeps a node's rowid across updates.
        return records(db, "nodes", lazy=True)

    @staticmethod
    def node_records(db):
        return records(db, "nodes", lazy=True)

    @staticmethod
    def some_nodes(db, ids) -> dict[str, dict]:
        """The named nodes, and nothing else: `ids` is the caller's own short
        list, so this is one index lookup per id and never a walk of the plan."""
        out: dict[str, dict] = {}
        wanted = [id for id in dict.fromkeys(ids) if isinstance(id, str)]
        batch_size = min(400, db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
        for start in range(0, len(wanted), batch_size):
            chunk = wanted[start:start + batch_size]
            for id, record in db.execute(
                    "SELECT id, record FROM nodes WHERE id IN (%s)" % ",".join("?" * len(chunk)),
                    chunk):
                out[id] = json.loads(record)
        return out

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
        """Publish attention and append the host outbox without reading the log.

        Progress and the last destination identity live in the host database.
        A replaced/truncated destination gets the complete host history. A
        crash between append and checkpoint may duplicate a diagnostic event;
        the authoritative transitions and RPC effects remain exactly once.
        Attention failures propagate; a retry publishes the current plan.
        """
        # Serialize mirrors, not plan mutations. The lock is host-only: a
        # container must not be able to suppress publication with a flock.
        # Take each snapshot after locking so a delayed older writer cannot
        # overwrite a newer mirror. No SQLite transaction spans file I/O.
        with (self.directory / "mirror.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._mirror_attention()
            self._mirror_events()

    def _mirror_attention(self) -> None:
        from . import attention
        with self.transaction(write=False) as db:
            content = (encode({"holds": attention.current(self, db)}) + "\n").encode()
        self.paths.data.mkdir(parents=True, exist_ok=True)
        if gitops._read_beneath(self.paths.data, (), "attention.json", 16 * 1024 * 1024) != content:
            # Unlike the diagnostic log, attention is a required signal.
            # Report failures to the caller; the daemon retries current state.
            gitops._write_beneath(self.paths.data, (), "attention.json", content, create=True)

    def _mirror_events(self) -> None:
        try:
            with self.transaction(write=False) as db:
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
                    with self.transaction(write=False) as db:
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
                size = os.fstat(events.fileno()).st_size
            with self.transaction() as db:
                self.set_meta(db, "mirror_seq", records[-1][0])
                self.set_meta(db, "mirror_file", identity)
                self.set_meta(db, "mirror_size", size)
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
