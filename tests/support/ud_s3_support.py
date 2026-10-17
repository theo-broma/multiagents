"""Shared helpers for the user-decisions slice 3 tests (UD-C11..UD-C13).

The watcher is driven as a real `multiagents bg watch` subprocess over a real
project directory (the DJ harness), with decisions created and answered through
the merged slice-1 module API. Nothing here depends on slice 2's tools.

Time: the watcher's clock is the wall clock (no MULTIAGENTS_BG_NOW is set), so
a delivery seeded `age` seconds ago is `age` seconds old when the watcher runs.
Ages are chosen well clear of the 900 s boundary (880 / 920), because a real
process start takes a moment.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from multiagents.procs import start_time
from multiagents.tree import Tree

from support.ud_support import INIT, ORCH, USER, D, create

from test_dj_support import CLI_BOUND, ROOT

WINDOW = 900
INSIDE = 880
OUTSIDE = 920
SESSION = "ud-session-a"
OTHER = "ud-session-b"


def tree_of(jobs) -> Tree:
    return Tree(jobs.paths.tree_file, jobs.paths.events_file)


def registry(jobs) -> Path:
    root = Path(jobs.env["MULTIAGENTS_STATE_DIR"]) / "bg" / jobs.paths.slug
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def bind(jobs, pid, session=SESSION, role="orchestrator"):
    folder = registry(jobs) / "bindings"
    folder.mkdir(mode=0o700, exist_ok=True)
    (folder / f"{session}.json").write_text(json.dumps(dict(
        version=1, role=role, session_id=session, pid=pid, pid_start=start_time(pid))))
    return session


def watch_argv(jobs, owner="orchestrator", timeout=1):
    return [sys.executable, "-m", "multiagents.cli", "--path", str(jobs.paths.root),
            "bg", "watch", "--owner", owner, "--timeout", str(timeout)]


def watch(jobs, session=SESSION, owner="orchestrator", timeout=1):
    return jobs.cli("watch", "--owner", owner, "--timeout", timeout,
                    input=json.dumps({"session_id": session}))


def spawn_watch(jobs, session=SESSION, owner="orchestrator"):
    """A watcher started without waiting for it (for concurrency tests)."""
    proc = subprocess.Popen(watch_argv(jobs, owner), cwd=jobs.paths.root, env=jobs.env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    proc.stdin.write(json.dumps({"session_id": session}))
    proc.stdin.close()
    return proc


def answered_decision(tree, *, by=ORCH, mode="single", comment="", free_text="",
                      options=("yes",), **over):
    """A decision asked by `by` and answered by the user, through slice 1."""
    rec = create(tree, mode, by=by, **over)
    if mode == "free_text":
        return D.answer(tree, rec["id"], revision=rec["revision"], by=USER,
                        free_text=free_text or "ship it", comment=comment)["decision"]
    return D.answer(tree, rec["id"], revision=rec["revision"], by=USER,
                    options=list(options), comment=comment, free_text=free_text)["decision"]


def initializer_decision(tree, **over):
    return answered_decision(tree, by=INIT, **over)


def seed_delivery(tree, id, *, age, session):
    """Write a delivery record `age` seconds old straight into the store, as an
    earlier watcher would have left it (an on-disk fixture, like the DJ ones).
    The revision moves with it, as UD-C11 requires."""
    with tree.transaction() as data:
        for rec in data["questions"]:
            if rec["id"] == id:
                rec["delivery"] = {"delivered_at": time.time() - age, "session": session}
                rec["revision"] += 1
                return rec
    raise AssertionError(id)


def stored(tree, id):
    return D.get(tree, id)


def stable_part(rec):
    """Everything about a decision that delivery must leave alone."""
    return {k: v for k, v in rec.items() if k not in ("revision", "delivery")}


def delivered_events(tree, id=None):
    if not tree.events_path.exists():
        return []
    out = [json.loads(line) for line in tree.events_path.read_text().splitlines() if line.strip()]
    return [e for e in out if e.get("kind") == "decision" and e.get("transition") == "delivered"
            and (id is None or e["id"] == id)]
