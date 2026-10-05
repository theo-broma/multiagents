"""The scheduler as the monitor and `doctor` show it (NC-R43, NC-R93).

Read only. A live scheduler is asked over its socket, with the root
capability, for the two read ops (`scheduler_status`, `list_nodes`), so the
derived blocked reasons are the scheduler's own and nothing is recomputed
here. A dead one is described from its store, opened read-only, and never
created: a project whose scheduler never ran has no store to open.

No capability token is ever part of what this returns.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import procs
from . import sessions
from .store import Store, directory, root_capability

# NC-R93: held first, then running, open, suspended, done.
ORDER = ("held", "running", "open", "suspended", "done", "cancelled")
ALIAS_FIELDS = ("alias", "instance", "provider", "account", "model", "session_id")


def _process(project_root: Path) -> dict | None:
    try:
        info = json.loads((directory(project_root) / "process.json").read_text())
        pid, start = info["pid"], info["pid_start"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return info if procs.alive(pid, start) else None


def _ask(project_root: Path) -> tuple[dict, list[dict]] | None:
    from . import call
    try:
        token = root_capability(project_root)
        status = call(project_root, "scheduler_status", {}, token)
        listed = call(project_root, "list_nodes", {}, token)
    except (OSError, ValueError):
        return None
    if not status.get("ok") or not listed.get("ok"):
        return None
    return status["result"], listed["result"].get("nodes", [])


def _stored(project_root: Path) -> list[dict]:
    """The plan as last committed, for a scheduler that is not answering."""
    store = Store(project_root)
    if not store.file.is_file():
        return []
    try:
        db = sqlite3.connect(f"{store.file.as_uri()}?mode=ro", uri=True)
        try:
            records = [json.loads(raw) for (raw,) in db.execute("SELECT record FROM nodes")]
        finally:
            db.close()
    except (sqlite3.Error, ValueError):
        return []
    for node in records:
        node["blocked"] = [{"code": "held", "detail": node["hold"]}] if node.get("state") == "held" else []
    return records


def _row(node: dict, windows: dict, starving: set) -> dict:
    row = {"node_id": node["id"], "kind": node.get("kind"), "state": node.get("state"),
           "agent": node.get("agent"), "parent": node.get("parent"),
           "outcome": node.get("outcome"), "blocked": node.get("blocked") or [],
           "runs": [r.get("run_id") for r in node.get("runs") or [] if isinstance(r, dict)]}
    if node.get("hold"):
        row["hold"] = node["hold"]
    if node.get("locks"):
        row["wants_locks"] = node["locks"]
    if node.get("ready_since"):
        row["ready_since"] = node["ready_since"]
    if node.get("window"):
        row["window_declared"] = node["window"]
    # The effective window and its next open/close are the scheduler's (M5);
    # shown on the row as soon as `scheduler_status` carries them.
    if node["id"] in windows:
        row["window"] = windows[node["id"]]
    if node["id"] in starving:
        row["starving"] = True
    return row


def _aliases(nodes: list[dict], bound: list[dict], config) -> list[dict]:
    """Every session alias of the plan: its frozen binding once the first
    activation made one, else what its nodes declare (NC-R50), unbound."""
    by_id = {n["id"]: n for n in nodes}
    shown: dict[str, dict] = {}
    for binding in bound:
        shown[binding.get("id")] = {**{k: binding.get(k) for k in ALIAS_FIELDS}, "bound": True, "nodes": []}
    for node in nodes:
        key = sessions.alias_id(node, by_id)
        if not key:
            continue
        if key not in shown:
            spec = config.agents.get(node.get("agent")) if config else None
            pins = node.get("pins") or {}
            shown[key] = {"alias": node["session"],
                          "instance": sessions.instance_node(node, by_id)["template"]["instance"],
                          "provider": pins.get("provider") or (spec.provider if spec else None),
                          "account": None,
                          "model": pins.get("model") or (spec.model if spec else None),
                          "session_id": None, "bound": False, "nodes": []}
        shown[key]["nodes"].append(node["id"])
    return list(shown.values())


def _rank(node: dict) -> tuple:
    state = node.get("state")
    return (ORDER.index(state) if state in ORDER else len(ORDER), node.get("created_at") or 0)


def section(project_root: Path, config=None) -> dict:
    """The `scheduler` object of `/api/state` for a project with the gate on.

    `config` names the agents behind aliases not bound yet; without it they
    are listed with no provider."""
    process = _process(project_root)
    answered = _ask(project_root) if process else None
    if answered:
        status, nodes = answered
    else:
        status, nodes = {}, _stored(project_root)
    windows = {w["node_id"]: w for w in status.get("windows") or []
               if isinstance(w, dict) and w.get("node_id")}
    starving = list(status.get("starving") or [])
    counts = {state: 0 for state in ORDER}
    for node in nodes:
        counts[node.get("state")] = counts.get(node.get("state"), 0) + 1
    return {
        "running": answered is not None,
        "state": "running" if answered else "unavailable" if process else "not_running",
        "pid": status.get("pid") or (process or {}).get("pid"),
        "since": status.get("since") or (process or {}).get("since"),
        "last_tick": status.get("last_tick"),
        "counts": counts,
        "nodes": [_row(n, windows, set(starving)) for n in sorted(nodes, key=_rank)],
        "locks": [{k: lock.get(k) for k in ("name", "holder_run", "holder_node")}
                  for lock in status.get("locks") or []],
        "aliases": _aliases(nodes, status.get("aliases") or [], config),
        "windows": list(windows.values()),
        "starving": starving,
    }


def doctor_lines(sec: dict) -> tuple[list[str], list[str]]:
    """(information lines, problem lines) for `doctor`, from `section`."""
    info: list[str] = []
    problems: list[str] = []
    if sec["running"]:
        info.append(f"scheduler running (pid {sec['pid']}, last tick {sec['last_tick'] or 'none yet'})")
    else:
        problems.append("scheduler is not running while scheduler.enabled is on: nothing will "
                        "launch (start it with `multiagents scheduler start`)")
    counts = ", ".join(f"{n} {s}" for s, n in sec["counts"].items() if n)
    if counts:
        info.append(f"nodes: {counts}")
    starving = set(sec["starving"])
    for row in sec["nodes"]:
        reasons = "; ".join(_reason(b) for b in row["blocked"])
        if row["state"] == "held":
            hold = row.get("hold") or {}
            detail = hold.get("detail") if isinstance(hold, dict) else ""
            reason = hold.get("reason") if isinstance(hold, dict) else hold
            info.append(f"held {row['node_id']}: {reason}{f' ({detail})' if detail else ''}")
        elif row["node_id"] in starving:
            info.append(f"starving {row['node_id']}: ready since {row.get('ready_since')}"
                        f"{f', blocked by {reasons}' if reasons else ''}")
        elif reasons:
            info.append(f"{row['state']} {row['node_id']}: blocked by {reasons}")
        elif row["state"] in ("running", "open", "suspended"):
            info.append(f"{row['state']} {row['node_id']}")
        window = row.get("window") or {}
        upcoming = [f"{key.replace('_', ' ')} {window[key]}" for key in ("next_open", "next_close")
                    if window.get(key)]
        if upcoming:
            info.append(f"window {row['node_id']}: {'open' if window.get('open') else 'closed'}, "
                        + ", ".join(upcoming))
    for lock in sec["locks"]:
        info.append(f"lock {lock['name']} held by {lock['holder_node']} (run {lock['holder_run']})")
    for alias in sec["aliases"]:
        binding = (f"bound to {alias['provider']} {alias['model']} ({alias['account']})" if alias["bound"]
                   else f"not bound yet, declares {alias['provider']} {alias['model']}")
        info.append(f"session {alias['alias']} of {alias['instance']}: {binding}")
    return info, problems


def _reason(blocked) -> str:
    if not isinstance(blocked, dict):
        return str(blocked)
    detail = blocked.get("detail")
    if isinstance(detail, list):
        detail = ", ".join(map(str, detail))
    elif isinstance(detail, dict):
        detail = ", ".join(f"{v}" for v in detail.values() if v)
    return f"{blocked.get('code')}{f' {detail}' if detail else ''}"
