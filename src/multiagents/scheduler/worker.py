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
        if attempt["state"] not in {"claimed", "launched"} and not any("result" not in c for c in attempt.get("steer_commands", {}).values()):
            return
        runner = Runner(paths, load(paths, seed=False))
        runner._scheduler_supervisor = True
        existing = runner.tree.get(attempt["run_id"])
        if existing:
            # A supervisor died after writing launch evidence. Adoption reads
            # the recorded wrapper, session and run directory, never relaunches.
            await runner.adopt(exclude={n.id for n in runner.tree.active() if n.id != attempt["run_id"]})
        else:
            with store.transaction() as db:
                attempt = attempts(db)[attempt_id]
                node = store.nodes(db)[attempt["node_id"]]
                if node["state"] != "open" or attempt["state"] != "claimed" or attempt.get("cancel_requested"):
                    if attempt["state"] == "claimed":
                        attempt.update(state="abandoned", ended_at=now())
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
                                    provider=node["pins"].get("provider", ""), effort=node["pins"].get("effort", ""))
            try:
                result = await runner.start(node["agent"], node["task"], launch_context=context,
                                            model=node["pins"].get("model"), **node.get("launch", {}))
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
                current.pop("launch_in_progress", None)
                save_attempt(db, current)
                record_launch(store, db, attempt, run)
            store.mirror()
        while True:
            with store.transaction(write=False) as db:
                current = attempts(db)[attempt_id]
            if current.get("cancel_requested") and not current.get("cancel_confirmed"):
                result = await runner.stop(attempt["run_id"])
                with store.transaction() as db:
                    current = attempts(db)[attempt_id]
                    current["cancel_confirmed"] = result.get("predecessor_death_confirmed", False)
                    current.pop("launch_in_progress", None)
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
