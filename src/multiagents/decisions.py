"""Versioned decisions in the tree's locked question store.

Legacy callers use projections here; only a transition migrates an old record.
Events follow the commit and contain routing metadata only.
"""
from __future__ import annotations

import base64
import builtins
import copy
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

from .redact import scrub
from .tree import now


class DecisionError(ValueError):
    def __init__(self, code, **details):
        self.code = code
        self.__dict__.update(details)
        message = code + (": use decisions.answer" if code == "wrong_mode" else "")
        super().__init__(message, details)


def _struct(value, field, code="invalid_record"):
    if scrub(value) != value:
        raise DecisionError(code, field=field)
    return copy.deepcopy(value)


def _actor(by, code="invalid_record", asker=False):
    roles = ("orchestrator", "initializer", "agent") if asker else None
    if (not isinstance(by, dict) or set(by) != {"role", "agent", "channel"}
            or any(not isinstance(v, str) for v in by.values())
            or (roles and by["role"] not in roles)):
        raise DecisionError(code, field="by")
    return _struct(by, "by", code)


def _line(text, limit):
    line = str(text or "").splitlines()[0] if str(text or "").splitlines() else ""
    return (line[:limit - 1] + "…" if len(line) > limit else line) or "(no topic)"


def _project(raw):
    if raw.get("schema_version") == 1:
        return copy.deepcopy(raw)
    by = {"role": "agent", "agent": raw.get("agent", ""), "channel": "legacy"}
    rec = _record(raw["id"], by, "orchestrator", raw.get("asked_at", 0),
                  _line(raw.get("topic"), 60), _line(raw.get("topic"), 200),
                  raw.get("question") or "(no question)",
                  {"blocks": "", "if_unanswered": ""}, "free_text", [], [], True,
                  raw.get("proposed_default", ""), [])
    if raw.get("status") == "answered":
        actor = {"role": raw.get("answered_by", ""), "agent": "", "channel": "legacy"}
        rec.update(status="answered", revision=2,
                   answer={"options": [], "comment": "", "free_text": raw.get("answer", ""),
                           "answered_by": actor, "answered_at": raw.get("answered_at"),
                           "revision": 1, "request_id": None})
        rec["history"].append({"status": "answered", "at": raw.get("answered_at"), "by": actor})
    return rec


def _record(id, by, audience, at, topic, title, context, implications, mode,
            options, recommended, allow_free_text, default_text, refs):
    return dict(id=id, schema_version=1, revision=1,
                asked_by={"role": by["role"], "agent": by["agent"]}, audience=audience,
                asked_at=at, topic=topic, title=title, context=context,
                implications=implications, mode=mode, options=options,
                recommended=recommended, allow_free_text=allow_free_text,
                default_text=default_text, refs=refs, status="open",
                history=[{"status": "open", "at": at, "by": by}],
                answer=None, delivery=None, handled=None)


def _emit(tree, rec, transition):
    asker = rec["asked_by"]
    tree.emit(asker["agent"] or asker["role"], "decision", transition=transition,
              id=rec["id"], revision=rec["revision"], audience=rec["audience"],
              asker_role=asker["role"])


def _insert(tree, rec, supersedes=None, enforce=False):
    old = None
    with tree.transaction() as data:
        if supersedes is not None:
            index, old = _find(data, supersedes)
            if old["status"] != "open":
                _not_open(old)
            if enforce:
                authorize(old, rec["history"][0]["by"], "supersede")
        used = {q["id"] for q in data["questions"] if _usable(q)}
        prefix, length = ("q-", 6) if rec["id"].startswith("q-") else ("d-", 8)
        while rec["id"] in used:
            rec["id"] = prefix + uuid.uuid4().hex[:length]
        if old is not None:
            _advance(old, rec["history"][0]["by"], "superseded", superseded_by=rec["id"])
            data["questions"][index] = old
        data["questions"].append(rec)
    _emit(tree, rec, "created")
    if old is not None:
        _emit(tree, old, "superseded")
    return copy.deepcopy(rec)


MAX_REF_BYTES = 1024 * 1024
MAX_REFS = 8


def authorize(record, actor, operation, *, delegate=False):
    """The single authority function (D-UD2.2): None if `actor` may perform
    `operation` on `record`, else DecisionError("forbidden", reason=...).
    `delegate` lets a run answer an orchestrator question through the legacy
    answer_question path; it never opens a user decision."""
    ok = (isinstance(actor, dict) and set(actor) == {"role", "agent", "channel"}
          and all(isinstance(v, str) for v in actor.values()))
    role, channel = (actor["role"], actor["channel"]) if ok else ("", "")
    if operation == "answer":
        if ok and role == "user" and channel in ("cli", "monitor"):
            return None
        if record["audience"] == "orchestrator":
            if ok and channel == "mcp_root" and role == "orchestrator":
                return None
            if ok and delegate and channel == "mcp_run" and role == "agent":
                return None
            raise DecisionError("forbidden", reason="not_answerable_here")
        raise DecisionError("forbidden", reason="not_answerable_here" if channel == "mcp_run" else "user_only")
    asker = record["asked_by"]["role"]
    if ok and channel == "mcp_root" and role in ("orchestrator", "initializer"):
        if operation in ("withdraw", "supersede") and role == asker:
            return None
        if operation == "mark_handled" and (role == asker or (role == "orchestrator" and asker == "agent")):
            return None
        if operation == "escalate" and role == "orchestrator":
            return None
    raise DecisionError("forbidden", reason="not_asker")


def _root(tree):
    return Path(tree.path).parent.parent


def _refs_dir(tree):
    return _root(tree) / ".multiagents" / "decisions" / "snapshots"


def _resolve(root, path):
    """The regular file `path` names inside `root`, or None."""
    if (not isinstance(path, str) or not path.strip() or "\0" in path
            or os.path.isabs(path) or ".." in path.split("/")):
        return None
    try:
        base = (root).resolve(strict=True)
        target = (base / path).resolve(strict=True)
        target.relative_to(base)
        if not target.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return target


def _read(root, target):
    """The bytes of `target`, read through one descriptor: opened without
    following a symlink or blocking on a FIFO, then checked with fstat (a
    regular file) and against `root` (by the descriptor's own realpath)."""
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        # O_NOFOLLOW guards only the last component: prove containment on the
        # opened descriptor, not on a name that may have been swapped.
        Path(os.path.realpath(f"/proc/self/fd/{handle.fileno()}")).relative_to(
            root.resolve(strict=True))
        return handle.read(MAX_REF_BYTES + 1)


def _commit(root, spelling):
    if (not isinstance(spelling, str) or not spelling or spelling.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in spelling)):
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                              "--end-of-options", spelling + "^{commit}"],
                             capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None


def _bind_refs(tree, refs):
    """Validated refs as recorded, plus the snapshots to store."""
    if len(refs) > MAX_REFS:
        raise DecisionError("invalid_record", field="refs")
    root, bound, snapshots = _root(tree), [], {}
    for i, ref in enumerate(refs):
        field = f"refs[{i}]"
        if not isinstance(ref, dict) or set(ref) not in ({"path"}, {"path", "sha256"}, {"commit"}):
            raise DecisionError("invalid_record", field=field)
        if "commit" in ref:
            sha = _commit(root, ref["commit"])
            if sha is None:
                raise DecisionError("invalid_record", field=field)
            bound.append({"commit": sha})
            continue
        target = _resolve(root, ref["path"])
        try:
            data = _read(root, target) if target else None
        except (OSError, RuntimeError, ValueError):
            data = None
        if data is None or len(data) > MAX_REF_BYTES:
            raise DecisionError("invalid_record", field=field)
        digest = hashlib.sha256(data).hexdigest()
        if ref.get("sha256", digest) != digest:
            raise DecisionError("invalid_record", field=field)
        snapshots[digest] = data
        bound.append({"path": ref["path"], "sha256": digest})
    return bound, snapshots


def _store(tree, snapshots):
    if not snapshots:
        return
    directory = _refs_dir(tree)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    for digest, data in snapshots.items():
        final = directory / digest
        try:
            if hashlib.sha256(final.read_bytes()).hexdigest() == digest:
                continue
        except OSError:
            pass
        tmp = directory / f".{digest}.{uuid.uuid4().hex[:8]}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)


def check_refs(tree, id):
    """Do the approved path refs still hold the bytes they were approved with?"""
    rec = get(tree, id)
    if rec is None:
        raise DecisionError("unknown")
    changed, missing = [], []
    for ref in rec["refs"]:
        if "path" not in ref:
            continue
        root = _root(tree)
        target = _resolve(root, ref["path"])
        try:
            data = _read(root, target) if target else None
        except (OSError, RuntimeError, ValueError):
            data = None
        if data is None:
            missing.append(ref["path"])
        elif len(data) > MAX_REF_BYTES or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            changed.append(ref["path"])
    return {"ok": not changed and not missing, "changed": changed, "missing": missing}


def snapshot(tree, id, path):
    """The bytes stored for `path` when decision `id` was asked."""
    rec = get(tree, id)
    if rec is None:
        raise DecisionError("unknown")
    ref = next((r for r in rec["refs"] if r.get("path") == path), None)
    if ref is None:
        raise DecisionError("invalid_argument", field="path")
    try:
        data = (_refs_dir(tree) / ref["sha256"]).read_bytes()
    except OSError:
        raise DecisionError("unknown", field="snapshot") from None
    if hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise DecisionError("unknown", field="snapshot")
    return data


def create(tree, *, by, audience, topic, title, context, implications, mode,
           options=(), min=None, max=None, recommended=(), allow_free_text=True,
           default_text="", refs=(), supersedes=None, enforce=False):
    by = _actor(by, asker=True)
    for field, value, limit in (("topic", topic, 60), ("title", title, 200),
                                 ("context", context, 20000)):
        value = scrub(value)
        if (not isinstance(value, str) or not 1 <= len(value) <= limit
                or (field != "context" and ("\n" in value or "\r" in value))):
            raise DecisionError("invalid_record", field=field)
    topic, title, context = scrub(topic), scrub(title), scrub(context)
    if audience not in ("orchestrator", "user"):
        raise DecisionError("invalid_record", field="audience")
    if mode not in ("single", "multi", "free_text"):
        raise DecisionError("invalid_record", field="mode")
    if (not isinstance(implications, dict) or set(implications) != {"blocks", "if_unanswered"}
            or any(not isinstance(v, str) for v in implications.values())):
        raise DecisionError("invalid_record", field="implications")
    opts = []
    if not isinstance(options, (builtins.list, tuple)):
        raise DecisionError("invalid_record", field="options")
    for i, option in enumerate(options):
        field = f"options[{i}]"
        if not isinstance(option, dict) or set(option) != {"id", "label", "description", "implies"}:
            raise DecisionError("invalid_record", field=field)
        _struct(option["id"], field + ".id")
        if (not isinstance(option["id"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", option["id"])
                or any(not isinstance(v, str) for v in option.values())):
            raise DecisionError("invalid_record", field=field)
        opts.append(scrub(option))
    ids = [o["id"] for o in opts]
    if len(set(ids)) != len(ids) or (mode == "single" and len(ids) < 2) or (mode == "free_text" and ids):
        raise DecisionError("invalid_record", field="options")
    for field, value in (("min", min), ("max", max)):
        _struct(value, field)
        if mode != "multi" and value is not None:
            raise DecisionError("invalid_record", field=field)
    if mode == "multi":
        if type(min) is not int or min < 1:
            raise DecisionError("invalid_record", field="min")
        if type(max) is not int or not min <= max <= len(ids):
            raise DecisionError("invalid_record", field="max")
    recommended = _struct(recommended, "recommended")
    if (not isinstance(recommended, (builtins.list, tuple))
            or any(r not in ids for r in recommended)
            or (mode == "single" and len(recommended) > 1)):
        raise DecisionError("invalid_record", field="recommended")
    if type(allow_free_text) is not bool or (mode == "free_text" and not allow_free_text):
        raise DecisionError("invalid_record", field="allow_free_text")
    if not isinstance(default_text, str):
        raise DecisionError("invalid_record", field="default_text")
    refs = _struct(refs, "refs")
    if not isinstance(refs, (builtins.list, tuple)):
        raise DecisionError("invalid_record", field="refs")
    refs, snapshots = _bind_refs(tree, refs)
    if supersedes is not None and not isinstance(supersedes, str):
        raise DecisionError("invalid_argument", field="supersedes")
    rec = _record("d-" + uuid.uuid4().hex[:8], by, audience, now(), topic, title,
                  context, scrub(implications), mode, opts, builtins.list(recommended),
                  allow_free_text, scrub(default_text), builtins.list(refs))
    if mode == "multi":
        rec.update(min=min, max=max)
    _store(tree, snapshots)
    return _insert(tree, rec, supersedes, enforce)


def _usable(raw):
    """UD-C13: whether a stored record has the one field every pass keys on.

    The store sits in tree.json, which a docker agent can write (SG-R7), and
    a hand edit or an older writer can leave a record without a string id.
    Passes skip such a record rather than raising on a neighbour."""
    return isinstance(raw, dict) and isinstance(raw.get("id"), str) and bool(raw["id"])


def get(tree, id):
    raw = next((q for q in tree.read()["questions"] if _usable(q) and q["id"] == id), None)
    return _project(raw) if raw is not None else None


def _key(rec):
    if rec["status"] == "open":
        return (0, rec["asked_at"], rec["id"])
    return (1, -(rec["history"][-1]["at"] or 0), rec["id"])


def list(tree, *, status=None, audience=None, cursor=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise DecisionError("invalid_argument", field="limit")
    last = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (set(decoded) != {"v", "status", "audience", "key"} or decoded["v"] != 1
                    or decoded["status"] != status or decoded["audience"] != audience):
                raise ValueError()
            last = tuple(decoded["key"])
            if (len(last) != 3 or type(last[0]) is not int or last[0] not in (0, 1)
                    or type(last[1]) not in (int, float) or not math.isfinite(last[1])
                    or not isinstance(last[2], str)):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise DecisionError("invalid_cursor") from None
    records = []
    for raw in tree.read()["questions"]:
        try:
            rec = _project(raw)
            key = _key(rec)
            if ((status is None or rec["status"] == status)
                    and (audience is None or rec["audience"] == audience)
                    and (last is None or key > last)):
                records.append((key, rec))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue    # UD-C13: a damaged neighbour never breaks the listing
    records.sort(key=lambda pair: pair[0])
    page = [rec for _, rec in records[:limit]]
    following = None
    if len(records) > limit:
        following = base64.urlsafe_b64encode(json.dumps(dict(
            v=1, status=status, audience=audience, key=records[limit - 1][0])).encode()).decode()
    return {"decisions": page, "next": following}


def _find(data, id):
    for i, raw in enumerate(data["questions"]):
        if _usable(raw) and raw["id"] == id:
            return i, _project(raw)
    raise DecisionError("unknown")


def _revision(rec, revision):
    if type(revision) is not int or revision != rec["revision"]:
        raise DecisionError("stale_revision", revision=rec["revision"])


def _not_open(rec):
    details = {"status": rec["status"]}
    if rec["status"] == "superseded":
        details["superseded_by"] = rec["history"][-1]["superseded_by"]
    raise DecisionError("not_open", **details)


def _advance(rec, by, status, **details):
    rec["status"] = status
    rec["revision"] += 1
    rec["history"].append(dict(status=status, at=now(), by=by, **details))


def _answer(tree, id, *, revision, by, options=(), comment="", free_text="",
            request_id=None, legacy=False, enforce=False, delegate=False):
    duplicate = False
    with tree.transaction() as data:
        index, rec = _find(data, id)
        if enforce:
            authorize(rec, by, "answer", delegate=delegate)
        if legacy and rec["mode"] != "free_text":
            raise DecisionError("wrong_mode")
        if rec["status"] in ("withdrawn", "superseded"):
            _not_open(rec)
        if legacy and rec["status"] in ("answered", "handled"):
            return {"decision": rec, "already_answered": True}
        comment, free_text = scrub(comment), scrub(free_text)
        if rec["status"] in ("answered", "handled"):
            old = rec["answer"]
            equal = (isinstance(options, (builtins.list, tuple))
                     and all(isinstance(o, str) for o in options)
                     and set(options) == set(old["options"])
                     and len(options) == len(old["options"])
                     and comment == old["comment"] and free_text == old["free_text"])
            if not equal:
                raise DecisionError("conflicting_answer")
            duplicate = True
        else:
            if not legacy:
                _revision(rec, revision)
            by = _actor(by, "invalid_answer")
            _struct(options, "options", "invalid_answer")
            _struct(request_id, "request_id", "invalid_answer")
            if request_id is not None and not isinstance(request_id, str):
                raise DecisionError("invalid_answer", field="request_id")
            if not isinstance(comment, str) or len(comment) > 4000:
                raise DecisionError("invalid_answer", field="comment")
            if not isinstance(free_text, str) or len(free_text) > 20000:
                raise DecisionError("invalid_answer", field="free_text")
            if (not isinstance(options, (builtins.list, tuple))
                    or any(not isinstance(o, str) for o in options)
                    or len(set(options)) != len(options)
                    or any(o not in {o["id"] for o in rec["options"]} for o in options)):
                raise DecisionError("invalid_answer", field="options")
            text = bool(free_text.strip())
            if text:
                valid = not options and rec["allow_free_text"]
            elif rec["mode"] == "free_text":
                valid = False
            elif rec["mode"] == "single":
                valid = len(options) == 1
            else:
                valid = rec["min"] <= len(options) <= rec["max"]
            if not valid:
                raise DecisionError("invalid_answer")
            rec["answer"] = dict(options=builtins.list(options), comment=comment,
                                 free_text=free_text, answered_by=by, answered_at=now(),
                                 revision=rec["revision"], request_id=request_id)
            if rec["refs"]:
                rec["answer"]["approved_refs"] = copy.deepcopy(rec["refs"])
            _advance(rec, by, "answered")
            data["questions"][index] = rec
    if not duplicate:
        _emit(tree, rec, "answered")
    return {"decision": copy.deepcopy(rec), "already_answered": duplicate}


def answer(tree, id, *, revision, by, options=(), comment="", free_text="", request_id=None,
           enforce=False):
    return _answer(tree, id, revision=revision, by=by, options=options, comment=comment,
                   free_text=free_text, request_id=request_id, enforce=enforce)


_OPERATION = {"withdrawn": "withdraw", "superseded": "supersede", "handled": "mark_handled",
              "escalated": "escalate"}


def _transition(tree, id, revision, by, operation, *, reason="", outcome=None,
                superseded_by=None, enforce=False):
    with tree.transaction() as data:
        index, rec = _find(data, id)
        if enforce:
            authorize(rec, by, _OPERATION[operation])
        if operation == "handled":
            if rec["status"] != "answered":
                raise DecisionError("bad_transition", status=rec["status"])
        elif rec["status"] != "open":
            _not_open(rec)
        _revision(rec, revision)
        by = _actor(by)
        reason = scrub(reason)
        details = {}
        if operation in ("withdrawn", "handled"):
            if operation == "handled" and outcome not in ("applied", "not_applied", "error"):
                raise DecisionError("invalid_argument", field="outcome")
            if (not isinstance(reason, str)
                    or (not reason.strip() and (operation == "withdrawn" or outcome != "applied"))):
                raise DecisionError("invalid_argument", field="reason")
            details["reason"] = reason
        if operation == "superseded":
            _struct(superseded_by, "superseded_by", "invalid_argument")
            other = next((q for q in data["questions"]
                          if _usable(q) and q["id"] == superseded_by), None)
            if other is None or superseded_by == id or other["status"] != "open":
                raise DecisionError("invalid_argument", field="superseded_by")
            details["superseded_by"] = superseded_by
        if operation == "escalated":
            if rec["audience"] == "user":
                raise DecisionError("bad_transition")
            rec["audience"] = "user"
        if operation == "handled":
            rec["handled"] = dict(at=now(), by=by, outcome=outcome, reason=reason)
        _advance(rec, by, "open" if operation == "escalated" else operation, **details)
        data["questions"][index] = rec
    _emit(tree, rec, operation)
    return copy.deepcopy(rec)


REDELIVERY_WINDOW = 900


def _skipped(raw, exc):
    ident = raw.get("id") if isinstance(raw, dict) else None
    ident = ident if isinstance(ident, str) and ident else "(no id)"
    return (f"{ident}: skipped, unreadable decision record "
            f"({type(exc).__name__}: {exc})")[:200]


def scan_for_delivery(tree, *, role, session, now):
    """UD-C11/UD-C13: one delivery pass for asker role `role` at `session`.

    Returns `(due, skipped)`: what `due_for_delivery` returns, and one line
    per record this pass could not read. tree.json is agent-writable
    (SG-R7), and a watcher runs this on every poll, so a damaged record is
    skipped — never delivered, never allowed to raise past the pass that
    reports the healthy decisions and the job ends beside them — and the
    read is `Tree.peek_questions`, which is read-only: an unparseable tree
    is no decisions this pass, with no recovery copy, no rewrite and no
    warning to flood the report with."""
    if role not in ("orchestrator", "initializer"):
        raise DecisionError("invalid_argument", field="role")
    due, skipped = [], []
    for raw in tree.peek_questions() or []:
        try:
            rec = _project(raw)
            if rec["status"] != "answered" or rec["asked_by"]["role"] != role:
                continue
            delivery = rec["delivery"]
            if delivery is not None and delivery.get("session") == session:
                at = delivery.get("delivered_at") or 0
                if isinstance(at, bool) or not isinstance(at, (int, float)):
                    raise TypeError(f"delivered_at is {type(at).__name__}")
                if now - at <= REDELIVERY_WINDOW:
                    continue
            due.append(rec)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            skipped.append(_skipped(raw, exc))
    return due, skipped


def due_for_delivery(tree, *, role, session, now):
    """UD-C11: the answered decisions asker-role `role` must have reported to
    `session` now: never delivered, delivered to another session, or last
    delivered more than one window ago. Terminal statuses never resume. A
    record the pass cannot read is skipped and never delivered (UD-C13)."""
    due, _ = scan_for_delivery(tree, role=role, session=session, now=now)
    return due


def record_delivery(tree, id, *, session, now):
    """UD-C11: record one delivery. Not a transition: no history entry, the
    status stays `answered`, only the revision moves."""
    if not isinstance(session, str) or not session:
        raise DecisionError("invalid_argument", field="session")
    with tree.transaction() as data:
        index, rec = _find(data, id)
        if rec["status"] != "answered":
            raise DecisionError("bad_transition", status=rec["status"])
        rec["delivery"] = {"delivered_at": now, "session": session}
        rec["revision"] += 1
        data["questions"][index] = rec
    _emit(tree, rec, "delivered")
    return copy.deepcopy(rec)


def withdraw(tree, id, *, revision, by, reason, enforce=False):
    return _transition(tree, id, revision, by, "withdrawn", reason=reason, enforce=enforce)


def supersede(tree, id, *, revision, by, superseded_by):
    return _transition(tree, id, revision, by, "superseded", superseded_by=superseded_by)


def mark_handled(tree, id, *, revision, by, outcome, reason="", enforce=False):
    return _transition(tree, id, revision, by, "handled", outcome=outcome, reason=reason,
                       enforce=enforce)


def escalate(tree, id, *, revision, by, enforce=False):
    return _transition(tree, id, revision, by, "escalated", enforce=enforce)


def _legacy(rec):
    if "schema_version" not in rec:
        return copy.deepcopy(rec)
    ans = rec["answer"]
    return dict(id=rec["id"], agent=rec["asked_by"]["agent"], topic=rec["topic"],
                question=rec["context"], proposed_default=rec["default_text"],
                asked_at=rec["asked_at"], status=rec["status"],
                answer=ans["free_text"] if ans else "",
                answered_at=ans["answered_at"] if ans else None,
                answered_by=ans["answered_by"]["role"] if ans else "")


def legacy_questions(tree, *, status="open", agent=None):
    out = []
    for raw in tree.read()["questions"]:
        try:
            if not _usable(raw) or not raw["id"].startswith("q-"):
                continue
            rec = _legacy(raw)
            if ((status is None or rec["status"] == status)
                    and (agent is None or rec["agent"] == agent)):
                out.append(rec)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            continue    # UD-C13: a damaged neighbour never breaks the listing
    return out


def get_question(tree, id):
    raw = next((q for q in tree.read()["questions"] if _usable(q) and q["id"] == id), None)
    return _legacy(raw) if raw is not None else None


def add_question(tree, agent_id, topic, question, proposed=""):
    by = {"role": "agent", "agent": str(agent_id or ""), "channel": "legacy"}
    topic = scrub(str(topic or ""))
    question = scrub(str(question or ""))
    proposed = scrub(str(proposed or ""))
    rec = scrub(_record("q-" + uuid.uuid4().hex[:6], by, "orchestrator", now(),
                        _line(topic, 60), _line(topic, 200), question or "(no question)",
                        {"blocks": "", "if_unanswered": ""}, "free_text", [], [], True,
                        proposed, []))
    rec = _insert(tree, rec)
    result = _legacy(rec)
    tree.emit(result["agent"], "question", topic=result["topic"],
              question=result["question"][:300], proposed=result["proposed_default"][:200],
              question_id=result["id"])
    return result


def answer_question(tree, id, text, answered_by="user", actor=None, delegate=False):
    try:
        out = _answer(tree, id, revision=None,
                      by=actor or {"role": answered_by, "agent": "", "channel": "legacy"},
                      free_text=text, legacy=True, enforce=actor is not None,
                      delegate=delegate)
    except DecisionError as exc:
        if exc.code == "unknown":
            return None
        raise
    result = _legacy(out["decision"])
    if out["already_answered"]:
        result["already_answered"] = True
    else:
        tree.emit(result["agent"], "answered", question_id=id,
                  answered_by=result["answered_by"], answer=result["answer"][:300])
    return result
