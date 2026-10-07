"""Journaled, node-local rate-limit resumes (TB-R4)."""
from __future__ import annotations

from ..tree import now
from .engine import attempts, save_attempt
from . import sessions, suspension


def interrupted(engine, db, current, node, result):
    """Record the completed turn without capturing or settling its work."""
    store = engine.store
    count = node.get("rate_limit_attempts", 0) + 1
    node.update(rate_limit_attempts=count, outcome=None, revision=node["revision"] + 1)
    entry = next(r for r in node["runs"] if r["attempt_id"] == current["attempt_id"])
    entry["cause"] = "rate_limited"
    current.update(state="suspended", rate_limit_pending=True, rate_limit_result=result)
    current.pop("rate_limit_recovery", None)
    if current.get("alias_id"):
        binding = sessions.aliases(db)[current["alias_id"]]
        binding.update(session_id=result.get("session_id", ""), last_run=current["run_id"])
        sessions.save_alias(db, binding)
    store.transition(db, "rate_limited", node["id"],
                     {"run_id": current["run_id"], "cause": "rate_limited", "attempt": count})
    if current.get("cancel_requested") or node["state"] in {"cancelled", "held", "done"}:
        current.update(state="abandoned", cancel_confirmed=True)
        node.pop("pending_resume", None)
    elif count >= 6:
        current["state"] = "recorded"
        node.pop("pending_resume", None)
        engine.hold(db, node, "rate_limited", current["run_id"])
    else:
        cooldown = float(engine.runner.config.limits.get("rate_limit_cooldown_seconds", 60))
        delay = result.get("retry_after")
        if delay is None:
            delay = max(0.0, cooldown)
        delay = (min(900.0, delay * 2 ** (count - 1)) if result.get("retry_after") is None
                 else max(delay, min(900.0, delay * 2 ** (count - 1))))
        node["pending_resume"] = {"at": now() + delay, "attempt": count}
    store.save_node(db, node)
    save_attempt(db, current)


def fresh(store, db, current, node, paths):
    """A lost provider session continues fresh, retaining the prior artifacts."""
    current.update(state="recorded")
    for key in ("rate_limit_pending", "rate_limit_resume", "launch_in_progress", "resume_pending"):
        current.pop(key, None)
    node.pop("pending_resume", None)
    if not current.get("cancel_requested") and node["state"] not in {"held", "done", "cancelled"}:
        node.update(state="open", outcome=None, revision=node["revision"] + 1,
                    rate_limit_fresh_from={"run_id": current["run_id"],
                                          "run_dir": str(paths.run_dir(current["run_id"]))})
        store.transition(db, "rate_limit_relaunch", node["id"], node["rate_limit_fresh_from"])
    store.save_node(db, node)
    save_attempt(db, current)


def resumed(store, run_id):
    """Acknowledge the new turn before the supervisor can consume its result."""
    with store.transaction() as db:
        current = next((a for a in attempts(db).values() if a["run_id"] == run_id), None)
        if not current or not current.get("rate_limit_resume"):
            return
        current.pop("rate_limit_resume", None)
        current.pop("rate_limit_pending", None)
        current.pop("launch_in_progress", None)
        current.update(state="launched", rate_limit_recovery=True)
        node = store.nodes(db)[current["node_id"]]
        node.pop("pending_resume", None)
        store.save_node(db, node)
        save_attempt(db, current)
        store.transition(db, "resumed", node["id"], {"run_id": run_id, "cause": "rate_limited"})
    store.mirror()


async def command(store, runner, attempt):
    """Replay a resume under the same supervisor flock as window commands."""
    resume = attempt.get("rate_limit_resume")
    if not resume:
        return False
    with store.transaction(write=False) as db:
        node = store.nodes(db)[attempt["node_id"]]
    run = runner.tree.get(attempt["run_id"])
    launched = run and (run.turn_started_at or 0) > (resume["previous_turn"] or 0)
    if launched:
        resumed(store, attempt["run_id"])
        return True
    evidence = attempt.get("launch_evidence") or {}
    if evidence.get("pid") and evidence["pid"] != resume.get("previous_pid"):
        # A wrapper may have spawned before its supervisor died. Recover and
        # confirm that identity first; no new invocation runs beside it.
        if run:
            runner.tree.update(run.id, pid=evidence["pid"], pid_start=evidence.get("pid_start", ""),
                               exec_identity=evidence.get("executor", {}), status="pending")
        stopped = await runner.stop(attempt["run_id"], internal=True)
        if not stopped.get("predecessor_death_confirmed"):
            return True
    allowed = (node["state"] == "running" and not attempt.get("cancel_requested")
               and node.get("pending_resume", {}).get("at", float("inf")) <= now())
    if allowed:
        # Expected refusals and session loss are returned by _steer. An
        # unexpected exception must leave the resume journal intact and
        # reach the supervisor's error reporting (review ag-e2ca36).
        result = await runner._steer(attempt["run_id"], resume["message"], rate_limit_resume=True)
        run = runner.tree.get(attempt["run_id"])
        if run and (run.turn_started_at or 0) > (resume["previous_turn"] or 0):
            resumed(store, attempt["run_id"])
            return True
    else:
        result = {"error": "resume cancelled or held"}
    confirmed = await runner._steer_predecessor_dead(runner._steer_predecessor(attempt["run_id"]))
    with store.transaction() as db:
        current = attempts(db)[attempt["attempt_id"]]
        if not current.get("rate_limit_resume"):
            return True
        current.pop("rate_limit_resume", None)
        current.pop("launch_in_progress", None)
        current.pop("resume_pending", None)
        current["resume_refusal"] = result
        node = store.nodes(db)[current["node_id"]]
        if confirmed:
            if result.get("error") in {"session_unavailable", "no_session"} or not (run and run.session_id):
                fresh(store, db, current, node, store.paths)
            else:
                current["state"] = "abandoned" if current.get("cancel_requested") else "suspended"
                suspension.cancelled(store, db, current)
                save_attempt(db, current)
        else:
            # Keep ownership until the worker can prove the wrapper dead.
            current.update(rate_limit_resume=resume, launch_in_progress=True)
            save_attempt(db, current)
    store.mirror()
    return True
