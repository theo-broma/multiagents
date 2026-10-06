"""The scheduler's ntfy sender (NT-R3..R6, the NT-R4 pause of NT-R8).

Part 1 (`notify.py`) covered the config, the one-shot `notify` tool and the
CLI. This module is what the host scheduler adds:

- **NT-R3.** Event detection: `question` (a parked NEED_DECISION question),
  `held` (a node becoming held, one notification per hold), `anomaly` (an AN
  `anomaly` transition) and `done` (a top-level node reaching a terminal
  state). Messages are built only from the templates below — ids, kinds and
  reason/outcome codes — never free-text detail, question text or transcript.
- **NT-R4.** A durable outbox (`notify_outbox`) with its own cursor
  (`notify_seq`), separate from the orchestrator's `ack_nodes` cursor.
  Delivery is at least once: a row leaves the outbox only after the server
  accepted the message, so a crash after accept resends instead of losing.
  Retriable failures back off exponentially (30 s doubling to a 10 min cap);
  400/401/403/404 pause sending until the config changes or `notify test`
  succeeds. Rows older than 24 h expire, counted into the next message.
- **NT-R5.** At most one message per `min_interval_seconds` (the scheduler's
  clock); faster events group into one message of up to 10 lines plus
  "+N more", at the highest priority of everything it carries.
- **NT-R6.** Sending happens on a sender thread, outside the scheduler tick
  and outside any store transaction: a slow, hanging or failing server never
  delays a tick and never changes a node, run or transition.
- **NT-R8.** A successful `notify test` clears the pause.

Timing (backoff, rate limit, expiry) runs on the scheduler's clock (the one
`--clock-file` drives), never on wall time. The backoff carries no jitter:
the contract allows at most ±10 %, and zero is within that bound — the exact
30 s / 10 min steps are what the suite pins, and any positive jitter would
push a capped retry past the step that must contain it.

Lock scope: scan and status do short SQLite reads/writes plus one locked
tree read, all outside any other transaction. The network, the token file
and the clock file are touched only outside store transactions. The token
appears in no row, no log line and no status output: rows hold ids, kinds,
codes and counts; reasons come from `notify.publish_detailed`, whose fixed
strings never carry secrets.

Nothing here grows with the length of the transition history (round 3):
`scan` and the sender read the hold marks of the nodes they are already
about, through the `notifications_by_node` index, and the sender applies the
pause, the backoff and the rate limit before it reads anything about a
message — a sender the store has stopped opens no transaction at all, and
notices a pause cleared elsewhere from a stat of the store file.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import notify as notify_mod

log = logging.getLogger(__name__)

# NT-R4: retry steps and bounds, on the scheduler's clock.
BACKOFF_FIRST_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 600.0
EXPIRY_SECONDS = 24 * 3600

# NT-R5: a grouped message lists the first lines in happened order.
GROUP_LINES = 10

# The scan and the sender run on different threads: a burst of events may
# still be arriving (scan enqueuing) when the clock first allows a send. A
# group goes out only once the outbox has sat unchanged for this long on the
# wall clock, so a burst created in one go leaves in one message. Rate,
# backoff, expiry and pause stay on the scheduler's clock; this only ever
# delays a send that is otherwise due, within the suites' arrival timeouts.
SEND_COHERENCE_SECONDS = 1.0

KINDS = ("question", "held", "anomaly", "done")


# --------------------------------------------------------------------------
# Config views
# --------------------------------------------------------------------------

def section_of(config) -> dict | None:
    """The normalised `notify:` section, or None when the feature is off."""
    try:
        project = config.project or {}
    except AttributeError:
        project = config.get("project", {}) if isinstance(config, dict) else {}
    section = project.get("notify")
    return section if isinstance(section, dict) else None


def dest_of(section: dict) -> tuple[str, str]:
    return (str(section.get("ntfy_url")), str(section.get("topic")))


def interval_of(section: dict) -> float:
    try:
        value = float(section.get("min_interval_seconds",
                                  notify_mod.DEFAULT_MIN_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        return float(notify_mod.DEFAULT_MIN_INTERVAL_SECONDS)
    return value if value > 0 else float(notify_mod.DEFAULT_MIN_INTERVAL_SECONDS)


def events_of(section: dict) -> set[str]:
    events = section.get("events")
    if not isinstance(events, list):
        return set(notify_mod.DEFAULT_EVENTS)
    return {e for e in events if isinstance(e, str)}


def _cfg_fingerprint(section: dict) -> str:
    return json.dumps({"events": sorted(events_of(section)),
                       "min_interval_seconds": interval_of(section),
                       "token_file": section.get("token_file"),
                       "ntfy_url": section.get("ntfy_url"),
                       "topic": section.get("topic")},
                      sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------
# Store access (short transactions only; no other I/O inside)
# --------------------------------------------------------------------------

def _meta_get(db, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(db, key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))


def _meta_float(db, key: str) -> float | None:
    raw = _meta_get(db, key)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _meta_int(db, key: str) -> int:
    raw = _meta_get(db, key)
    try:
        return int(float(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _held_seqs(db, node_ids) -> dict[str, int]:
    """The seq of each named node's latest `held` transition (NT-R3, round-2 fix 1,
    round-3 item 1).

    One notification goes out per hold, so the "already announced" mark is keyed
    on the hold itself — the seq of its `held` transition — and not on the node
    revision: editing a node that stays held bumps the revision but writes no new
    `held` transition, so it announces nothing new. A later hold of the same
    node writes a new transition with a new seq, so it is a new event.

    Every path that holds a node writes that transition, so the seq is always
    there; `_hold_mark`'s revision is the fallback for a hold that predates the
    change. The read is bounded twice over: only the ids it is given, and through
    the `notifications_by_node` index, so no id means no query and no call ever
    walks the history.
    """
    ids = [id for id in dict.fromkeys(node_ids) if isinstance(id, str)]
    if not ids:
        return {}
    try:
        pairs = db.execute(
            "SELECT json_extract(record, '$.node_id'), MAX(seq) FROM notifications"
            " WHERE json_extract(record, '$.kind') = 'held'"
            " AND json_extract(record, '$.node_id') IN (%s) GROUP BY 1"
            % ",".join("?" * len(ids)), ids).fetchall()
    except Exception:
        return {}
    seqs: dict[str, int] = {}
    for nid, seq in pairs:
        if not isinstance(nid, str):
            continue
        try:
            seqs[nid] = int(seq)
        except (TypeError, ValueError):
            continue
    return seqs


def _hold_mark(node: dict, seq: int | None) -> str:
    """The `notify_held:<node>` mark for this hold: its transition seq, or —
    for a hold that predates any `held` transition — the revision, so the hold
    is still announced exactly once."""
    if seq is not None:
        return str(seq)
    return "rev:" + str(node.get("revision"))


def _outbox_rows(db) -> list[dict]:
    try:
        rows = db.execute("SELECT seq, kind, ref, data, enqueued_at FROM notify_outbox"
                          " ORDER BY seq").fetchall()
    except Exception:
        return []
    out = []
    for seq, kind, ref, data, enqueued_at in rows:
        try:
            payload = json.loads(data)
        except ValueError:
            payload = {}
        out.append({"seq": seq, "kind": kind, "ref": ref, "data": payload,
                    "enqueued_at": float(enqueued_at)})
    return out


def _snapshot_read(db) -> dict:
    try:
        pending = db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    except Exception:
        pending = 0
    last = _meta_get(db, "notify_last_accepted")
    failure = _meta_get(db, "notify_failure")
    return {"pending": int(pending), "last_accepted_at": last, "failure": failure}


def snapshot(store, db=None) -> dict:
    """The NT-R6 status mapping: pending, last accepted, current failure.

    Reads the store only, so it never stalls behind a hanging send. The
    token appears in no field. `db` is the caller's open connection when
    there is one: opening a second connection inside an open read
    transaction deadlocks against a commit-waiting writer, so it is never
    done.
    """
    if db is not None:
        return _snapshot_read(db)
    try:
        with store.transaction(write=False) as conn:
            return _snapshot_read(conn)
    except Exception:
        return {"pending": 0, "last_accepted_at": None, "failure": None}


def clear_pause(store) -> None:
    """Clear an NT-R4 pause (NT-R8): a successful `notify test`, or a config
    change, resumes sending. The failure that caused it goes with it, so a
    status read after a successful test does not still report the old one
    (round 3, decision 3). Raises when the store cannot be updated, so
    that `notify test` can report the still-standing pause instead of
    claiming success (round-2 fix 6). There may be no store yet, which is a
    no-op rather than a failure."""
    if not store.file.is_file():
        return
    with store.transaction() as db:
        for key in ("notify_paused", "notify_next_retry", "notify_attempts", "notify_failure"):
            db.execute("DELETE FROM meta WHERE key=?", (key,))


def _clear_pause_in(db) -> None:
    for key in ("notify_paused", "notify_next_retry", "notify_attempts", "notify_failure"):
        db.execute("DELETE FROM meta WHERE key=?", (key,))


# --------------------------------------------------------------------------
# NT-R3: templates. Ids, kinds, reason/outcome codes only — never free text.
# --------------------------------------------------------------------------

def _line(row: dict) -> str:
    kind, ref, data = row["kind"], row["ref"], row["data"]
    if kind == "question":
        return f"question {ref}: need_decision"
    if kind == "held":
        return f"held {ref}: {data.get('reason') or 'held'}"
    if kind == "anomaly":
        return f"anomaly {ref}: {data.get('kind') or 'anomaly'}"
    if kind == "done":
        return f"done {ref}: {data.get('outcome') or 'done'}"
    if kind == "summary":
        nq = data.get("open_questions", 0)
        nh = data.get("held_nodes", 0)
        return (f"notification summary: {nq} open "
                f"{'question' if nq == 1 else 'questions'}, "
                f"{nh} held {'node' if nh == 1 else 'nodes'}")
    return f"{kind} {ref}"


def build_message(project_name: str, rows: list[dict], expired: int) -> tuple[str, str, str]:
    """(title, body, priority) for one grouped message (NT-R3, NT-R5).

    Events in happened order, up to GROUP_LINES lines plus "+N more"; the
    expiry count goes into the message as its own line; the priority is the
    highest of ALL carried events, including the hidden ones.
    """
    kinds = {row["kind"] for row in rows}
    head = kinds.pop() if len(kinds) == 1 else "notifications"
    title = f"{project_name} · {head}"
    lines = [_line(row) for row in rows[:GROUP_LINES]]
    if len(rows) > GROUP_LINES:
        lines.append(f"+{len(rows) - GROUP_LINES} more")
    if expired > 0:
        lines.append(f"{expired} notification{'s' if expired != 1 else ''} expired")
    # The highest priority of ALL carried events, including the ones hidden
    # behind "+N more".
    priority = "high" if any(row["kind"] in ("question", "held") for row in rows) \
        else "default"
    return title, "\n".join(lines), priority


# --------------------------------------------------------------------------
# NT-R3: scan. Called at the end of every scheduler tick; fast, no I/O but
# short store transactions and one locked tree read.
# --------------------------------------------------------------------------

def scan(service, store, tree, project_name: str, now: float) -> None:
    """Detect new scheduler events into the durable outbox (NT-R3).

    Never raises: a scan failure is logged and the next tick retries. Never
    writes a transition and never touches a node, run or cursor but its own.
    """
    try:
        config = service.configuration()
    except Exception:
        log.exception("ntfy scan: unreadable configuration")
        return
    section = section_of(config)
    try:
        with store.transaction(write=False) as db:
            top = db.execute("SELECT coalesce(max(seq), 0) FROM notifications").fetchone()[0]
            cursor = _meta_int(db, "notify_seq")
            stored_dest = _meta_get(db, "notify_dest")
            stored_cfg = _meta_get(db, "notify_cfg")
            nodes = store.nodes(db)
            fresh = [json.loads(raw) for (raw,) in
                     db.execute("SELECT record FROM notifications WHERE seq>? ORDER BY seq",
                                (cursor,))] if section is not None else []
            outbox_has = db.execute(
                "SELECT 1 FROM notify_outbox LIMIT 1").fetchone() is not None
            pause_keys = db.execute(
                "SELECT 1 FROM meta WHERE key IN ('notify_paused', 'notify_next_retry',"
                " 'notify_attempts', 'notify_failure', 'notify_expired')"
                " LIMIT 1").fetchone() is not None
    except Exception:
        log.exception("ntfy scan: store snapshot failed")
        return
    try:
        questions = [q for q in (tree.read().get("questions") or [])
                     if isinstance(q, dict) and q.get("status") == "open"]
    except Exception:
        log.exception("ntfy scan: tree read failed")
        return
    if section is None:
        # NT-R4: removing the section discards the pending outbox. The
        # cursor and the seen-marks stay, so re-adding it does not resend
        # the history — only a fresh activation summary goes out. The stored
        # destination goes with the section, so adding it back counts as an
        # activation even when the address is unchanged (round-2 fix 5).
        # A quiet tick writes nothing: the write transaction below runs only
        # when something actually changed (round-2 fix 7).
        if top <= cursor and not outbox_has and not pause_keys \
                and stored_dest is None and stored_cfg is None:
            return
        try:
            with store.transaction() as db:
                if top > _meta_int(db, "notify_seq"):
                    _meta_set(db, "notify_seq", top)
                db.execute("DELETE FROM notify_outbox")
                _clear_pause_in(db)
                db.execute("DELETE FROM meta WHERE key=?", ("notify_expired",))
                db.execute("DELETE FROM meta WHERE key=?", ("notify_dest",))
                db.execute("DELETE FROM meta WHERE key=?", ("notify_cfg",))
        except Exception:
            log.exception("ntfy scan: outbox discard failed")
        return

    dest = dest_of(section)
    dest_json = json.dumps(list(dest), separators=(",", ":"))
    cfg_json = _cfg_fingerprint(section)
    events = events_of(section)
    activated = stored_dest is None or stored_dest != dest_json
    held_ids = [nid for nid, node in nodes.items() if node.get("state") == "held"]
    try:
        with store.transaction(write=False) as db:
            seen_q = {key[len("notify_q:"):] for (key,) in
                      db.execute("SELECT key FROM meta WHERE key LIKE 'notify_q:%'")}
            seen_held = {key[len("notify_held:"):]: value for (key, value) in
                         db.execute("SELECT key, value FROM meta WHERE key LIKE 'notify_held:%'")}
            # The hold marks of the nodes held right now, and no other log read:
            # without the `held` event and without an activation nothing is held
            # to mark.
            held_seqs = (_held_seqs(db, held_ids)
                         if held_ids and (activated or stored_cfg is None
                                          or "held" in events) else {})
    except Exception:
        log.exception("ntfy scan: seen-marks read failed")
        return

    inserts: list[tuple] = []
    marks: list[tuple[str, str]] = []
    open_ids = [q["id"] for q in questions if isinstance(q.get("id"), str)]

    def enqueue(kind: str, ref: str, data: dict) -> None:
        inserts.append((kind, ref, json.dumps(data, sort_keys=True,
                                              separators=(",", ":")), now))

    if activated or stored_cfg is None:
        # On activation — the first start with `notify:`, or a destination
        # changed since the last start — send one summary of the counts, not
        # the history: the cursor jumps over everything so far, and every
        # open question and current hold is marked seen without being
        # announced.
        enqueue("summary", "", {"open_questions": len(open_ids),
                                "held_nodes": sum(1 for n in nodes.values()
                                                  if n.get("state") == "held")})
        for qid in open_ids:
            if qid not in seen_q:
                marks.append(("notify_q:" + qid, "1"))
        for nid, node in nodes.items():
            if node.get("state") == "held" \
                    and seen_held.get(nid) != _hold_mark(node, held_seqs.get(nid)):
                marks.append(("notify_held:" + nid,
                              _hold_mark(node, held_seqs.get(nid))))
        if top > cursor:
            cursor = top
    else:
        for record in fresh:
            kind = record.get("kind")
            nid = record.get("node_id")
            detail = record.get("detail") or {}
            if kind == "anomaly" and "anomaly" in events and isinstance(nid, str):
                code = detail.get("kind") if isinstance(detail, dict) else None
                if (code or "anomaly") == "held_idle":
                    # A held node going quiet is a reminder of the hold the
                    # `held` event already announced with its reason — not a
                    # new event. Announcing it as an anomaly would re-send
                    # every held node under `events: [anomaly]`.
                    continue
                enqueue("anomaly", nid, {"kind": code or "anomaly"})
            elif kind in ("done", "cancelled") and "done" in events and isinstance(nid, str):
                node = nodes.get(nid)
                if node is not None and node.get("parent") is None:
                    outcome = (detail.get("outcome") if isinstance(detail, dict)
                               and detail.get("outcome") else kind)
                    enqueue("done", nid, {"outcome": str(outcome)})
        for q in questions:
            qid = q.get("id")
            if (isinstance(qid, str) and qid not in seen_q and "question" in events):
                enqueue("question", qid, {})
                marks.append(("notify_q:" + qid, "1"))
                seen_q.add(qid)
        if "held" in events:
            for nid, node in nodes.items():
                if node.get("state") != "held":
                    continue
                mark = _hold_mark(node, held_seqs.get(nid))
                if seen_held.get(nid) != mark:
                    hold = node.get("hold") or {}
                    reason = hold.get("reason") if isinstance(hold, dict) else None
                    enqueue("held", nid, {"reason": reason or "held",
                                          "held_seq": held_seqs.get(nid),
                                          "rev": node.get("revision")})
                    marks.append(("notify_held:" + nid, mark))
                    seen_held[nid] = mark
        if fresh:
            cursor = max(cursor, max(int(r.get("seq", cursor)) for r in fresh))
    # NT-R4/R6, round-2 fix 8: the per-question and per-hold marks are
    # bounded — a closed question's mark and a terminal (or no longer held)
    # node's mark go away with the scan that observes it.
    drop_keys = ["notify_q:" + qid for qid in seen_q if qid not in set(open_ids)]
    for nid in seen_held:
        node = nodes.get(nid)
        # The hold is over — the node is terminal, missing, or simply no
        # longer held — so its mark goes away. A later hold writes a new
        # `held` transition with a new seq, which announces again.
        if node is None or node.get("state") != "held":
            drop_keys.append("notify_held:" + nid)
    # A quiet tick writes nothing: the write transaction below (with its
    # fsync) runs only when something actually changed.
    if not inserts and not marks and not drop_keys and stored_dest == dest_json \
            and stored_cfg == cfg_json and cursor >= top:
        return
    try:
        with store.transaction() as db:
            if top > _meta_int(db, "notify_seq"):
                _meta_set(db, "notify_seq", top)
            for kind, ref, data, at in inserts:
                db.execute("INSERT INTO notify_outbox(kind, ref, data, enqueued_at)"
                           " VALUES (?, ?, ?, ?)", (kind, ref, data, at))
            for key, value in marks:
                _meta_set(db, key, value)
            for key in drop_keys:
                db.execute("DELETE FROM meta WHERE key=?", (key,))
            if _meta_get(db, "notify_dest") != dest_json:
                _meta_set(db, "notify_dest", dest_json)
            if _meta_get(db, "notify_cfg") != cfg_json:
                # Any config change ends the pause; only a changed
                # destination re-announces (above). (NT-R4)
                _meta_set(db, "notify_cfg", cfg_json)
                _clear_pause_in(db)
    except Exception:
        log.exception("ntfy scan: outbox append failed")


# --------------------------------------------------------------------------
# Sender: one thread per scheduler process, outside every tick and every
# store transaction (NT-R6).
# --------------------------------------------------------------------------

def _retry_after_seconds(value, now: float) -> float | None:
    """A 429's Retry-After honoured inside [30 s, 10 min], clamped (NT-R4).

    A non-finite or unparsable value is treated as absent, so the normal
    backoff applies (round-2 fix 4): `float` accepts "nan", "inf" and
    "1e999" (which overflows to inf), and none of them is a usable delay.
    """
    if value is None:
        return None
    try:
        delay = float(str(value).strip())
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            return None
        stamp = moment.timestamp()
        try:
            delay = float(stamp - now)
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay):
        return None
    return min(max(delay, BACKOFF_FIRST_SECONDS), BACKOFF_CAP_SECONDS)


class Sender(threading.Thread):
    """The pump: gate, expire, drop the stale, send, back off."""

    def __init__(self, service, store, tree, project_name: str, clock):
        super().__init__(name="ntfy-sender", daemon=True)
        self.service = service
        self.store = store
        self.tree = tree
        self.project_name = project_name
        self.clock = clock
        self.stopped = threading.Event()
        # Round-2 fix 3: an outage runs from the first failed send to the
        # next accepted one, whatever mix of reasons occurs within it, and
        # writes one log line. The current reason still reaches the status
        # mapping on every failure; only the log line is once per outage.
        self._in_outage = False
        # The outbox shape the last pump saw, and wall-clock when it first
        # did: the NT-R5 coherence gate below.
        self._coherent_key: tuple | None = None
        self._coherent_since: float = 0.0
        # Round 3, decision 2: what the store last said about this sender's own
        # timing. A sender the store has paused or backed off opens no
        # transaction at all; it looks again when the store file itself changed
        # (a `notify test` clearing the pause from another process, or a config
        # change the scan recorded), which is a stat, not a query.
        self._paused: bool | None = None
        self._retry_at: float | None = None
        self._store_stamp: tuple | None = None

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.pump()
            except Exception:
                log.exception("ntfy sender: pump failed")
            self.stopped.wait(0.15)

    # -- the gate, remembered -------------------------------------------
    def _stamp(self) -> tuple | None:
        try:
            info = self.store.file.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _remember(self, meta, now: float) -> None:
        """Keep what the store said about the pause and the backoff, so the next
        poll can decide without opening a transaction."""
        self._paused = meta.get("notify_paused") == "1"
        self._retry_at = self._float(meta.get("notify_next_retry"))
        self._store_stamp = self._stamp()

    def _waiting(self, now: float) -> bool:
        """Whether the last thing the store said stops this sender from sending."""
        if self._paused is None:
            return False                     # nothing read yet: look
        return self._paused or (self._retry_at is not None and now < self._retry_at)

    # -- pump ----------------------------------------------------------
    def pump(self) -> None:
        try:
            section = section_of(self.service.configuration())
        except Exception:
            log.exception("ntfy sender: unreadable configuration")
            return
        if section is None:
            return
        now = self.clock()
        # Round 3, decision 2: a paused or backing-off sender reads nothing.
        # The store changed is the only thing that can lift the pause from
        # outside this process, and that is a stat of the store file.
        if self._waiting(now) and self._store_stamp == self._stamp():
            return
        try:
            with self.store.transaction(write=False) as db:
                rows = _outbox_rows(db)
                meta = {k: v for k, v in
                        db.execute("SELECT key, value FROM meta WHERE key LIKE 'notify_%'")}
        except Exception:
            log.exception("ntfy sender: store snapshot failed")
            return
        self._remember(meta, now)
        if not rows:
            return
        # NT-R4 and NT-R5 come before any staleness read (round 3, decision 2):
        # there is nothing to look up about a message that may not go out yet.
        if self._paused:
            return  # NT-R4: paused until the config changes or `notify test`.
        if self._retry_at is not None and now < self._retry_at:
            return
        last_sent = self._float(meta.get("notify_last_sent"))
        if last_sent is not None and now - last_sent < interval_of(section):
            return  # NT-R5: at most one message per interval.
        expired_total = int(float(meta.get("notify_expired", "0") or "0"))
        live = []
        drop: list[int] = []
        for row in rows:
            if now - row["enqueued_at"] > EXPIRY_SECONDS:
                # NT-R4: older than 24 h is dropped; the count of dropped
                # events goes into the next sent message.
                drop.append(row["seq"])
                expired_total += 1
        rows = [r for r in rows if r["seq"] not in drop]
        if rows:
            try:
                asked = [row["ref"] for row in rows
                         if row["kind"] in ("question", "held")]
                asked_held = [row["ref"] for row in rows if row["kind"] == "held"]
                with self.store.transaction(write=False) as db:
                    # Bounded by the outbox's own node ids, and by the index
                    # behind them (round 3, decision 2): the staleness read
                    # never walks the plan, and the hold marks never walk the
                    # history.
                    nodes = self.store.some_nodes(db, asked)
                    held_seqs = _held_seqs(db, asked_held)
                open_ids: set = set()
                if any(row["kind"] == "question" for row in rows):
                    open_ids = {q.get("id") for q in
                                (self.tree.read().get("questions") or [])
                                if isinstance(q, dict) and q.get("status") == "open"}
            except Exception:
                log.exception("ntfy sender: staleness read failed")
                return
            for row in rows:
                # NT-R3: an event no longer true when its turn comes is
                # dropped — the question was answered, or the node left held.
                # A hold is identified by its `held` transition's seq
                # (round-2 fix 1): an edit that keeps the node held changes
                # the revision but not the hold, so the row stays live.
                if row["kind"] == "question" and row["ref"] not in open_ids:
                    drop.append(row["seq"])
                elif row["kind"] == "held":
                    node = nodes.get(row["ref"])
                    want = row["data"].get("held_seq")
                    if node is None or node.get("state") != "held":
                        drop.append(row["seq"])
                    elif want is not None:
                        if held_seqs.get(row["ref"]) != want:
                            drop.append(row["seq"])
                    elif node.get("revision") != row["data"].get("rev"):
                        drop.append(row["seq"])
            live = [r for r in rows if r["seq"] not in drop]
        if drop or expired_total != int(float(meta.get("notify_expired", "0") or "0")):
            try:
                with self.store.transaction() as db:
                    for seq in drop:
                        db.execute("DELETE FROM notify_outbox WHERE seq=?", (seq,))
                    _meta_set(db, "notify_expired", expired_total)
            except Exception:
                log.exception("ntfy sender: drop failed")
                return
        if not live:
            return
        key = (live[-1]["seq"], len(live))
        if key != self._coherent_key:
            self._coherent_key = key
            self._coherent_since = time.monotonic()
        if time.monotonic() - self._coherent_since < SEND_COHERENCE_SECONDS:
            return  # NT-R5: let a burst finish arriving before grouping it.
        title, body, priority = build_message(self.project_name, live, expired_total)
        url, topic = dest_of(section)
        token, token_error = notify_mod.read_token(section.get("token_file"))
        if token_error is not None:
            self._failed(token_error, pause=False)
            return
        result = notify_mod.publish_detailed(url, topic, title, body, priority,
                                             None, token)
        self._finish(section, result, [r["seq"] for r in live])

    @staticmethod
    def _float(raw) -> float | None:
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backoff(attempts: int) -> float:
        # 30 s doubling to a 10 min cap, no jitter (see the module note).
        return min(BACKOFF_FIRST_SECONDS * (2 ** max(0, attempts)), BACKOFF_CAP_SECONDS)

    def _failed(self, reason: str, pause: bool, delay: float | None = None) -> None:
        """Record a failure: pause, or back off. Logs once per outage."""
        now = self.clock()
        try:
            with self.store.transaction() as db:
                _meta_set(db, "notify_failure", reason)
                if pause:
                    _meta_set(db, "notify_paused", "1")
                    db.execute("DELETE FROM meta WHERE key=?", ("notify_next_retry",))
                else:
                    attempts = _meta_int(db, "notify_attempts")
                    if delay is None:
                        delay = self._backoff(attempts)
                    _meta_set(db, "notify_attempts", attempts + 1)
                    _meta_set(db, "notify_next_retry", now + delay)
        except Exception:
            log.exception("ntfy sender: failure record failed")
        # The gate is remembered even when the record failed: the next poll must
        # not open a transaction to learn what this one already knows.
        self._paused, self._retry_at = pause, None if pause else now + (delay or 0.0)
        self._store_stamp = self._stamp()
        if not self._in_outage:
            # NT-R6: one log line per outage per scheduler process.
            log.error("ntfy notification failed: %s", reason)
            self._in_outage = True

    def _finish(self, section: dict, result: dict, seqs: list[int]) -> None:
        now = self.clock()
        if result.get("ok") is True:
            try:
                with self.store.transaction() as db:
                    for seq in seqs:
                        db.execute("DELETE FROM notify_outbox WHERE seq=?", (seq,))
                    _meta_set(db, "notify_last_sent", now)
                    _meta_set(db, "notify_last_accepted",
                              datetime.fromtimestamp(now, timezone.utc).isoformat())
                    db.execute("DELETE FROM meta WHERE key=?", ("notify_failure",))
                    db.execute("DELETE FROM meta WHERE key=?", ("notify_next_retry",))
                    db.execute("DELETE FROM meta WHERE key=?", ("notify_attempts",))
                    db.execute("DELETE FROM meta WHERE key=?", ("notify_expired",))
            except Exception:
                # At least once: the rows stay queued and go out again.
                log.exception("ntfy sender: accept record failed")
                return
            # The outage is over: the next failure logs again, and the gate is
            # open whatever it was before this send.
            self._in_outage = False
            self._paused, self._retry_at = False, None
            self._store_stamp = self._stamp()
            return
        status = result.get("status")
        reason = result.get("reason") or "the ntfy server could not be reached"
        if result.get("error") in ("timeout", "network") or status == 429 or \
                (isinstance(status, int) and 500 <= status < 600):
            # NT-R4: retried with backoff; a 429's Retry-After is honoured
            # inside [30 s, 10 min] and clamped to that range otherwise.
            delay = None
            if status == 429:
                delay = _retry_after_seconds(result.get("retry_after"), now)
            self._failed(reason, pause=False, delay=delay)
            return
        # NT-R4: 400, 401, 403, 404 (and anything else refused) are not
        # retried; the messages stay queued until the config changes or
        # `notify test` succeeds.
        self._failed(reason, pause=True)
