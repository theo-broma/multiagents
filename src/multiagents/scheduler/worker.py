"""A detached Runner supervisor; scheduler death never ends its run (NC-R57)."""
from __future__ import annotations

import asyncio
import fcntl
import os
import sys
from pathlib import Path

from ..config import load
from ..paths import ProjectPaths
from ..runner import LaunchContext, Runner
from ..tree import now, ACTIVE, PAUSED
from .engine import attempts, record_launch, save_attempt
from . import sessions, suspension, windows
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
        if existing and not (attempt.get("window_stop") or attempt.get("window_resume") or attempt["state"] == "suspended"):
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
                    if sessions.unavailable(checked_binding, runner):
                        raise Refused("session_unavailable", detail=checked_binding["provider"])
                    sessions.check_clean(paths, checked_binding, runner.authority)
                except (Refused, gitops.GitError, OSError, ValueError) as exc:
                    preflight_failure = (exc.result if isinstance(exc, Refused)
                                         else {"error": "reseat_failed", "detail": str(exc)})
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
                    if detail["error"] != "session_unavailable":
                        node.update(state="held", hold={"reason": detail["error"], "detail": detail.get("detail", "")},
                                    revision=node["revision"] + 1)
                        store.save_node(db, node)
                        store.transition(db, detail["error"], node["id"], node["hold"])
                        # NT-R3: one notification per hold, keyed on the seq of
                        # its `held` transition (engine.hold writes the same
                        # pair), so a preflight hold is announced exactly once
                        # however often the node is edited or resumed.
                        store.transition(db, "held", node["id"], node["hold"])
                    attempt.update(state="abandoned", refusal=detail, ended_at=now())
                    save_attempt(db, attempt)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                    return
                attempt["launch_in_progress"] = True
                save_attempt(db, attempt)
            parent = node.get("run_parent", None if node["created_by"] == "root" else node["created_by"])
            creator = runner.tree.get(parent) if parent else None
            context = LaunchContext(caller=node.get("launch_caller", parent), run_parent=parent,
                                    depth=node.get("depth") or (creator.depth + 1 if creator else 1),
                                    node_id=node["id"], attempt_id=attempt_id, run_id=attempt["run_id"],
                                    provider=attempt.get("binding", {}).get("provider", node["pins"].get("provider", "")),
                                    effort=node["pins"].get("effort", ""))
            try:
                result = await runner.start(node["agent"], node["task"], launch_context=context,
                                            model=attempt.get("binding", {}).get("model", node["pins"].get("model")), **node.get("launch", {}))
            except BaseException:
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current.pop("launch_in_progress", None)
                    save_attempt(db, current)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                raise
            if not result.get("agent_id"):
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current.pop("launch_in_progress", None)
                    if current["state"] == "claimed":
                        current.update(state="abandoned", refusal=result.get("blocked"), ended_at=now())
                    save_attempt(db, current)
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                return
        run = runner.tree.get(attempt["run_id"])
        if run:
            with store.transaction() as db:
                current = attempts(db)[attempt_id]
                if not current.get("window_resume"):
                    current.pop("launch_in_progress", None)
                save_attempt(db, current)
                record_launch(store, db, attempt, run)
            store.mirror()
        while True:
            with store.transaction(write=False) as db:
                current = attempts(db)[attempt_id]
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
                if tree_run and tree_run.status in ACTIVE | PAUSED | {"quota_paused"}:
                    await asyncio.sleep(0.05)
                    continue
                with store.transaction() as db:
                    db.execute("UPDATE capabilities SET revoked=1 WHERE subject=?", (attempt["run_id"],))
                break
            await asyncio.sleep(0.05)
        await runner.shutdown(detach=True)


if __name__ == "__main__":
    asyncio.run(supervise(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None))
