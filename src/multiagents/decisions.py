"""Versioned decisions in the tree's locked question store.

Legacy callers use projections here; only a transition migrates an old record.
Events follow the commit and contain routing metadata only.
"""
from __future__ import annotations

import base64
import builtins
import copy
import json
import math
import re
import uuid

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


def _insert(tree, rec):
    with tree.transaction() as data:
        used = {q["id"] for q in data["questions"]}
        prefix, length = ("q-", 6) if rec["id"].startswith("q-") else ("d-", 8)
        while rec["id"] in used:
            rec["id"] = prefix + uuid.uuid4().hex[:length]
        data["questions"].append(rec)
    _emit(tree, rec, "created")
    return copy.deepcopy(rec)


def create(tree, *, by, audience, topic, title, context, implications, mode,
           options=(), min=None, max=None, recommended=(), allow_free_text=True,
           default_text="", refs=()):
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
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) not in ({"path", "sha256"}, {"commit"})
                or any(not isinstance(v, str) for v in ref.values())):
            raise DecisionError("invalid_record", field="refs")
    rec = _record("d-" + uuid.uuid4().hex[:8], by, audience, now(), topic, title,
                  context, scrub(implications), mode, opts, builtins.list(recommended),
                  allow_free_text, scrub(default_text), builtins.list(refs))
    if mode == "multi":
        rec.update(min=min, max=max)
    return _insert(tree, rec)


def get(tree, id):
    raw = next((q for q in tree.read()["questions"] if q["id"] == id), None)
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
    records = [_project(q) for q in tree.read()["questions"]]
    records = sorted((r for r in records if (status is None or r["status"] == status)
                      and (audience is None or r["audience"] == audience)
                      and (last is None or _key(r) > last)), key=_key)
    page, following = records[:limit], None
    if len(records) > limit:
        following = base64.urlsafe_b64encode(json.dumps(dict(
            v=1, status=status, audience=audience, key=_key(page[-1]))).encode()).decode()
    return {"decisions": page, "next": following}


def _find(data, id):
    for i, raw in enumerate(data["questions"]):
        if raw["id"] == id:
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
            request_id=None, legacy=False):
    duplicate = False
    with tree.transaction() as data:
        index, rec = _find(data, id)
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
            _advance(rec, by, "answered")
            data["questions"][index] = rec
    if not duplicate:
        _emit(tree, rec, "answered")
    return {"decision": copy.deepcopy(rec), "already_answered": duplicate}


def answer(tree, id, *, revision, by, options=(), comment="", free_text="", request_id=None):
    return _answer(tree, id, revision=revision, by=by, options=options, comment=comment,
                   free_text=free_text, request_id=request_id)


def _transition(tree, id, revision, by, operation, *, reason="", outcome=None, superseded_by=None):
    with tree.transaction() as data:
        index, rec = _find(data, id)
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
            other = next((q for q in data["questions"] if q["id"] == superseded_by), None)
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


def withdraw(tree, id, *, revision, by, reason):
    return _transition(tree, id, revision, by, "withdrawn", reason=reason)


def supersede(tree, id, *, revision, by, superseded_by):
    return _transition(tree, id, revision, by, "superseded", superseded_by=superseded_by)


def mark_handled(tree, id, *, revision, by, outcome, reason=""):
    return _transition(tree, id, revision, by, "handled", outcome=outcome, reason=reason)


def escalate(tree, id, *, revision, by):
    return _transition(tree, id, revision, by, "escalated")


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
    return [_legacy(q) for q in tree.read()["questions"]
            if q["id"].startswith("q-") and (status is None or q.get("status") == status)
            and (agent is None or _legacy(q)["agent"] == agent)]


def get_question(tree, id):
    raw = next((q for q in tree.read()["questions"] if q["id"] == id), None)
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


def answer_question(tree, id, text, answered_by="user"):
    try:
        out = _answer(tree, id, revision=None,
                      by={"role": answered_by, "agent": "", "channel": "legacy"},
                      free_text=text, legacy=True)
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
