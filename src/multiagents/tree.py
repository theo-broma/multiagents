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
import shutil
import sys
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
        self.backup_path = tree_file.with_name(tree_file.name + ".bak")
        self.lock_path = tree_file.with_suffix(".lock")

    # ------------------------------------------------------------------ io --

    def _empty(self) -> dict:
        return {"version": 1, "nodes": {}, "deferred": [], "cooldowns": {},
                "pause": {}, "provider_health": {},
                "questions": [], "tickets": []}

    def _read_unlocked(self) -> dict:
        if not self.path.is_file():
            return self._empty()
        try:
            with self.path.open() as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            data = self._recover()
        if data is None:
            return self._empty()
        data.setdefault("nodes", {})
        data.setdefault("deferred", [])
        data.setdefault("pause", {})
        data.setdefault("provider_health", {})
        data.setdefault("cooldowns", {})
        data.setdefault("questions", [])
        data.setdefault("tickets", [])
        return data

    def _recover(self) -> dict | None:
        """Fall back to the previous copy after a corrupt read, loudly.

        Emptying the tree silently was the old behaviour, and it is the worst
        possible one: every session id, open question, queued ticket and
        deferred task disappears, and the next command reports a clean project
        as though nothing had been lost. The damaged file is kept, because the
        first thing anyone will want is to see what was in it.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            shutil.copy2(self.path, kept)
        except OSError:
            kept = None

        restored = None
        if self.backup_path.is_file():
            try:
                with self.backup_path.open() as handle:
                    restored = json.load(handle)
            except (json.JSONDecodeError, OSError):
                restored = None

        where = f" kept at {kept.name}" if kept else ""
        if restored is not None:
            # Heal it. Without writing the recovery back, every later read
            # re-recovers and re-warns — and the project stays one bad read away
            # from the empty case for as long as the damaged file sits there.
            # Safe here: _read_unlocked only ever runs while the lock is held.
            try:
                self._write_unlocked(restored)
            except OSError:
                pass
            print(f"multiagents: {self.path} was unreadable{where}; recovered "
                  f"{len(restored.get('nodes', {}))} agent(s) from "
                  f"{self.backup_path.name}", file=sys.stderr)
        else:
            print(f"multiagents: {self.path} was unreadable and no usable backup "
                  f"exists{where}. Starting from an empty tree — sessions, open "
                  f"questions and deferred work from before this point are lost. "
                  f"events.jsonl still holds the history.", file=sys.stderr)
        return restored

    def _write_unlocked(self, data: dict) -> None:
        """Replace the tree, durably, keeping the previous copy.

        `os.replace` is atomic, so no reader ever sees half a file — but atomic
        is not durable. Without the fsync below, a power cut can land the rename
        while the temp file's *contents* are still in the page cache, leaving a
        zero-length tree. The directory is synced too, or the rename itself can
        be lost.

        The `.bak` is the second half: fsync narrows the window and cannot close
        it, so there has to be something to fall back to.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file() and self.path.stat().st_size > 0:
            try:
                shutil.copy2(self.path, self.backup_path)
            except OSError:
                pass                      # a missing backup must not stop a write
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(scrub(data), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass                          # not all filesystems allow it

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
        learned = ""
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
                learned = session_id
        if learned:
            # The one field that cannot be reconstructed from anywhere else. A
            # tree lost to a corrupt write takes every session with it unless
            # the id also reached the append-only log.
            self.emit(agent_id, "session", session_id=learned)

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

    def usage_by_model(self) -> list[dict[str, Any]]:
        """Spend and tokens per provider/model, from our own stream accounting.

        The providers do not offer this: opencode's usage endpoint reports three
        whole-account windows and no breakdown, and agy reports nothing at all.
        We already parse per-run usage out of every stream, so the finer figure
        is ours to compute — and it is more useful than a vendor's would be,
        because it is joined to the agent that spent it.
        """
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for node in self.read()["nodes"].values():
            usage = node.get("usage") or {}
            if not usage:
                continue
            key = (node.get("provider") or "?", node.get("model") or "?")
            row = rows.setdefault(key, {
                "provider": key[0], "model": key[1], "runs": 0,
                "tokens": 0, "cost_usd": 0.0, "agents": set(),
            })
            row["runs"] += 1
            row["agents"].add(node.get("agent") or "?")
            row["tokens"] += int(usage.get("total") or usage.get("total_tokens") or 0)
            row["cost_usd"] += float(usage.get("cost_usd") or 0)
        out = []
        for row in rows.values():
            row["agents"] = sorted(row["agents"])
            row["cost_usd"] = round(row["cost_usd"], 6)
            out.append(row)
        out.sort(key=lambda r: (-r["cost_usd"], -r["tokens"]))
        return out

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

    # ------------------------------------------------------- provider health --
    #
    # A circuit breaker, and deliberately cause-agnostic. When a provider's
    # credentials were revoked mid-session, thirteen agents failed identically
    # before anyone noticed — and the only evidence was prose in the agents' own
    # output, which this project already learned not to classify on: a
    # classifier reading agent text once cooled a provider down because an
    # advisor used the word "quota" in a sentence.
    #
    # Counting consecutive failures needs none of that. A provider whose last
    # three runs all failed is broken whatever the reason, and continuing to
    # spawn into it is the failure worth preventing.

    def note_run_outcome(self, provider: str, ok: bool, threshold: int = 3,
                         reason: str = "") -> dict | None:
        """Record how a run ended. Returns trip details when the breaker opens."""
        if not provider:
            return None
        with self.transaction() as data:
            health = data["provider_health"].setdefault(
                provider, {"consecutive_failures": 0, "last_reason": ""})
            if ok:
                health["consecutive_failures"] = 0
                health["last_reason"] = ""
                return None
            health["consecutive_failures"] += 1
            health["last_reason"] = reason[:200]
            count = health["consecutive_failures"]
            if count < threshold or health.get("tripped"):
                return None
            health["tripped"] = now()
            trip = {"provider": provider, "failures": count, "reason": reason[:200]}
        self.emit("system", "provider_down", **trip)
        return trip

    def provider_health(self) -> dict:
        return self.read().get("provider_health", {})

    def clear_provider_health(self, provider: str) -> None:
        with self.transaction() as data:
            data["provider_health"].pop(provider, None)

    # ---------------------------------------------------------------- pause --
    #
    # A whole-tree stop, used when there is no provider left to run anything on.
    # Deliberately NOT a per-agent state: the failure it represents is global,
    # and a system that keeps spawning what it can while its checking agents are
    # unreachable is worse than one that stops — it writes code it cannot review
    # and nobody notices which half is missing.
    #
    # It lives in the tree rather than in memory because every nested agent runs
    # its own server process, and a pause only one of them knows about is not a
    # pause.

    def pause(self, until: float, reason: str, providers: list[str] | None = None) -> dict:
        """Record that there is nothing to run `providers` work on until `until`.

        Keeps the EARLIEST reset of any active pause, not the latest. Waking
        early costs one wasted check and an immediate re-pause; waking late
        blocks tasks whose provider came back ten minutes ago, and nothing
        would notice.
        """
        record = {"until": until, "reason": reason, "since": now(),
                  "providers": sorted(providers or [])}
        with self.transaction() as data:
            existing = data.get("pause") or {}
            if existing.get("until", 0) and existing["until"] <= until:
                return dict(existing)
            data["pause"] = record
        self.emit("system", "paused", reason=reason, until=until)
        return record

    def pause_state(self) -> dict:
        """The active pause, or {} — expired pauses clear themselves on read."""
        record = self.read().get("pause") or {}
        if not record:
            return {}
        if record.get("until", 0) <= now():
            self.resume("the window it was waiting for has passed")
            return {}
        return record

    def resume(self, reason: str = "") -> None:
        with self.transaction() as data:
            if not data.get("pause"):
                return
            data["pause"] = {}
        self.emit("system", "resumed", reason=reason)

    # ------------------------------------------------------------- deferred --

    def defer(self, spec: dict, retry_after: float, reason: str) -> dict:
        record = {"id": "df-" + uuid.uuid4().hex[:6], "spec": spec,
                  "retry_after": retry_after, "reason": reason, "queued_at": now()}
        with self.transaction() as data:
            data["deferred"].append(record)
        self.emit(spec.get("agent", "?"), "deferred", reason=reason, retry_after=retry_after)
        return record

    def due_deferred(self) -> list[dict]:
        """Entries whose window has passed. **Does not remove them.**

        It used to pop, which made any exception between the pop and the
        restart delete the whole remaining batch permanently. The caller drops
        each entry with drop_deferred once it has actually dealt with it, so a
        crash leaves work queued rather than losing it.
        """
        current = now()
        return [d for d in self.read()["deferred"] if d["retry_after"] <= current]

    def drop_deferred(self, deferred_id: str) -> bool:
        with self.transaction() as data:
            before = len(data["deferred"])
            data["deferred"] = [d for d in data["deferred"]
                                if d.get("id") != deferred_id]
            return len(data["deferred"]) < before

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

        pause = data.get("pause") or {}
        if pause and pause.get("until", 0) > now():
            waiting = int(pause["until"] - now())
            lines.append("")
            lines.append(f"  || PAUSED  {pause.get('reason', '')[:60]}")
            lines.append(f"     clears in ~{waiting // 60}m; deferred work "
                         f"restarts by itself")

        rollup = self.rollup_usage()
        total_cost = rollup.get("cost_usd", 0)
        total_tokens = rollup.get("total", 0) + rollup.get("total_tokens", 0)
        if total_tokens or total_cost:
            lines.append(f"\ntotal: {int(total_tokens):,} tokens, ${total_cost:.4f}")
        pending = len(data.get("deferred", []))
        if pending:
            lines.append(f"{pending} deferred task(s) waiting on quota")
        return "\n".join(lines)
