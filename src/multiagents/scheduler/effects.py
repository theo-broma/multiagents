"""Replay host Git effects after committing their store intent."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import threading

from .. import gitops
from ..tree import now
from . import model
from .results import Results
from .store import encode

# Failures a durable effect may raise. Anything else is a crash: its intent
# stays journalled and the next evaluation replays it.
FAILURES = (gitops.GitError, OSError, ValueError)
# Evaluations a disposal is retried in before it gives up and holds its node.
DISPOSAL_TRIES = 3

_locks = {}
_guard = threading.Lock()
_local = threading.local()


@contextmanager
def serialized(store):
    # Git serialization is independent of SQLite. Re-entry lets a sibling
    # finish an older integration before computing its own merge.
    key = str(store.file)
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        owned = getattr(_local, "owned", set())
        if key in owned:
            yield
            return
        with (store.directory / "git-effects.lock").open("a+") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _local.owned = owned | {key}
            try:
                yield
            finally:
                _local.owned = owned


def reply(store, db, intent, node, error=None):
    request = intent.get("request")
    if not request:
        return
    subject, request_id = request
    result = {"request_id": request_id, "ok": error is None}
    if error:
        result["error"] = error
    else:
        result["result"] = {**node, "plan_revision": int(store.meta(db, "plan_revision"))}
    db.execute("UPDATE requests SET reply=? WHERE subject=? AND request_id=?",
               (encode(result), subject, request_id))


def finish_completions(engine):
    store = engine.store
    changed = False
    with serialized(store):
        with store.transaction(write=False) as db:
            pending = [(n["id"], n["completion_pending"]) for n in store.nodes(db).values()
                       if n.get("completion_pending")]
        for id, intent in pending:
            results = Results(engine.paths, engine.service.configuration())
            generation = intent["generation"]
            try:
                results.move(intent["ref"], generation["commit"], "")
                failure = None
            except FAILURES as exc:
                failure = str(exc) or type(exc).__name__
            with engine.service.changed, store.transaction() as db:
                node = store.nodes(db)[id]
                if node.get("completion_pending") != intent:
                    continue
                node.pop("completion_pending")
                if failure:
                    # The retention ref is not ours: hold rather than replay
                    # forever. A relaunch recomputes the completion.
                    if node["state"] == "cancelled":
                        store.save_node(db, node)
                    else:
                        engine.hold(db, node, "integration_conflict", failure)
                else:
                    node["generations"].append(generation)
                    if node["state"] == "cancelled":
                        store.save_node(db, node)
                    else:
                        engine.complete(db, node, intent["outcome"])
                reply(store, db, intent, node)
                engine.service.changed.notify_all()
                changed = True
    return changed


def finish_operations(engine):
    store = engine.store
    with serialized(store):
        with store.transaction(write=False) as db:
            pending = [(n["id"], n["git_operation"]) for n in store.nodes(db).values()
                       if n.get("git_operation")]
        for id, intent in pending:
            with store.transaction(write=False) as db:
                nodes = store.nodes(db)
                node = nodes[id]
                if node.get("git_operation") != intent:
                    continue
            results = Results(engine.paths, engine.service.configuration())
            try:
                if intent["kind"] == "publish":
                    if "publication" not in intent:
                        publication = results.prepare_publication(node)
                        with store.transaction() as db:
                            node = store.nodes(db)[id]
                            if node.get("git_operation") != intent:
                                continue
                            intent = {**intent, "publication": publication}
                            node["git_operation"] = intent
                            store.save_node(db, node)
                    results.apply_publication(intent["publication"])
                else:
                    dispose(engine, results, intent, nodes)
            except (model.Refused, *FAILURES) as exc:
                error = exc.result if isinstance(exc, model.Refused) else {
                    "error": "merge_conflict" if intent["kind"] == "publish" else "disposal_failed",
                    "detail": str(exc) or type(exc).__name__}
                with engine.service.changed, store.transaction() as db:
                    nodes = store.nodes(db)
                    node = nodes[id]
                    if node.get("git_operation") != intent:
                        continue
                    tries = intent.get("tries", 0) + 1
                    if intent["kind"] == "dispose" and tries < DISPOSAL_TRIES:
                        # A partially applied disposal keeps its intent for
                        # replay in a later evaluation, a bounded number of times.
                        node["git_operation"] = {**intent, "tries": tries, "failure": error}
                        store.save_node(db, node)
                        continue
                    # Publication leaves main and the node unchanged (NC-R86);
                    # a disposal that keeps failing holds its partial result.
                    node.pop("git_operation")
                    if intent["kind"] == "dispose":
                        for child_id in intent["scope"]:
                            if nodes[child_id].pop("disposal_pending", None) and child_id != id:
                                store.save_node(db, nodes[child_id])
                        engine.hold(db, node, "disposal_failed", error.get("detail", error["error"]))
                    else:
                        store.save_node(db, node)
                    reply(store, db, intent, node, error)
                    engine.service.changed.notify_all()
                continue
            with engine.service.changed, store.transaction() as db:
                nodes = store.nodes(db)
                node = nodes[id]
                if node.get("git_operation") != intent:
                    continue
                node.pop("git_operation")
                if intent["kind"] == "publish":
                    node.update(published=intent["publication"]["target"], revision=node["revision"] + 1)
                    store.save_node(db, node)
                    store.transition(db, "published", id, {"commit": node["published"]})
                else:
                    flipped = []
                    for child_id in intent["scope"]:
                        child = nodes[child_id]
                        child.pop("disposal_pending", None)
                        if child["state"] not in {"done", "cancelled"}:
                            child["state"] = "cancelled"
                            flipped.append(child_id)
                        child.update(disposed=now(), revision=child["revision"] + 1)
                        store.save_node(db, child)
                    for alias in intent["aliases"]:
                        db.execute("DELETE FROM aliases WHERE id=?", (alias,))
                    store.transition(db, "disposed", id)
                    # NT-R3 (round-2 fix 2): a top-level node that becomes
                    # `cancelled` through disposal sends `done` like any
                    # other terminal top-level node. The scheduler's
                    # notification scan reads `cancelled` transitions, so
                    # each node the disposal actually cancelled gets one —
                    # and a node that was already finished gets none, so it
                    # is not announced again.
                    for child_id in flipped:
                        store.transition(db, "cancelled", child_id)
                reply(store, db, intent, node)
                engine.service.changed.notify_all()


def dispose(engine, results, intent, nodes):
    for ref in intent["refs"]:
        if results.tip(ref):
            results.git("update-ref", "--no-deref", "-d", ref, check=True)
    authority = engine.runner.authority
    for id in intent["scope"]:
        for run in nodes[id]["runs"]:
            record = authority.get(run["run_id"]) if authority else None
            if record and record.get("worktree"):
                checkout = Path(record["worktree"])
                if (checkout.exists() or checkout.is_symlink()) and not authority.remove_worktree(checkout, recorded=True):
                    raise model.Refused("disposal_failed")
                authority.clear(run["run_id"], worktree=True, branch=True)
