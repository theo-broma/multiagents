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

from .redact import depersonalise, scrub

# Terminal states never transition again.
TERMINAL = {"done", "failed", "cancelled", "discarded", "merged", "orphaned"}
ACTIVE = {"pending", "running", "stuck"}
# Two states are neither, for the same reason: the process has exited but the
# session is resumable, so they must not be counted against the concurrency
# limit, reaped as orphans, or cleaned up as finished work.
#   idle           a standing conversation between turns
#   awaiting_user  parked on a question only a human can answer
AWAITING = "awaiting_user"
PAUSED = {"idle", AWAITING}


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
    conversation: bool = False        # a standing dialogue, resumed each turn
    turns: int = 0
    paused_at: float | None = None    # entered idle / awaiting_user at
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
        return {"version": 1, "nodes": {}, "deferred": [], "cooldowns": {},
                "questions": [], "tickets": []}

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
        data.setdefault("questions", [])
        data.setdefault("tickets", [])
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
            node["paused_at"] = now() if status in PAUSED else None
            if status in TERMINAL:
                node["ended_at"] = now()
            else:
                # Leaving a terminal state must clear it. steer() stops an agent
                # (terminal: cancelled) and relaunches it, and without this the
                # node's elapsed() stays frozen at the moment of the stop for the
                # rest of its life. answer_question() uses the same path.
                node["ended_at"] = None
        self.emit(agent_id, "status", status=status, reason=reason)

    def note_event(self, agent_id: str, steps: int | None = None,
                   usage: dict | None = None, session_id: str | None = None,
                   events: int = 1) -> None:
        """Flush accumulated stream progress for one agent.

        `events` is a batch count, not a single increment. Every call here
        flocks, reads and rewrites the whole tree, so calling it per stream line
        turned a 39-event run into 39 full rewrite cycles — and with several
        agents streaming at once that is lock contention on the one file every
        nested server shares. The reader batches; this writes the total.
        """
        with self.transaction() as data:
            node = data["nodes"].get(agent_id)
            if node is None:
                return
            node["events"] = node.get("events", 0) + events
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
        total: dict[str, float] = {}
        for node in selected:
            for key, value in (node.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    total[key] = total.get(key, 0) + value
        # Token counts are whole; cost is dollars and must keep its fraction —
        # rounding it to int silently reported every run as free.
        return {
            k: (round(v, 6) if k.endswith("_usd") else int(v))
            for k, v in total.items()
        }

    # ------------------------------------------------------------ questions --
    #
    # Authoritative records live here rather than in a side file: several server
    # processes hold this tree (one per nested agent), it is already flock-
    # protected and already scrubbed on write, and answering is a read-modify-
    # write that a plain append cannot do safely. events.jsonl keeps the
    # append-only audit trail, mirroring the tree.json / events.jsonl split.

    def add_question(self, agent_id: str, topic: str, question: str,
                     proposed: str = "") -> dict:
        record = {
            "id": "q-" + uuid.uuid4().hex[:6],
            "agent": agent_id,
            "topic": topic,
            "question": question,
            "proposed_default": proposed,
            "asked_at": now(),
            "status": "open",
            "answer": "",
            "answered_at": None,
            "answered_by": "",
        }
        with self.transaction() as data:
            data["questions"].append(record)
        self.emit(agent_id, "question", topic=topic, question=question[:300],
                  proposed=proposed[:200], question_id=record["id"])
        return record

    def open_questions(self, agent_id: str | None = None) -> list[dict]:
        return [
            q for q in self.read()["questions"]
            if q.get("status") == "open" and (agent_id is None or q.get("agent") == agent_id)
        ]

    def get_question(self, question_id: str) -> dict | None:
        return next((q for q in self.read()["questions"] if q["id"] == question_id), None)

    def answer_question(self, question_id: str, answer: str,
                        answered_by: str = "user") -> dict | None:
        """Record an answer. Returns the updated record, or None if unknown.

        Claiming and answering happen in one locked transaction so two
        processes cannot both decide they are the one resuming the agent.
        """
        with self.transaction() as data:
            record = next((q for q in data["questions"] if q["id"] == question_id), None)
            if record is None:
                return None
            if record.get("status") == "answered":
                return dict(record, already_answered=True)
            record.update({"status": "answered", "answer": answer,
                           "answered_at": now(), "answered_by": answered_by})
            result = dict(record)
        self.emit(result["agent"], "answered", question_id=question_id,
                  answered_by=answered_by, answer=answer[:300])
        return result

    # -------------------------------------------------------------- tickets --
    #
    # Bugs in multiagents *itself*, written up by the bug-reporter agent and
    # queued for the orchestrator. They live beside questions for the same
    # reasons — one lock, one scrub, read-modify-write on status — but they
    # differ in a way that matters: a question stays on this machine, and a
    # ticket is written to be published. Everything stored here has been
    # depersonalised on the way in, so what the orchestrator reads is already
    # what would be posted.

    TICKET_SEVERITIES = ("blocking", "minor")

    def add_ticket(self, agent_id: str, title: str, body: str,
                   severity: str = "minor", proposed_fix: str = "",
                   project_root=None) -> dict:
        record = {
            "id": "bug-" + uuid.uuid4().hex[:6],
            "agent": agent_id,
            "title": title[:200],
            "body": body,
            "proposed_fix": proposed_fix,
            "severity": severity if severity in self.TICKET_SEVERITIES else "minor",
            "filed_at": now(),
            # open -> reported (submitted upstream) | fixed (handled locally)
            # | declined (the user said no) | awaiting_user (needs a decision)
            "status": "open",
            "url": "",
            "note": "",
            "resolved_at": None,
        }
        record = depersonalise(record, project_root)
        with self.transaction() as data:
            data["tickets"].append(record)
        self.emit(agent_id, "ticket", ticket_id=record["id"],
                  severity=record["severity"], title=record["title"])
        return record

    def open_tickets(self, severity: str | None = None) -> list[dict]:
        return [
            t for t in self.read()["tickets"]
            if t.get("status") in ("open", "awaiting_user")
            and (severity is None or t.get("severity") == severity)
        ]

    def get_ticket(self, ticket_id: str) -> dict | None:
        return next((t for t in self.read()["tickets"] if t["id"] == ticket_id), None)

    def set_ticket_status(self, ticket_id: str, status: str, note: str = "",
                          url: str = "") -> dict | None:
        with self.transaction() as data:
            record = next((t for t in data["tickets"] if t["id"] == ticket_id), None)
            if record is None:
                return None
            record.update({"status": status, "note": note or record.get("note", "")})
            if url:
                record["url"] = url
            if status in ("reported", "fixed", "declined"):
                record["resolved_at"] = now()
            result = dict(record)
        self.emit(result["agent"], "ticket_status", ticket_id=ticket_id,
                  status=status, url=url)
        return result

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
        roots = [n for n in nodes.values() if not n.get("parent") or n["parent"] not in nodes]
        # Not an early return: a project with no live agents can still have a
        # question parked or a bug ticket queued, and those are exactly what
        # someone runs this to find.
        lines: list[str] = [] if nodes else ["(no agents)"]

        def walk(node: dict, indent: str, last: bool, top: bool) -> None:
            # Only non-root nodes get a connector. Keying this off "have we
            # printed anything yet" made the second root render as a child of
            # the first.
            branch_glyph = "" if top else ("└─ " if last else "├─ ")
            status = node.get("status", "?")
            usage = node.get("usage") or {}
            tokens = usage.get("total") or usage.get("total_tokens") or 0
            cost = usage.get("cost_usd") or 0
            label = f"[{status}]"
            if node.get("conversation"):
                label = f"[{status} · {node.get('turns', 0)} turns]"
            if status == AWAITING:
                # An agent parked overnight would otherwise read as a runaway,
                # because elapsed() keeps growing while it waits on a human.
                waited = now() - (node.get("paused_at") or now())
                label = f"[awaiting you · {waited / 60:.0f}m]"
            bits = [f"{node['id']}", f"{node.get('agent','?')}", label]
            if node.get("reason"):
                bits.append(f"({node['reason']})")
            if tokens:
                bits.append(f"{tokens:,}tok")
            if cost:
                bits.append(f"${cost:.4f}")
            if node.get("branch"):
                bits.append(node["branch"])
            lines.append(indent + branch_glyph + " ".join(bits))
            kids = [nodes[c] for c in node.get("children", []) if c in nodes]
            child_indent = indent if top else indent + ("   " if last else "│  ")
            for i, kid in enumerate(kids):
                walk(kid, child_indent, i == len(kids) - 1, top=False)

        for root in roots:
            walk(root, "", True, top=True)
        pending = [q for q in data.get("questions", []) if q.get("status") == "open"]
        if pending:
            lines.append("")
            for q in pending[:6]:
                lines.append(f"  ? {q['agent']} asks about {q['topic']}: {q['question'][:70]}")
            lines.append(f"  answer with `multiagents ask`  ({len(pending)} open)")

        tickets = [t for t in data.get("tickets", [])
                   if t.get("status") in ("open", "awaiting_user")]
        if tickets:
            lines.append("")
            for t in tickets[:6]:
                mark = "!" if t.get("severity") == "blocking" else "·"
                lines.append(f"  {mark} bug {t['id']}: {t['title'][:66]}")
            lines.append(f"  review with `multiagents tickets`  ({len(tickets)} open)")

        rollup = self.rollup_usage()
        total_cost = rollup.get("cost_usd", 0)
        total_tokens = rollup.get("total", 0) + rollup.get("total_tokens", 0)
        if total_tokens or total_cost:
            lines.append(f"\ntotal: {int(total_tokens):,} tokens, ${total_cost:.4f}")
        pending = len(data.get("deferred", []))
        if pending:
            lines.append(f"{pending} deferred task(s) waiting on quota")
        return "\n".join(lines)
