"""Project-wide run identities, reserved before their first record is written."""
from __future__ import annotations

from contextlib import closing
import os
import sqlite3
import uuid

from .paths import ProjectPaths, state_root


def _scheduler_ids(db) -> set[str]:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    known = set()
    if "attempts" in tables:
        known.update(row[0] for row in db.execute("SELECT json_extract(record, '$.run_id') FROM attempts"))
    if "consults" in tables:
        known.update(row[0] for row in db.execute("SELECT run_id FROM consults"))
    return known


def allocate(paths: ProjectPaths, db=None) -> str:
    """Draw an unused id and permanently reserve it (RP14, F207).

    Atomic mkdir arbitrates threads and processes without holding a lock
    across tree or scheduler reads. Reservations live outside run listings
    and survive abandoned admissions and restarts. A caller already inside
    a scheduler transaction passes its connection, including unsaved claims.
    """
    from .tree import Tree

    nodes = Tree(paths.tree_file, paths.events_file).read().get("nodes", {})
    known = set(nodes)
    known.update(node.get("id") for node in nodes.values())
    if db is not None:
        known.update(_scheduler_ids(db))
    else:
        database = paths.scheduler / "plan.sqlite3"
        if database.exists():
            with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30)) as connection:
                known.update(_scheduler_ids(connection))

    # NC-R48: legacy runs reserve identities without creating scheduler state.
    reservations = state_root() / "run-ids" / paths.slug
    reservations.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        run_id = "ag-" + uuid.uuid4().hex[:6]
        if run_id in known or os.path.lexists(paths.run_dir(run_id)):
            continue
        try:
            (reservations / run_id).mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id
