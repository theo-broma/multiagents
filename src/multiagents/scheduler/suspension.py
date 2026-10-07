"""Replayable supervisor commands for interrupted turns (NC-R40/NC-R69)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os

from .. import gitops

from ..tree import now, ACTIVE, PAUSED
from . import windows, sessions
from .engine import attempts, save_attempt


def instant(clock_file):
    if clock_file:
        return datetime.fromisoformat(Path(clock_file).read_text().strip().replace("Z", "+00:00")).timestamp()
    return now()


def operator_stop(store, db, run_id):
    from .model import Refused
    attempt = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
    if attempt is None:
        raise Refused("not_found")
    node = store.nodes(db)[attempt["node_id"]]
    if (node["state"] == "held" and (node.get("hold") or {}).get("reason") == "stopped_by_orchestrator"):
        return {"admitted": True}
    if node["state"] == "suspended" or attempt.get("window_stop"):
        attempt.update(cancel_requested=True, cancel_kind="operator")
        if attempt["state"] == "suspended":
            attempt["state"] = "abandoned"
            db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (run_id,))
        save_attempt(db, attempt)
        node.update(state="held", hold={"reason": "stopped_by_orchestrator", "detail": ""},
                    revision=node["revision"] + 1)
        store.save_node(db, node)
        store.transition(db, "stopped_by_orchestrator", node["id"], node["hold"])
        store.transition(db, "held", node["id"], node["hold"])
    return {"admitted": True}


def cancelled(store, db, current):
    """Complete a node cancellation that waited for the interrupted turn."""
    if not current.get("cancel_requested"):
        return
    current.update(cancel_confirmed=True)
    current.pop("launch_in_progress", None)
    if current.get("cancel_kind") == "node":
        node = store.nodes(db)[current["node_id"]]
        if node["state"] == "held" and (node.get("hold") or {}).get("reason") == "termination_unconfirmed":
            node.update(state="cancelled", hold=None, outcome=None, revision=node["revision"] + 1)
            store.save_node(db, node)
            store.transition(db, "cancelled", node["id"])


def natural_result(paths, run):
    """Read this turn's own completion outside the store lock (NC-R40).

    Internal stops never finalize result.json. The wrapper's cause covers the
    race before finalization, even if the child's TERM handler returns zero.
    Neither file proves process death; callers must separately confirm it.
    """
    directory = paths.run_dir(run.id)
    turn = run.turn_started_at or 0
    try:
        fd = gitops._open_file_beneath(*gitops.beneath(directory), "result.json", os.O_RDONLY)
        with os.fdopen(fd, "rb") as file:
            timestamp = os.fstat(file.fileno()).st_mtime
            raw = file.read(16 * 1024 * 1024 + 1)
        if len(raw) <= 16 * 1024 * 1024:
            result = json.loads(raw)
            recorded_turn = result.get("turn_started_at")
            # Pre-NC-R40 artifacts have no turn id. Their mtime is read from
            # the same descriptor as the result, never from a replacement.
            fresh = bool(turn and timestamp >= turn) if recorded_turn is None else recorded_turn == turn
            if fresh and result.get("status") in {"done", "failed", "rate_limited"}:
                return {"id": run.id, "status": result["status"],
                        "session_id": result.get("session_id") or run.session_id,
                        "turn_started_at": turn}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raw = gitops._read_beneath(*gitops.beneath(directory), "exit_reason.json", 4096)
    if raw is not None:
        try:
            result = json.loads(raw)
            if (turn and result["started_at"] >= turn and result["cause"] == "natural"
                    and type(result["exit_code"]) is int):
                return {"id": run.id, "status": "done" if result["exit_code"] == 0 else "failed",
                        "session_id": run.session_id, "turn_started_at": turn}
        except (ValueError, TypeError, KeyError):
            pass
    return None


def record_completion(store, db, current, result):
    """Drop an unconfirmed stop superseded by the turn's own completion.

    The transaction orders completion against suspension confirmation. Once
    that turn is suspended, a late result cannot turn it into a completion.
    """
    if (current.get("window_resume") or current.get("cancel_requested")
            or current["state"] != "launched"
            or (current.get("turn_started_at") is not None
                and current["turn_started_at"] != result.get("turn_started_at"))):
        return False
    node = store.nodes(db)[current["node_id"]]
    if node["state"] == "suspended":
        return False
    current["window_completion"] = result
    current["state"] = "launched"
    for key in ("window_stop", "window_stop_started", "window_suspended_at"):
        current.pop(key, None)
    if node["state"] == "held" and node["revision"] == current.get("window_hold_revision"):
        node.update(state="running", hold=None, outcome=None, revision=node["revision"] + 1)
        store.save_node(db, node)
    save_attempt(db, current)
    return True


def completed(store, run_id, result):
    with store.transaction() as db:
        current = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
        if not current or not record_completion(store, db, current, result):
            return False
    store.mirror()
    return True


def starting(store, run_id):
    """Reserve suspension before Runner can publish a finished tree entry."""
    with store.transaction() as db:
        current = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
        if (not current or current["state"] not in {"launched", "suspended"}
                or current.get("capture_intent") or current.get("window_completion")):
            return False
        current.update(window_stop=True, window_stop_started=True)
        save_attempt(db, current)
    return True


def record_suspension(store, db, current, stopped_run):
    """Record death proof and the interrupted activation, without tree I/O."""
    node = store.nodes(db)[current["node_id"]]
    current["state"] = "abandoned" if current.get("cancel_requested") else "suspended"
    current.setdefault("window_suspended_at", now())
    cancelled(store, db, current)
    if not current.get("cancel_requested") and (node["state"] == "running" or (node["state"] == "held"
            and node["revision"] == current.get("window_hold_revision"))):
        node.update(state="suspended", hold=None, outcome=None, revision=node["revision"] + 1)
        store.save_node(db, node)
        store.transition(db, "suspended", node["id"], {"run_id": current["run_id"]})
    save_attempt(db, current)
    if current.get("alias_id") and stopped_run:
        binding = sessions.aliases(db)[current["alias_id"]]
        binding.update(session_id=stopped_run.session_id, last_run=stopped_run.id)
        sessions.save_alias(db, binding)


def confirmed(store, run_id, stopped_run):
    with store.transaction() as db:
        current = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
        if not current or not current.get("window_stop") or current.get("capture_intent"):
            return False
        record_suspension(store, db, current, stopped_run)
    store.mirror()
    return True


def resumed(store, run_id):
    """Publish confirmed launch before waiting for the provider response."""
    with store.transaction() as db:
        current = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
        if not current or not current.get("window_resume"):
            return
        node = store.nodes(db)[current["node_id"]]
        current.pop("window_resume", None)
        current.pop("launch_in_progress", None)
        current["window_resumed_at"] = now()
        current["state"] = "launched"
        save_attempt(db, current)
        if node["state"] == "suspended" and not current.get("cancel_requested"):
            node.update(state="running", hold=None, revision=node["revision"] + 1)
            store.save_node(db, node)
            store.transition(db, "resumed", node["id"], {"run_id": run_id})
    store.mirror()


async def command(store, runner, attempt):
    id = attempt["attempt_id"]
    if attempt.get("window_stop"):
        with store.transaction(write=False) as db:
            current = attempts(db).get(id)
        if current and current["state"] == "suspended" and current.get("window_stop"):
            # Confirmation survived, but the supervisor may have died before
            # publishing idle and acknowledging the stop. Replay that effect
            # before considering any late result or releasing the intent.
            run = runner.tree.get(current["run_id"])
            if (run and current.get("turn_started_at") is not None
                    and run.turn_started_at != current["turn_started_at"]):
                return True
            if run:
                runner._settle_holds()
                runner.tree.set_status(run.id, "idle", "window suspended")
                runner._release(run.id)
            with store.transaction() as db:
                fresh = attempts(db).get(id)
                if fresh == current:
                    fresh.pop("window_stop", None)
                    fresh.pop("window_stop_started", None)
                    save_attempt(db, fresh)
            store.mirror()
            return True
        run = runner.tree.get(attempt["run_id"])
        # Read durable completion evidence before acquiring the journal lock;
        # intent order cannot decide whether this turn was interrupted.
        result = natural_result(store.paths, run) if run else None
        if run and (result or (not attempt.get("window_stop_started")
                              and run.status not in ACTIVE | PAUSED | {"quota_paused"})):
            predecessor = runner._steer_predecessor(run.id)
            if await runner._steer_predecessor_dead(predecessor):
                if result:
                    completed(store, run.id, result)
                else:
                    with store.transaction() as db:
                        current = attempts(db)[id]
                        current.pop("window_stop", None)
                        current.pop("window_stop_started", None)
                        save_attempt(db, current)
                return True
        with store.transaction() as db:
            current = attempts(db)[id]
            if (not current.get("window_stop") or current.get("capture_intent")
                    or current["state"] not in {"launched", "suspended"}):
                return True
            current["window_stop_started"] = True
            save_attempt(db, current)
        result = await runner.suspend(attempt["run_id"])
        stopped_run = runner.tree.get(attempt["run_id"])
        with store.transaction() as db:
            current = attempts(db)[id]
            node = store.nodes(db)[current["node_id"]]
            if not current.get("window_stop"):
                return True
            if result.get("completed"):
                current.pop("window_stop", None)
                current.pop("window_stop_started", None)
                save_attempt(db, current)
                return True
            if not result.get("predecessor_death_confirmed"):
                if node["state"] == "running":
                    node.update(state="held", hold={"reason": "termination_unconfirmed"},
                                revision=node["revision"] + 1)
                    current["window_hold_revision"] = node["revision"]
                    store.save_node(db, node)
                    store.transition(db, "held", node["id"], node["hold"])
                    save_attempt(db, current)
                return True
            record_suspension(store, db, current, stopped_run)
            current.pop("window_stop", None)
            current.pop("window_stop_started", None)
            save_attempt(db, current)
        store.mirror()
        return True
    if attempt.get("window_resume"):
        with store.transaction(write=False) as db:
            nodes = store.nodes(db)
            clock_file = store.meta(db, "clock_file")
        node = nodes[attempt["node_id"]]
        windows.prepare(runner.config.project["scheduler"].get("timezone", "Europe/Paris"), nodes)
        window = windows.effective(node, nodes, runner.config.project["scheduler"].get("timezone", "Europe/Paris"), instant(clock_file))
        run = runner.tree.get(attempt["run_id"])
        resume = attempt["window_resume"]
        launched = run and (run.turn_started_at or 0) > (resume["previous_turn"] or 0)
        if not launched:
            evidence = attempt.get("launch_evidence") or {}
            if evidence.get("pid") and evidence["pid"] != resume.get("previous_pid"):
                # A supervisor died after spawn but before publishing the new
                # turn. Stop that identity before retrying; never put another
                # invocation beside a wrapper whose death is unknown.
                if run:
                    runner.tree.update(run.id, pid=evidence["pid"], pid_start=evidence.get("pid_start", ""),
                                       exec_identity=evidence.get("executor", {}), status="pending")
                with store.transaction() as db:
                    current = attempts(db)[id]
                    current.pop("window_resume", None)
                    current.update(window_stop=True, window_stop_started=True)
                    current_node = store.nodes(db)[current["node_id"]]
                    if current_node["state"] == "suspended":
                        current_node.update(state="held", hold={"reason": "termination_unconfirmed"},
                                            revision=current_node["revision"] + 1)
                        current["window_hold_revision"] = current_node["revision"]
                        store.save_node(db, current_node)
                        store.transition(db, "held", current_node["id"], current_node["hold"])
                    save_attempt(db, current)
                return True
            if node["state"] != "suspended" or attempt.get("cancel_requested") or not window["open"]:
                with store.transaction() as db:
                    current = attempts(db)[id]
                    current.pop("window_resume", None)
                    current.pop("launch_in_progress", None)
                    current["state"] = "abandoned" if current.get("cancel_requested") else "suspended"
                    cancelled(store, db, current)
                    save_attempt(db, current)
                return True
            # The supervisor owns this attempt's flock. Ordinary Runner steer
            # supplies session verification, slot admission and death proof;
            # the scheduler's external steer gate stays closed to operators.
            result = await runner._steer(attempt["run_id"], resume["message"], window_resume=True)
            run = runner.tree.get(attempt["run_id"])
            launched = run and (run.turn_started_at or 0) > (resume["previous_turn"] or 0)
        else:
            result = {"steered": True}
        confirmed = True if launched else await runner._steer_predecessor_dead(
            runner._steer_predecessor(attempt["run_id"]))
        with store.transaction() as db:
            current = attempts(db)[id]
            if not current.get("window_resume"):
                return True
            node = store.nodes(db)[current["node_id"]]
            current.pop("window_resume", None)
            current.pop("launch_in_progress", None)
            if launched:
                current["state"] = "launched"
                if node["state"] == "suspended":
                    node.update(state="running", hold=None, revision=node["revision"] + 1)
                    store.save_node(db, node)
                    store.transition(db, "resumed", node["id"], {"run_id": current["run_id"]})
            elif confirmed:
                current.update(state="abandoned" if current.get("cancel_requested") else "suspended", resume_refusal=result)
                cancelled(store, db, current)
            else:
                current.update(state="launched", window_stop=True, window_stop_started=True, resume_refusal=result)
                if node["state"] == "suspended":
                    node.update(state="held", hold={"reason": "termination_unconfirmed"},
                                revision=node["revision"] + 1)
                    current["window_hold_revision"] = node["revision"]
                    store.save_node(db, node)
                    store.transition(db, "held", node["id"], node["hold"])
            save_attempt(db, current)
        store.mirror()
        return True
    return False
