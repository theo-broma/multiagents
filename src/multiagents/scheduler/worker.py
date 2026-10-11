"""A detached Runner supervisor; scheduler death never ends its run (NC-R57)."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path

from ..config import load
from ..paths import ProjectPaths
from ..runner import LaunchContext, Runner
from ..tree import now, ACTIVE, PAUSED
from .engine import attempts, record_launch, save_attempt
from . import rate_limits, sessions, suspension, windows
from .model import Refused
from .. import gitops
from .store import Store


async def supervise(root, attempt_id, lock_fd=None):
    paths = ProjectPaths(root)
    store = Store(root)
    if lock_fd is not None:
        os.set_inheritable(lock_fd, False)
    with (os.fdopen(lock_fd, "a+") if lock_fd is not None else
          (store.directory / (attempt_id + ".lock")).open("a+")) as lock:
        if lock_fd is None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        with store.transaction(write=False) as db:
            attempt = attempts(db)[attempt_id]
            node = store.nodes(db)[attempt["node_id"]]
        from .owner import _bind_run
        _bind_run(attempt["run_id"])
        if attempt["state"] not in {"claimed", "launched", "suspended"} and not any("result" not in c for c in attempt.get("steer_commands", {}).values()):
            return
        configuration = load(paths, seed=False)
        runner = Runner(paths, configuration)
        window_timezone = configuration.project["scheduler"].get("timezone", "Europe/Paris")
        runner._scheduler_supervisor = True
        with store.transaction(write=False) as db:
            window_nodes = store.nodes(db)
        windows.prepare(window_timezone, window_nodes)
        existing = runner.tree.get(attempt["run_id"])
        if existing and not (attempt.get("window_stop") or attempt.get("window_resume") or attempt.get("rate_limit_resume") or attempt["state"] == "suspended"):
            # A supervisor died after writing launch evidence. Adoption reads
            # the recorded wrapper, session and run directory, never relaunches.
            await runner.adopt(exclude={n.id for n in runner.tree.active() if n.id != attempt["run_id"]})
        elif not existing:
            with store.transaction(write=False) as db:
                checked_attempt = attempts(db)[attempt_id]
                checked_nodes = store.nodes(db)
                checked_node = checked_nodes[checked_attempt["node_id"]]
                clock_file = store.meta(db, "clock_file")
                checked_binding = sessions.aliases(db).get(checked_attempt.get("alias_id"))
            checked_instant = suspension.instant(clock_file)
            preflight_failure = None
            if checked_binding:
                try:
                    route = checked_attempt.get("binding", checked_binding) if checked_attempt.get("cleanup") else checked_binding
                    if sessions.unavailable(route, runner):
                        raise Refused("session_unavailable", detail=checked_binding["provider"])
                    if checked_attempt.get("cleanup"):
                        previous = runner.tree.get(checked_attempt["cleanup_previous_run"])
                        if previous is None or previous.session_id != checked_attempt["session_id"]:
                            raise Refused("dirty_worktree", detail=checked_attempt["cleanup_archive"])
                        spec, provider = runner._spec_of(previous)
                        if not provider.spawn.get("resume") or runner._missing_session(previous, spec, provider, Path(checked_binding["worktree"])):
                            raise Refused("dirty_worktree", detail=checked_attempt["cleanup_archive"])
                    else:
                        sessions.check_clean(paths, checked_binding, runner.authority)
                except (Refused, gitops.GitError, OSError, ValueError, KeyError, RuntimeError) as exc:
                    preflight_failure = (exc.result if isinstance(exc, Refused)
                                         else {"error": "reseat_failed", "detail": str(exc)})
            cleanup = None
            cleanup_task = (sessions.cleanup_message(preflight_failure["detail"])
                            if preflight_failure and preflight_failure["error"] == "dirty_worktree"
                            and not checked_attempt.get("cleanup") else None)
            with store.transaction() as db:
                attempt = attempts(db)[attempt_id]
                node = store.nodes(db)[attempt["node_id"]]
                current_nodes = store.nodes(db)
                current_window = windows.effective(node, current_nodes, window_timezone,
                                                   checked_instant)
                if (not current_window["open"] or node["state"] != "open" or attempt["state"] != "claimed" or attempt.get("cancel_requested")
                        or node["revision"] != checked_node["revision"] or attempt != checked_attempt
                        or sessions.aliases(db).get(attempt.get("alias_id")) != checked_binding):
                    if attempt["state"] == "claimed":
                        attempt.update(state="abandoned", ended_at=now())
                        save_attempt(db, attempt)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                    return
                if preflight_failure:
                    detail = preflight_failure
                    if attempt.get("cleanup"):
                        detail = {"error": "dirty_worktree", "detail": attempt["cleanup_archive"]}
                    elif detail["error"] == "dirty_worktree":
                        cleanup = sessions.request_cleanup(store, db, node, checked_binding, runner,
                                                           detail["detail"], cleanup_task, replacing=attempt_id)
                    if not cleanup and detail["error"] != "session_unavailable":
                        node.update(state="held", hold={"reason": detail["error"], "detail": detail.get("detail", "")},
                                    revision=node["revision"] + 1)
                        store.save_node(db, node)
                        store.transition(db, detail["error"], node["id"], node["hold"])
                        # NT-R3: one notification per hold, keyed on the seq of
                        # its `held` transition (engine.hold writes the same
                        # pair), so a preflight hold is announced exactly once
                        # however often the node is edited or resumed.
                        store.transition(db, "held", node["id"], node["hold"])
                    if not cleanup:
                        attempt.update(state="abandoned", refusal=detail, ended_at=now())
                        save_attempt(db, attempt)
                        db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                        return
                else:
                    attempt["launch_in_progress"] = True
                    save_attempt(db, attempt)
            if cleanup:
                store.mirror()
                await supervise(root, cleanup["attempt_id"])
                return
            parent = node.get("run_parent", None if node["created_by"] == "root" else node["created_by"])
            creator = runner.tree.get(parent) if parent else None
            context = LaunchContext(caller=node.get("launch_caller", parent), run_parent=parent,
                                    depth=node.get("depth") or (creator.depth + 1 if creator else 1),
                                    node_id=node["id"], attempt_id=attempt_id, run_id=attempt["run_id"],
                                    provider=attempt.get("binding", {}).get("provider", node["pins"].get("provider", "")),
                                    effort=attempt.get("cleanup_effort", node["pins"].get("effort", "")))
            try:
                task = attempt.get("cleanup_task", node["task"])
                if node.get("rate_limit_fresh_from"):
                    old = node["rate_limit_fresh_from"]
                    task += (f"\n\nThe rate-limited session could not be resumed. Read the prior run "
                             f"{old['run_id']} in {old['run_dir']} before continuing the task.")
                result = await runner.start(attempt.get("cleanup_agent", node["agent"]), task, launch_context=context,
                                            model=attempt.get("binding", {}).get("model", node["pins"].get("model")), **node.get("launch", {}))
            except BaseException:
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current.pop("launch_in_progress", None)
                    save_attempt(db, current)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                raise
            if not result.get("agent_id") and not result.get("error_kind"):
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current.pop("launch_in_progress", None)
                    if current["state"] == "claimed":
                        current.update(state="abandoned", refusal=result.get("blocked"), ended_at=now())
                    save_attempt(db, current)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                return
            if not result.get("agent_id"):
                # TF-R4: an exception out of the launch itself — recorded as a
                # classified `launch` cause, bounded by TF-R2's budget.
                from .. import failures as tf
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current.pop("launch_in_progress", None)
                    if current["state"] == "claimed":
                        kind = result.get("error_kind") or ""
                        detail = (result.get("error") or result.get("reason")
                                  or json.dumps(result.get("blocked") or result)[:2000])
                        if kind and not detail.startswith(kind):
                            detail = f"{kind}: {detail}"
                        node = store.nodes(db)[current["node_id"]]
                        provider_name = node["pins"].get("provider", "")
                        spec = runner.config.agents.get(node["agent"])
                        if not provider_name and spec is not None:
                            provider_name = spec.provider
                        provider = runner.providers.get(provider_name)
                        cause = tf.classify(
                            status="failed", launched=False, error_text=detail,
                            provider=provider_name,
                            account=(provider.container_account or provider_name) if provider else "",
                            model=node["pins"].get("model") or (spec.model if spec else None) or "")
                        # TF-R4: a launch that raised is still an attempt of
                        # this activation — recorded with its classified cause
                        # so TF-R2's budget bounds it, never abandoned silently.
                        tree_run = runner.tree.get(current["run_id"])
                        if tree_run is not None:
                            record_launch(store, db, attempt, tree_run)
                        # TF-R2/R4: Runner writes its tree reservation and
                        # run directory before spawning. A returned setup
                        # failure with neither proves no process was started;
                        # the cause class alone cannot prove that.
                        current["never_spawned"] = (tree_run is None
                                                    and not paths.run_dir(current["run_id"]).exists())
                        current.update(state="captured", ended_at=now(),
                                       refusal=result.get("blocked"),
                                       result={"status": "failed", "session_id": "",
                                               "run_dir": str(paths.run_dir(current["run_id"])),
                                               "cause": cause})
                        save_attempt(db, current)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                store.mirror()
                return
        run = runner.tree.get(attempt["run_id"])
        if run:
            with store.transaction() as db:
                current = attempts(db)[attempt_id]
                if not current.get("window_resume") and not current.get("rate_limit_resume"):
                    current.pop("launch_in_progress", None)
                save_attempt(db, current)
                record_launch(store, db, attempt, run)
            store.mirror()
        while True:
            with store.transaction(write=False) as db:
                current = attempts(db)[attempt_id]
            if await rate_limits.command(store, runner, current):
                with store.transaction(write=False) as db:
                    settled = attempts(db)[attempt_id]
                if settled["state"] in {"suspended", "abandoned", "recorded"}:
                    break
                await asyncio.sleep(0.05)
                continue
            if await suspension.command(store, runner, current):
                with store.transaction(write=False) as db:
                    settled = attempts(db)[attempt_id]
                if settled["state"] in {"suspended", "abandoned", "recorded"}:
                    break
                await asyncio.sleep(0.05)
                continue
            with store.transaction(write=False) as db:
                owner = store.nodes(db)[current["node_id"]]
            if current["state"] == "suspended" and owner["state"] == "suspended":
                break
            if current.get("cancel_requested") and not current.get("cancel_confirmed"):
                result = await runner.stop(attempt["run_id"])
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current["cancel_confirmed"] = result.get("predecessor_death_confirmed", False)
                    current.pop("launch_in_progress", None)
                    if current["cancel_confirmed"] and (current.get("window_resumed_at") or current.get("window_resume")):
                        suspension.cancelled(store, db, current)
                    save_attempt(db, current)
            command = next(((id, c) for id, c in current.get("steer_commands", {}).items()
                            if "result" not in c), None)
            if command:
                id, value = command
                result = await runner.steer(attempt["run_id"], value["message"])
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current["steer_commands"][id]["result"] = result
                    save_attempt(db, current)
                continue
            live = runner.runs.get(attempt["run_id"])
            if live is None or live.done.is_set():
                tree_run = runner.tree.get(attempt["run_id"])
                if tree_run and tree_run.status in (ACTIVE if current.get("cleanup") else ACTIVE | PAUSED | {"quota_paused"}):
                    await asyncio.sleep(0.05)
                    continue
                if current.get("cleanup") and tree_run:
                    if not await runner._steer_predecessor_dead(runner._steer_predecessor(tree_run.id)):
                        await asyncio.sleep(0.05)
                        continue
                    sessions.finish_cleanup(store, runner, current, tree_run)
                if tree_run is not None and tree_run.status not in (ACTIVE | PAUSED | {"quota_paused"}):
                    # TF-R1: the classified cause is public from the moment
                    # the run is terminal — capture and settlement follow on
                    # the scheduler's own tick, and must not delay the cause.
                    try:
                        from ..runner import _run_read
                        recorded = json.loads(_run_read(paths.run_dir(attempt["run_id"]),
                                                         "result.json"))
                    except (OSError, ValueError):
                        recorded = {}
                    failure_cause = (recorded.get("failure_cause")
                                     if isinstance(recorded, dict) else None)
                    if isinstance(failure_cause, dict) and failure_cause.get("class"):
                        with store.transaction() as db:
                            node = store.nodes(db)[current["node_id"]]
                            entry = next((r for r in node["runs"]
                                          if r["attempt_id"] == attempt_id), None)
                            if entry is not None and not entry.get("cause"):
                                entry["cause"] = failure_cause
                                store.save_node(db, node)
                with store.transaction() as db:
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                break
            await asyncio.sleep(0.05)
        await runner.shutdown(detach=True)


if __name__ == "__main__":
    from .owner import _watch
    with _watch(run=True):
        asyncio.run(supervise(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None))
