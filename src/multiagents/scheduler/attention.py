"""TB2-R7: current holds requiring an orchestrator action, by hold episode."""
from __future__ import annotations

import json

from ..notify_sender import _held_seqs, _hold_mark


def current(store, db, nodes=None, *, subject=None, unacknowledged=False):
    nodes = store.nodes(db) if nodes is None else nodes
    held = [n for n in nodes.values() if n["state"] == "held"
            and (subject is None or n["created_by"] == subject)
            and (n.get("hold") or {}).get("reason") not in {"child_held", "stopped_by_orchestrator"}]
    # Only a window stop whose confirmation will clear this exact hold is
    # automatic. Other termination_unconfirmed holds still need root action.
    window_holds = {}
    if any((n.get("hold") or {}).get("reason") == "termination_unconfirmed" for n in held):
        for (raw,) in db.execute("SELECT record FROM attempts WHERE json_extract(record, '$.window_stop') = 1"):
            attempt = json.loads(raw)
            if not attempt.get("cancel_requested"):
                window_holds[attempt["node_id"]] = attempt.get("window_hold_revision")
    held = [n for n in held if not ((n.get("hold") or {}).get("reason") == "termination_unconfirmed"
            and window_holds.get(n["id"]) == n["revision"])]
    seqs = _held_seqs(db, [n["id"] for n in held])
    ack = int(store.meta(db, "ack")) if unacknowledged else -1
    return [{"node_id": n["id"], "seq": seqs.get(n["id"], 0),
             "mark": _hold_mark(n, seqs.get(n["id"])), "hold": n.get("hold") or {},
             **({"agent_id": n["runs"][-1]["run_id"]} if n.get("runs") else {})}
            for n in held if seqs.get(n["id"], 0) > ack]


def pending(store):
    """Unsignalled current holds. Delivery and ack_nodes have separate marks."""
    if not store.file.is_file():
        return []
    with store.transaction(write=False) as db:
        seen = dict(db.execute("SELECT key, value FROM meta WHERE key LIKE 'attention_sent:%'"))
        return [h for h in current(store, db)
                if seen.get("attention_sent:" + h["node_id"]) != h["mark"]]


def delivered(store, holds):
    with store.transaction() as db:
        for hold in holds:
            store.set_meta(db, "attention_sent:" + hold["node_id"], hold["mark"])


def message(holds):
    if not holds:
        return ""
    lines = ["Scheduler holds need your action:"]
    for entry in holds:
        hold = entry["hold"]
        lines.append(f"- {entry['node_id']}: {hold.get('reason', 'held')}; "
                     f"detail: {json.dumps(hold, ensure_ascii=False)}. "
                     "Resolve the cause and use relaunch_node, or close_node; "
                     "use answer_question for a pending question and update_node to correct the plan.")
    return "\n".join(lines)
