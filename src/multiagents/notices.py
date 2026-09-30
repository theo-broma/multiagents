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
  was hit, and each `wait_for_agents` caller's cursor into the event log.
  Every agent's server is its own process and the monitor another, so a
  notice deduplicated in one process's memory would be announced once per
  process. The state is host-owned, in the protected directory H7's startup
  health already uses: `tree.json` is container-writable, and an `active`
  entry forged there must not be able to suppress a real hit (review
  finding 3). `tree.json` carries a mirror for the monitor's display, and no
  decision ever reads the mirror back.
* **The terminal line** (LN-C3): the one `multiagents watch` prints, and a
  small tailer that `multiagents run` leaves beside the CLI it hands the
  terminal to.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

from .paths import (global_config_dir, project_slug, shipped_defaults_dir,
                    state_root)
from .tree import TERMINAL, Tree, now

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
# shared state (LN-C4) — host-owned, mirrored into tree.json


def _ident(key: str, scope: str) -> str:
    return f"{key}|{scope}"


def _clean(state: Any) -> dict:
    """The state's own shape, or a fresh one. Anything unreadable resets the
    dedup — a notice announced twice is the safe direction; one suppressed by
    a record nobody vouches for is exactly the hole finding 3 closed."""
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("active"), dict):
        state["active"] = {}
    if not isinstance(state.get("cleared"), list):
        state["cleared"] = []
    if not isinstance(state.get("cursors"), dict):
        state["cursors"] = {}
    return state


class NoticeState:
    """The dedup authority for limit notices, beside H7's startup health.

    `tree.json` sits under the project root, where every agent in the
    container can write it; a state kept there would let a forged `active`
    entry silence the next real hit of the same key. So the state lives in
    the host's own protected directory, under this lock, and `tree.json`
    only holds a copy for the monitor — written, never read back.

    When the directory cannot be used at all, the state degrades to this
    process's memory: dedup then holds within one server, which is still
    more than nothing, and still never consults the forgeable copy.
    """

    def __init__(self, tree: Tree):
        root = tree.path.resolve().parent.parent
        self.directory = state_root() / "host-authority" / project_slug(root)
        self.file = self.directory / "limit-notices.json"
        self.lock_path = self.directory / "limit-notices.lock"
        self.memory: dict | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
        except OSError:
            self.memory = _clean({})

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict]:
        """The state dict under its lock. Mutate it and `commit` it inside
        the `with`; a degraded state is committed by the mutation itself."""
        if self.memory is None:
            try:
                with self.lock_path.open("a+b") as lock:
                    os.fchmod(lock.fileno(), 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield self.read()
                    finally:
                        with contextlib.suppress(OSError):
                            fcntl.flock(lock, fcntl.LOCK_UN)
                return
            except OSError:
                self.memory = _clean({})
        yield self.memory

    def read(self) -> dict:
        if self.memory is not None:
            return self.memory
        try:
            state = json.loads(self.file.read_text())
        except (OSError, ValueError):
            return _clean({})
        return _clean(state)

    def commit(self, state: dict) -> None:
        """Persist the state, durably, through a file nobody reads in place."""
        if self.memory is not None:
            return
        try:
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=".notices-")
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(state, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, self.file)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
        except OSError:
            self.memory = state            # keep going without the file


_states: dict[str, NoticeState] = {}


def _state(tree: Tree) -> NoticeState:
    known = _states.get(str(tree.path))
    if known is None:
        known = _states[str(tree.path)] = NoticeState(tree)
    return known


def _mirror(tree: Tree, state: dict) -> None:
    """Copy the state into `tree.json` — display only (LN-C3: the monitor's
    alerts and event view). The copy a container agent can forge is never
    read back; the next decision overwrites it with the host's truth."""
    view = _clean(state)
    snapshot = {"active": view["active"],
                "cleared": view["cleared"][-KEEP_CLEARED:],
                "cursors": view["cursors"]}
    with contextlib.suppress(OSError):
        with tree.transaction() as data:
            data[STATE_KEY] = snapshot


def hit(tree: Tree, *, key: str, value: Any, effect: str, scope: str,
        source: dict[str, Any] | None, message: str,
        node: str | None = None) -> dict[str, Any]:
    """Record one constraint. The first under `(key, scope)` emits
    `limit_hit`; later ones while it is active only count. Returns the active
    notice, with its count."""
    state = _state(tree)
    with state.locked() as data:
        active = data["active"]
        ident = _ident(key, scope)
        entry = active.get(ident)
        if entry is not None:
            entry["count"] = int(entry.get("count", 1)) + 1
            entry["last_hit"] = now()
            result = dict(entry)
        else:
            entry = {"key": key, "value": value, "effect": effect, "scope": scope,
                     "node": node, "source": source, "message": message,
                     "count": 1, "first_hit": now(), "last_hit": now()}
            active[ident] = entry
            # Under the lock, so two processes hitting it at once agree on
            # which of them announced it.
            fields = {"key": key, "value": value, "effect": effect,
                      "scope": scope, "source": source, "message": message}
            if node:
                fields["node"] = node
            tree.emit(node or "-", "limit_hit", **fields)
            result = dict(entry)
        state.commit(data)
    _mirror(tree, data)
    return result


def clear(tree: Tree, match) -> list[dict[str, Any]]:
    """End every active notice `match(entry)` accepts, each with one
    `limit_cleared` carrying its count."""
    state = _state(tree)
    with state.locked() as data:
        ended: list[dict[str, Any]] = []
        for ident, entry in list(data["active"].items()):
            if not match(entry):
                continue
            del data["active"][ident]
            entry = {**entry, "cleared_at": now()}
            ended.append(entry)
            data["cleared"] = [*data["cleared"], entry][-KEEP_CLEARED:]
            tree.emit(entry.get("node") or "-", "limit_cleared",
                      key=entry["key"], scope=entry["scope"],
                      count=entry.get("count", 1),
                      message=f"limit cleared: {entry['key']} "
                              f"({entry['scope']}) after {entry.get('count', 1)} hit(s)")
        if not ended:
            return []                     # the common case: nothing to end
        state.commit(data)
    _mirror(tree, data)
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
    notice once. The cursor starts at the caller's CREATION (finding 6): a
    first wait reports every hit and clear since the caller began, plus
    whatever is still active now, whatever its birthday. The event bytes and
    the state are read outside every lock (finding 7), and a cursor only
    ever advances past whole lines, so a torn final line is picked up on the
    next call. Cursors of callers whose node has ended are pruned.
    """
    state = _state(tree)
    # Node facts for this one call: the caller's creation, and which callers
    # have ended and so lose their cursors (finding 7).
    nodes = tree.read().get("nodes") or {}
    mine = nodes.get(caller)
    created = mine.get("created_at") if isinstance(mine, dict) else None
    ended = {key for key, raw in nodes.items()
             if isinstance(raw, dict) and raw.get("status") in TERMINAL}
    path = tree.events_path
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    # What to show, read without a lock: an unlocked state read decides which
    # of the two paths this is, and the bytes move after it. Anything that
    # lands in between is an event too, and the next call shows it.
    snapshot = state.read()
    cursor = snapshot["cursors"].get(caller)
    out: list[dict[str, Any]] = []
    if caller in ended:
        pass                          # an ended caller is shown nothing new
    elif cursor is None or cursor > size:
        # First call (or a log that shrank underneath us). Root, which is no
        # node, keeps the old contract: what is active now.
        if created is None:
            out = [{"kind": "limit_hit", **_brief(entry)}
                   for entry in sorted(snapshot["active"].values(),
                                       key=lambda e: e.get("first_hit") or 0)]
        else:
            out, _ = _notices_between(path, _offset_at_or_after(path,
                                                                float(created)),
                                      size)
            out = _with_active(out, snapshot["active"])
        cursor_out = size
    else:
        out, cursor_out = _notices_between(path, cursor, size)

    with state.locked() as data:
        prune = [c for c in data["cursors"] if c in ended]
        moved = (caller not in ended
                 and data["cursors"].get(caller) != cursor_out)
        if not prune and not moved:
            return out                # the common poll: nothing moved
        for gone in prune:
            data["cursors"].pop(gone, None)
        if moved:
            data["cursors"][caller] = max(cursor_out,
                                          data["cursors"].get(caller, 0))
        state.commit(data)
        _mirror(tree, data)
    return out


def _stamp(line: bytes) -> float:
    """One event's time, or -1 for anything that is not a timestamped event."""
    try:
        value = json.loads(line).get("t")
    except ValueError:
        return -1.0
    return value if isinstance(value, (int, float)) else -1.0


def _offset_at_or_after(path: Path, created: float) -> int:
    """Byte offset of the first whole event stamped at/after `created`; the
    end of the last whole event when every event predates it. Events are
    appended in time order, so the scan stops at the first hit."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    start = 0                          # the next unparsed line's own offset
    tail = b""
    try:
        with path.open("rb") as handle:
            while start < size:
                chunk = handle.read(1 << 16)
                if not chunk:
                    break
                *lines, tail = (tail + chunk).split(b"\n")
                for line in lines:
                    if _stamp(line) >= created:
                        return start
                    start += len(line) + 1
    except OSError:
        return 0
    return start                        # past the last whole line


def _notices_between(path: Path, start: int,
                     end: int) -> tuple[list[dict[str, Any]], int]:
    """The limit notices among the whole lines in `[start, end)`, and the
    offset just past the last of those lines."""
    if end <= start:
        return [], start
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(end - start)
    except OSError:
        return [], start
    cut = chunk.rfind(b"\n") + 1
    out: list[dict[str, Any]] = []
    for line in chunk[:cut].splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("kind") in ("limit_hit", "limit_cleared"):
            out.append(_brief(event))
    return out, start + cut


def _with_active(out: list[dict[str, Any]],
                 active_map: dict) -> list[dict[str, Any]]:
    """Add what is constraining right now but began before this caller did:
    a notice still active is news to a caller that has never been shown one,
    whatever its birthday."""
    seen = {(e.get("key"), e.get("scope")) for e in out
            if e.get("kind") == "limit_hit"}
    for entry in sorted(active_map.values(),
                        key=lambda e: e.get("first_hit") or 0):
        if (entry.get("key"), entry.get("scope")) not in seen:
            out.append({"kind": "limit_hit", **_brief(entry)})
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
