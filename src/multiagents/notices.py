"""D1: limit notices — telling the user when a configured limit constrained a run.

A limit that refuses, defers, trips or kills something used to do it with a
bare error text, or silently: nothing said which key it was, what it was set
to, or where to change it. A `limit_hit` event now says all three (LN-C1), and
a `limit_cleared` one says when it stopped (LN-C4).

Three things live here:

* **Provenance** (LN-C2): which config layer, file and line a covered value
  came from. Found by composing the yaml rather than loading it, because only
  the node tree keeps line numbers.
* **Shared notice state** (LN-C4): which notices are active, how often each
  was hit, and each `wait_for_agents` caller's cursor into the event log. In
  `tree.json`, under the tree's own lock — every agent's server is its own
  process and the monitor another, so a notice deduplicated in one process's
  memory would be announced once per process.
* **The terminal line** (LN-C3): the one `multiagents watch` prints, and a
  small tailer that `multiagents run` leaves beside the CLI it hands the
  terminal to.
"""

from __future__ import annotations

import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .paths import global_config_dir, shipped_defaults_dir
from .tree import Tree, now

# The effects of LN-C1.
EFFECTS = ("refused", "deferred", "stuck", "killed", "kill_uncertain", "stopped")
STATE_KEY = "limit_notices"
# How many cleared notices stay visible to the monitor after they stop.
KEEP_CLEARED = 20


# --------------------------------------------------------------------------
# provenance (LN-C2)


@lru_cache(maxsize=64)
def _composed(path: str, mtime_ns: int, size: int) -> yaml.Node | None:
    try:
        with open(path) as handle:
            return yaml.compose(handle)
    except (OSError, yaml.YAMLError):
        return None


def _node_at(path: Path, parts: list[str]) -> tuple[int, Any] | None:
    """`(1-based line of the key, its scalar value)` for a dotted key in one
    yaml file, or None when that file does not set it."""
    try:
        stat = path.stat()
    except OSError:
        return None
    node = _composed(str(path), stat.st_mtime_ns, stat.st_size)
    line = None
    for part in parts:
        if not isinstance(node, yaml.MappingNode):
            return None
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode) and key.value == part:
                line, node = key.start_mark.line + 1, value
                break
        else:
            return None
    if line is None:
        return None
    raw = None
    if isinstance(node, yaml.ScalarNode):
        raw = node.value if node.style else yaml.safe_load(node.value)
    return line, raw


def _same(raw: Any, value: Any) -> bool:
    """Does a layer's raw value account for the effective one? A malformed
    entry (`5m`, a negative) falls back to the next source, so the line that
    holds it is not where the value came from."""
    if value is None:
        return True
    if isinstance(raw, bool) or isinstance(value, bool):
        return raw == value
    try:
        return float(raw) == float(value)
    except (TypeError, ValueError):
        return str(raw) == str(value)


def _project_layer(config) -> Path | None:
    fixed = {shipped_defaults_dir().resolve(), global_config_dir().resolve()}
    for layer in reversed(getattr(config, "layers", None) or []):
        if layer.resolve() not in fixed:
            return layer
    return None


def provenance(config, key: str, value: Any = None,
               layer: str | None = None) -> dict[str, Any]:
    """Where a covered value came from, as LN-C2's `source`.

    `key` is dotted: `limits.max_concurrent`, `executor.docker.memory`, or
    `agents.<name>.<field>` for a per-agent value. `value` is the effective
    value, used to skip a layer whose entry was not usable. `layer` is the
    source H14 already resolved (`agent`, `project` or `builtin`), which
    narrows the search to that kind of layer.
    """
    parts = key.split(".")
    per_agent = parts[0] == "agents"
    filename = "agents.yaml" if per_agent else "project.yaml"
    shipped = shipped_defaults_dir().resolve()
    project_dir = _project_layer(config)
    layers = [Path(p) for p in (getattr(config, "layers", None) or [])]
    override_key = key
    override_file = str(((project_dir or Path(".")) / filename).resolve()) \
        if project_dir else None

    found = None
    for directory in reversed(layers):
        is_shipped = directory.resolve() == shipped
        if layer == "builtin" and not is_shipped:
            continue
        if layer in ("project", "agent") and is_shipped and not per_agent:
            continue
        hit = _node_at(directory / filename, parts)
        if hit is not None and _same(hit[1], value):
            found = (directory, is_shipped, hit[0])
            break

    if found is None and not layers and layer != "builtin":
        # A Config built in code: its values came from no file.
        return {"layer": layer or ("agent" if per_agent else "project"),
                "file": None, "line": None}
    if found is not None and per_agent:
        directory, _, line = found
        return {"layer": "agent", "file": str((directory / filename).resolve()),
                "line": line}
    if found is not None and not found[1]:
        directory, _, line = found
        return {"layer": "project", "file": str((directory / filename).resolve()),
                "line": line}
    if found is None and layer != "builtin":
        # Not in any layer we can read at the value in force, so shipped.
        hit = _node_at(shipped / filename, parts)
        found = (shipped, True, hit[0]) if hit and _same(hit[1], value) else None
    return {"layer": "builtin",
            "file": str((shipped / filename).resolve()) if found else None,
            "line": found[2] if found else None,
            "override_file": override_file,
            "override_key": override_key}


def call_source(argument: str, override_key: str | None = None,
                **detail: Any) -> dict[str, Any]:
    """LN-C2's `call` source: the value was an argument to a tool call."""
    source: dict[str, Any] = {"layer": "call", "tool": "start_agent",
                              "argument": argument}
    if override_key:
        source["override_key"] = override_key
    source.update({k: v for k, v in detail.items() if v not in (None, "")})
    return source


def where(source: dict[str, Any] | None) -> str:
    """The place to change a value, for the notice's one line."""
    if not source:
        return "no configured limit"
    layer = source.get("layer")
    if layer == "call":
        text = f"set by start_agent({source.get('argument')}=…)"
        if source.get("node"):
            text += f" from {source['node']}"
        if source.get("override_key"):
            text += f"; {source['override_key']} in config is the lasting setting"
        return text
    at = (f"{source['file']}:{source['line']}"
          if source.get("file") and source.get("line") else "")
    if layer == "builtin":
        text = "built-in default" + (f", {at}" if at else "")
        if source.get("override_file"):
            text += (f"; set {source.get('override_key')} in "
                     f"{source['override_file']} to change it")
        return text
    return at or f"{layer} config"


def message(key: str, value: Any, source: dict[str, Any] | None,
            what: str, advice: str = "") -> str:
    """LN-C3: one human line naming what was limited, the key, the value and
    the place to change it."""
    line = f"limit: {key} = {value} ({where(source)}) — {what}"
    return f"{line}; {advice}." if advice else f"{line}."


# --------------------------------------------------------------------------
# shared state (LN-C4)


def _ident(key: str, scope: str) -> str:
    return f"{key}|{scope}"


def _state(data: dict) -> dict:
    state = data.setdefault(STATE_KEY, {})
    state.setdefault("active", {})
    state.setdefault("cleared", [])
    state.setdefault("cursors", {})
    return state


def hit(tree: Tree, *, key: str, value: Any, effect: str, scope: str,
        source: dict[str, Any] | None, message: str,
        node: str | None = None) -> dict[str, Any]:
    """Record one constraint. The first under `(key, scope)` emits
    `limit_hit`; later ones while it is active only count. Returns the active
    notice, with its count."""
    with tree.transaction() as data:
        active = _state(data)["active"]
        ident = _ident(key, scope)
        entry = active.get(ident)
        if entry is not None:
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["last_hit"] = now()
            return dict(entry)
        entry = {"key": key, "value": value, "effect": effect, "scope": scope,
                 "node": node, "source": source, "message": message,
                 "count": 1, "first_hit": now(), "last_hit": now()}
        active[ident] = entry
        # Under the lock, so two processes hitting it at once agree on which
        # of them announced it.
        fields = {"key": key, "value": value, "effect": effect, "scope": scope,
                  "source": source, "message": message}
        if node:
            fields["node"] = node
        tree.emit(node or "-", "limit_hit", **fields)
        return dict(entry)


def clear(tree: Tree, match) -> list[dict[str, Any]]:
    """End every active notice `match(entry)` accepts, each with one
    `limit_cleared` carrying its count."""
    if not (tree.read().get(STATE_KEY) or {}).get("active"):
        return []                     # the common case: one read, no write
    ended: list[dict[str, Any]] = []
    with tree.transaction() as data:
        state = _state(data)
        for ident, entry in list(state["active"].items()):
            if not match(entry):
                continue
            del state["active"][ident]
            entry = {**entry, "cleared_at": now()}
            ended.append(entry)
            state["cleared"] = [*state["cleared"], entry][-KEEP_CLEARED:]
            tree.emit(entry.get("node") or "-", "limit_cleared",
                      key=entry["key"], scope=entry["scope"],
                      count=entry.get("count", 1),
                      message=f"limit cleared: {entry['key']} "
                              f"({entry['scope']}) after {entry.get('count', 1)} hit(s)")
    return ended


def clear_node(tree: Tree, node_id: str, effect: str | None = None) -> None:
    """The node ended (or moved past a trip): its own notices stop."""
    clear(tree, lambda e: e.get("scope") == node_id
          and (effect is None or e.get("effect") == effect))


def active(data: dict) -> list[dict[str, Any]]:
    """Active notices from an already-read tree, oldest first."""
    entries = ((data.get(STATE_KEY) or {}).get("active") or {}).values()
    return sorted(entries, key=lambda e: e.get("first_hit") or 0)


def recently_cleared(data: dict) -> list[dict[str, Any]]:
    return list((data.get(STATE_KEY) or {}).get("cleared") or [])


def _brief(event: dict[str, Any]) -> dict[str, Any]:
    return {k: event[k] for k in ("kind", "key", "value", "effect", "scope",
                                  "node", "message", "count")
            if event.get(k) is not None}


def since(tree: Tree, caller: str) -> list[dict[str, Any]]:
    """LN-C3/C4: the notices a `wait_for_agents` caller has not been shown.

    One cursor per caller over `events.jsonl`, so two callers each see a
    notice once. A caller's first call has no cursor and gets the notices that
    are active now instead of the log's whole history.
    """
    path = tree.events_path
    out: list[dict[str, Any]] = []
    with tree.transaction() as data:
        state = _state(data)
        cursor = state["cursors"].get(caller)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if cursor is None or cursor > size:
            out = [{"kind": "limit_hit", **_brief(entry)} for entry in active(data)]
            state["cursors"][caller] = size
            return out
        try:
            with path.open("rb") as handle:
                handle.seek(cursor)
                chunk = handle.read(size - cursor)
        except OSError:
            return []
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") in ("limit_hit", "limit_cleared"):
                out.append(_brief(event))
        state["cursors"][caller] = cursor + end
    return out


# --------------------------------------------------------------------------
# the terminal (LN-C3)


def event_line(event: dict[str, Any]) -> str:
    """One event as `multiagents watch` prints it."""
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", 0)))
    extra = " ".join(
        f"{k}={v}" for k, v in event.items()
        if k not in {"t", "agent", "kind"} and v not in (None, "", [])
    )
    return f"{stamp} {event.get('agent', '-'):10} {event.get('kind', ''):10} {extra}"


def _alive(pid: int) -> bool:
    """Still running. A zombie is not: `run`'s own caller may not reap the CLI
    until everything holding its stdout — this tailer included — lets go."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True                   # not Linux: kill(0) is all there is
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def tail(events: Path, pid: int, offset: int, poll: float = 0.5) -> int:
    """Print each limit notice appended after `offset`, until `pid` is gone.

    `run` hands the terminal to the provider's CLI by exec, after which
    nothing of ours is left in that process to print with; this runs beside
    it, on the same terminal, and ends with it.
    """
    position = offset
    while True:
        running = _alive(pid)
        try:
            with events.open("rb") as handle:
                handle.seek(position)
                chunk = handle.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") in ("limit_hit", "limit_cleared"):
                print(event_line(event), flush=True)
        position += end
        if not running:
            return 0
        time.sleep(poll)


def start_tailer(events: Path, pid: int) -> None:
    """Leave a `tail` beside `pid`, sharing this process's stdout."""
    import subprocess

    try:
        offset = events.stat().st_size
    except OSError:
        offset = 0
    try:
        subprocess.Popen(
            [sys.executable, "-m", "multiagents.notices", str(events), str(pid),
             str(offset)],
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass                        # observation must never block a launch


if __name__ == "__main__":
    try:
        sys.exit(tail(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
    except KeyboardInterrupt:
        sys.exit(0)
