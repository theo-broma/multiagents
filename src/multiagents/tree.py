"""The project agent tree — shared, on-disk, lock-protected.

Recursion forces this design. A stdio MCP server is spawned *per client*, so an
orchestrator that delegates to an agent which delegates again ends up with three
independent server processes. An in-memory registry would give each of them its
own empty world. The tree therefore lives in one JSON file, mutated only under
an exclusive ``flock``, and every process reads the same picture.

``events.jsonl`` sits beside it: append-only, one line per state transition
across all agents, so an external watcher (a ``/loop``, a status line) can follow
a run without touching the tree or the MCP layer at all. Both files are written
through :func:`multiagents.redact.scrub`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .redact import scrub

# Terminal states never transition again.
TERMINAL = {"done", "failed", "cancelled", "discarded", "merged", "orphaned"}
ACTIVE = {"pending", "running", "stuck"}


def new_id() -> str:
    return "ag-" + uuid.uuid4().hex[:6]


def now() -> float:
    return time.time()


@dataclass
class Node:
    id: str
    agent: str
    provider: str
    model: str
    parent: str | None
    depth: int
    task: str = ""
    status: str = "pending"
    reason: str = ""
    branch: str = ""
    worktree: str = ""
    session_id: str = ""
    pid: int | None = None
    children: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    events: int = 0
    created_at: float = field(default_factory=now)
    started_at: float | None = None
    ended_at: float | None = None
    last_event_at: float | None = None
    summary: str = ""

    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        return (self.ended_at or now()) - start


class Tree:
    """Read/modify/write access to ``tree.json`` under an exclusive lock."""

    def __init__(self, tree_file: Path, events_file: Path):
        self.path = tree_file
        self.events_path = events_file
        self.lock_path = tree_file.with_suffix(".lock")

    # ------------------------------------------------------------------ io --

    def _empty(self) -> dict:
        return {"version": 1, "nodes": {}, "deferred": [], "cooldowns": {}}

    def _read_unlocked(self) -> dict:
        if not self.path.is_file():
            return self._empty()
        try:
            with self.path.open() as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            # A truncated tree is recoverable: the runs/ directory and
            # events.jsonl hold the real history, so start clean rather than
            # wedging every future call.
            return self._empty()
        data.setdefault("nodes", {})
        data.setdefault("deferred", [])
        data.setdefault("cooldowns", {})
        return data

    def _write_unlocked(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(scrub(data), handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[Any]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield handle
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """Exclusive read-modify-write. Mutate the yielded dict in place."""
        with self._locked():
            data = self._read_unlocked()
            yield data
            self._write_unlocked(data)

    def read(self) -> dict:
        with self._locked():
            return self._read_unlocked()

    # --------------------------------------------------------------- events --

    def emit(self, agent_id: str, kind: str, **fields: Any) -> None:
        """Append one line to the global event log. Never raises."""
        entry = scrub({"t": now(), "agent": agent_id, "kind": kind, **fields})
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- nodes --

    def add(self, node: Node) -> Node:
        with self.transaction() as data:
            data["nodes"][node.id] = asdict(node)
            if node.parent and node.parent in data["nodes"]:
                kids = data["nodes"][node.parent].setdefault("children", [])
                if node.id not in kids:
                    kids.append(node.id)
        self.emit(
            node.id, "created",
            agent=node.agent, provider=node.provider, model=node.model,
            parent=node.parent, depth=node.depth, branch=node.branch,
        )
        return node

    def get(self, agent_id: str) -> Node | None:
        raw = self.read()["nodes"].get(agent_id)
        return Node(**raw) if raw else None

    def update(self, agent_id: str, **fields: Any) -> None:
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            node.update(fields)

    def set_status(self, agent_id: str, status: str, reason: str = "") -> None:
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            if node.get("status") == status and node.get("reason") == reason:
                return
            node["status"] = status
            if reason:
                node["reason"] = reason
            if status == "running" and not node.get("started_at"):
                node["started_at"] = now()
            if status in TERMINAL:
                node["ended_at"] = now()
        self.emit(agent_id, "status", status=status, reason=reason)

    def note_event(self, agent_id: str, steps: int | None = None,
                   usage: dict | None = None, session_id: str | None = None) -> None:
        """Cheap hot-path update from the stream reader."""
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            node["events"] = node.get("events", 0) + 1
            node["last_event_at"] = now()
            if steps is not None:
                node["steps"] = steps
            if usage:
                node["usage"] = usage
            if session_id and not node.get("session_id"):
                node["session_id"] = session_id

    def children_of(self, agent_id: str) -> list[Node]:
        data = self.read()
        node = data["nodes"].get(agent_id, {})
        return [Node(**data["nodes"][c]) for c in node.get("children", []) if c in data["nodes"]]

    def active(self) -> list[Node]:
        return [Node(**n) for n in self.read()["nodes"].values() if n.get("status") in ACTIVE]

    def ancestry(self, agent_id: str) -> list[str]:
        """Root-first chain of ids down to `agent_id`, for cycle and depth checks."""
        nodes = self.read()["nodes"]
        chain, cursor, seen = [], agent_id, set()
        while cursor and cursor in nodes and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            cursor = nodes[cursor].get("parent")
        return list(reversed(chain))

    def rollup_usage(self, agent_id: str | None = None) -> dict[str, int]:
        """Total token usage for the whole tree, or one subtree."""
        nodes = self.read()["nodes"]
        if agent_id is None:
            selected = list(nodes.values())
        else:
            stack, selected = [agent_id], []
            while stack:
                current = stack.pop()
                if current in nodes:
                    selected.append(nodes[current])
                    stack.extend(nodes[current].get("children", []))
        total: dict[str, int] = {}
        for node in selected:
            for key, value in (node.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    total[key] = int(total.get(key, 0) + value)
        return total

    # ------------------------------------------------------------- deferred --

    def defer(self, spec: dict, retry_after: float, reason: str) -> None:
        with self.transaction() as data:
            data["deferred"].append(
                {"spec": spec, "retry_after": retry_after, "reason": reason, "queued_at": now()}
            )
        self.emit(spec.get("agent", "?"), "deferred", reason=reason, retry_after=retry_after)

    def due_deferred(self) -> list[dict]:
        current = now()
        with self.transaction() as data:
            due = [d for d in data["deferred"] if d["retry_after"] <= current]
            data["deferred"] = [d for d in data["deferred"] if d["retry_after"] > current]
        return due

    def set_cooldown(self, provider: str, until: float, reason: str) -> None:
        with self.transaction() as data:
            data["cooldowns"][provider] = {"until": until, "reason": reason}
        self.emit("-", "cooldown", provider=provider, until=until, reason=reason)

    def cooldown(self, provider: str) -> dict | None:
        entry = self.read()["cooldowns"].get(provider)
        if entry and entry.get("until", 0) > now():
            return entry
        return None

    # -------------------------------------------------------------- display --

    def render(self) -> str:
        """The tree as indented text — used by ``multiagents tree`` and the
        ``tree://project`` MCP resource."""
        data = self.read()
        nodes = data["nodes"]
        if not nodes:
            return "(no agents)"
        roots = [n for n in nodes.values() if not n.get("parent") or n["parent"] not in nodes]
        lines: list[str] = []

        def walk(node: dict, indent: str, last: bool, top: bool) -> None:
            # Only non-root nodes get a connector. Keying this off "have we
            # printed anything yet" made the second root render as a child of
            # the first.
            branch_glyph = "" if top else ("└─ " if last else "├─ ")
            status = node.get("status", "?")
            usage = node.get("usage") or {}
            tokens = usage.get("total") or usage.get("total_tokens") or 0
            bits = [f"{node['id']}", f"{node.get('agent','?')}", f"[{status}]"]
            if node.get("reason"):
                bits.append(f"({node['reason']})")
            if tokens:
                bits.append(f"{tokens:,}tok")
            if node.get("branch"):
                bits.append(node["branch"])
            lines.append(indent + branch_glyph + " ".join(bits))
            kids = [nodes[c] for c in node.get("children", []) if c in nodes]
            child_indent = indent if top else indent + ("   " if last else "│  ")
            for i, kid in enumerate(kids):
                walk(kid, child_indent, i == len(kids) - 1, top=False)

        for root in roots:
            walk(root, "", True, top=True)
        pending = len(data.get("deferred", []))
        if pending:
            lines.append(f"\n{pending} deferred task(s) waiting on quota")
        return "\n".join(lines)
