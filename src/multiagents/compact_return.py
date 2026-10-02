"""CW-R4: what the orchestrator is told when it comes back from a compaction.

The driver may now compact the orchestrator's session while agents run. The
summary the compaction leaves cannot know what happened during it — an agent
that finished, an adoption that failed — and the orchestrator's waits did not
survive the stop. So the driver takes a snapshot of the session's root agents
at the stop, and on the way back compares it with the tree as it is now and
says, in a bounded message, what is in flight and what to re-check first.

Nothing here names a provider: the message is read by whichever model the
orchestrator runs on.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from .tree import ACTIVE, DRIVER_ROLES

CAP = 4_000
# Statuses that mean the node is still somebody's business.
IN_FLIGHT = ACTIVE | {"awaiting_user"}
GONE = {"merged", "discarded"}
EVENTS_MAX_BYTES = 16 * 1024 * 1024
DETAIL_MAX = 300

CHECKLIST = """Before anything else:
- Re-read the latest handoff/progress entry of BRIEF.md: durable state outranks the summary of your conversation.
- Check agent_tree for an adoption still in progress or one that failed.
- collect_agent every unseen result before deciding anything about it. Never restart an agent that is still running: resume an interrupted one with steer_agent.
- list_questions if any are parked.
- Re-arm what was lost: a wait_for_agents on the running agents, and the quota wake if tasks are deferred.
- A judgement not written down before the compaction is gone: rebuild it from the ledger, the specs and the branches, never from memory."""


def _roots(data: dict, session: str) -> dict[str, dict]:
    """The session's root agents that are not merged or discarded."""
    out = {}
    for key, node in (data.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("parent"):
            continue
        if node.get("role", "") in DRIVER_ROLES or node.get("status") in GONE:
            continue
        if session and node.get("session", "") not in {"", session}:
            continue
        out[key] = {"status": str(node.get("status", "")),
                    "agent": str(node.get("agent", ""))}
    return out


def _waiting(data: dict) -> list[dict]:
    return [d for d in data.get("deferred") or []
            if isinstance(d, dict) and d.get("status", "waiting") == "waiting"]


def _in_flight(tree, data: dict, session: str) -> bool:
    nodes = data.get("nodes") or {}
    if any(isinstance(n, dict) and n.get("status") in IN_FLIGHT
           and n.get("role", "") not in DRIVER_ROLES for n in nodes.values()):
        return True
    return bool(tree.unseen(session) or _waiting(data) or tree.open_questions())


def snapshot(tree, session: str, tokens: int | None) -> dict:
    """The state at the stop: kept until the relaunch, compared with it then."""
    data = tree.read()
    try:
        offset = tree.events_path.stat().st_size
    except OSError:
        offset = 0
    return {"at": time.time(), "session": session, "tokens": tokens,
            "agents": _roots(data, session), "events_offset": offset,
            "in_flight": _in_flight(tree, data, session)}


def _events_since(tree, offset: int) -> dict[str, list[dict]]:
    """Adoption events written since the stop, per agent."""
    out: dict[str, list[dict]] = {}
    try:
        with open(tree.events_path, "rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            start = offset if 0 <= offset <= end else 0
            fh.seek(max(start, end - EVENTS_MAX_BYTES))
            raw = fh.read()
    except OSError:
        return out
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if '"adopt' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") in ("adopted", "adopt_failed"):
            out.setdefault(str(event.get("agent", "")), []).append(event)
    return out


def _one_line(text: str, limit: int = DETAIL_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _figures(snap: dict, detail: str) -> str:
    found = re.search(r"(\d[\d,]*)\s*(?:->|→|to)\s*(\d[\d,]*)", detail or "")
    before = after = None
    if found:
        before, after = (int(g.replace(",", "")) for g in found.groups())
    if before is None and isinstance(snap.get("tokens"), int):
        before = snap["tokens"]

    def show(n):
        return f"{n:,}" if isinstance(n, int) else "unknown"
    return f"{show(before)} → {show(after)} tokens"


def _first_line(snap: dict, outcome: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(snap.get("at") or time.time()))
    waits = ("Your MCP waits that were running (wait_for_agents) were interrupted "
             "and did not survive; any other wait or wake you had armed must be "
             "checked.")
    code = outcome.get("code")
    if code == 0:
        return (f"This session was compacted by the driver at {when} "
                f"({_figures(snap, outcome.get('detail', ''))}). {waits}")
    if code == outcome.get("unsupported"):
        return (f"The driver stopped this session at {when} to compact it, but it "
                f"cannot be compacted from outside, so nothing was compacted. {waits}")
    why = _one_line(outcome.get("detail") or "") or "no reason given"
    return (f"The driver stopped this session at {when} to compact it, but the "
            f"compaction failed (exit {code}): {why}. The conversation is as it "
            f"was. {waits}")


def _label(before: str | None, now: str, events: list[dict]) -> str:
    failed = [e for e in events if e.get("kind") == "adopt_failed"]
    if failed:
        return f"adoption failed: {_one_line(failed[-1].get('detail') or '', 160)}"
    if now == "awaiting_user":
        return "awaiting decision"
    if before in ACTIVE and now not in IN_FLIGHT:
        return "finished during compaction"
    if now == "detached":
        return "not yet adopted"
    if any(e.get("kind") == "adopted" for e in events):
        return "adopted"
    return ""


def _rows(snap: dict, now: dict[str, dict], events: dict) -> list[str]:
    then = snap.get("agents") or {}
    ranked = []
    for order, (agent_id, info) in enumerate(now.items()):
        before = (then.get(agent_id) or {}).get("status")
        status = info["status"]
        label = _label(before, status, events.get(agent_id, []))
        if before is None:
            moved = f"{status} (not there at the stop)"
        elif before != status:
            moved = f"{before} → {status}"
        else:
            moved = status
        row = f"- {agent_id} ({info['agent']}): {moved}" + (f" — {label}" if label else "")
        rank = 0 if label else (1 if status in IN_FLIGHT else 2)
        ranked.append((rank, order, row))
    return [row for _, _, row in sorted(ranked)]


def _fit_rows(rows: list[str], budget: int, more: str) -> str:
    whole = "\n".join(rows)
    if len(whole) <= budget:
        return whole
    out, used = [], 0
    for i, row in enumerate(rows):
        tail = 0 if i == len(rows) - 1 else len(more.format(n=len(rows) - i - 1)) + 1
        if used + len(row) + 1 + tail > budget:
            out.append(more.format(n=len(rows) - i))
            break
        out.append(row)
        used += len(row) + 1
    return "\n".join(out)


def _fit_ids(head: str, ids: list[str], budget: int, more: str) -> str:
    if not ids:
        return f"{head}: none"
    whole = f"{head} ({len(ids)}): " + ", ".join(ids)
    if len(whole) <= budget:
        return whole
    text = f"{head} ({len(ids)}):"
    for i, agent_id in enumerate(ids):
        tail = 0 if i == len(ids) - 1 else len(more.format(n=len(ids) - i - 1)) + 1
        if len(text) + 1 + len(agent_id) + tail > budget:
            return f"{text} {more.format(n=len(ids) - i)}"
        text += f" {agent_id}" + ("," if i < len(ids) - 1 else "")
    return text


def _shares(needs: list[int], total: int) -> list[int]:
    """Split `total` between sections: none gets more than it needs, and what
    a small one leaves is shared by the rest."""
    shares, left, pending = [0] * len(needs), max(total, 0), list(range(len(needs)))
    while pending:
        each = left // len(pending)
        small = [i for i in pending if needs[i] <= each]
        if not small:
            for i in pending:
                shares[i] = each
            break
        for i in small:
            shares[i] = needs[i]
            left -= needs[i]
            pending.remove(i)
    return shares


def message(tree, session: str, snap: dict, outcome: dict) -> str:
    """The return message: one line, the agents, what waits, the checklist.

    `outcome` is the compaction's: `code` (0 compacted), `detail`, and
    `unsupported`, the code that means it cannot be done. At most `CAP`
    characters whatever the tree holds: every list gives way first.
    """
    data = tree.read()
    first = _first_line(snap, outcome)
    rows = _rows(snap, _roots(data, session), _events_since(tree, snap.get("events_offset", 0)))
    unseen = [n.id for n in tree.unseen(session)]
    questions = [q.get("id", "") for q in tree.open_questions()]
    waiting = _waiting(data)
    if waiting:
        earliest = min(float(d.get("retry_after") or 0) for d in waiting)
        stamp = datetime.fromtimestamp(earliest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        deferred = f"Deferred tasks: {len(waiting)}, earliest restart {stamp}"
    else:
        deferred = "Deferred tasks: none"
    tickets = f"Open tickets: {len(tree.open_tickets())}"
    agents_head = "Root agents (status at the stop → now):"

    fixed = "\n\n".join([first, agents_head, deferred, tickets, CHECKLIST])
    budget = CAP - len(fixed) - 8           # the joins the sections add
    more_agents = "+{n} more — see agent_tree"
    needs = [len("\n".join(rows)),
             len(_fit_ids("Unseen results", unseen, CAP * 50, "")),
             len(_fit_ids("Parked questions", questions, CAP * 50, ""))]
    shares = _shares(needs, budget)
    while True:
        agents = _fit_rows(rows, shares[0], more_agents) if rows else "- none"
        unseen_line = _fit_ids("Unseen results", unseen, shares[1],
                               "+{n} more — see agent_tree")
        questions_line = _fit_ids("Parked questions", questions, shares[2],
                                  "+{n} more — see list_questions")
        text = "\n".join([first, "", agents_head, agents, "", unseen_line,
                          questions_line, deferred, tickets, "", CHECKLIST])
        if len(text) <= CAP or not any(shares):
            return text[:CAP]
        shares = [int(s * 0.9) for s in shares]
