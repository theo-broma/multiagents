"""Adversary round 3 (M1): scoped RPC rules that survived mutation."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _host_write(live, node_id, **fields):
    from multiagents.scheduler.store import Store
    store = Store(live.root)
    with store.transaction() as db:
        record = store.nodes(db)[node_id]
        record.update(fields)
        store.save_node(db, record)


def _raw(live, request: dict) -> dict:
    with live.connect() as conn:
        conn.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            assert chunk, buf
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


def test_adv3_a_client_supplied_rounds_rejected_is_refused_on_create(live):
    child = live.create()
    before = live.snapshot()
    reply = live.create_raw({"kind": "loop", "children": [child["id"]],
                             "loop": {"verdict_child": child["id"], "max_rounds": 3,
                                      "rounds_rejected": 2}})
    assert code(reply) == "invalid", reply
    assert live.snapshot() == before


def test_adv3_a_renewed_capability_cannot_edit_or_cancel_its_own_node(live):
    own = live.create()
    first = live.issue("run-1", own["id"], {"read", "delegate"})
    mine = live.create(first)                     # created_by run-1
    renewed = live.issue("run-1", mine["id"], {"read", "delegate"})
    before = live.snapshot()
    assert code(live.update_raw(mine["id"], token=renewed, task="self-edit")) == "forbidden"
    assert code(live.cancel_raw(mine["id"], token=renewed)) == "forbidden"
    assert live.snapshot() == before


def test_adv3_cancel_of_a_done_node_is_refused(live):
    node = live.create()
    _host_write(live, node["id"], state="done", outcome="success")
    before = live.snapshot()
    assert code(live.cancel_raw(node["id"])) == "invalid"
    assert live.snapshot() == before


def test_adv3_cancelling_a_composite_keeps_a_done_child_done(live):
    a, b = live.create(), live.create()
    group = live.create(kind="group", children=[a["id"], b["id"]])
    _host_write(live, a["id"], state="done", outcome="success")
    assert live.cancel_raw(group["id"])["ok"] is True
    done = live.get(a["id"])
    assert done["state"] == "done" and done["outcome"] == "success", done
    assert live.get(b["id"])["state"] == "cancelled"


def test_adv3_a_run_waiting_without_a_cursor_starts_from_zero_not_roots_ack(live):
    own = live.create()
    token = live.issue("run-1", own["id"], {"read", "delegate"})
    child = live.create(token)
    top = live.ok("wait_for_nodes", {"timeout": 0})["next_cursor"]
    live.ok("ack_nodes", {"cursor": top})
    seen = live.ok("wait_for_nodes", {"timeout": 0}, token=token)["transitions"]
    assert child["id"] in [t.get("node_id") for t in seen], seen


def test_adv3_a_capability_revoked_during_a_wait_gets_no_transition(live):
    # `own` depends on a cancelled node, so it can never become eligible and no
    # launch transition can reach the waiter before the revocation (NC-R15
    # makes a wait return the transitions available to it).
    blocker = live.create()
    assert live.cancel_raw(blocker["id"])["ok"] is True
    own = live.create(depends_on=[{"node": blocker["id"], "require": "success"}])
    token = live.issue("run-1", own["id"], {"read", "delegate"})
    top = live.ok("wait_for_nodes", {"timeout": 0}, token=token)["next_cursor"]
    out = {}
    waiter = threading.Thread(target=lambda: out.update(reply=live.rpc(
        "wait_for_nodes", {"cursor": top, "timeout": 8}, token=token, timeout=15)))
    waiter.start()
    time.sleep(0.3)
    live.revoke("run-1")
    live.update(own["id"], task="changed after revocation")
    waiter.join(timeout=15)
    assert code(out["reply"]) == "unauthenticated", out


def test_adv3_the_gate_off_reply_from_a_running_scheduler_counts_pending_nodes(live):
    live.create()
    live.create()
    token = live.root_token()
    live.set_gate(False)
    reply = live.rpc("create_node", {"kind": "simple", "agent": "worker", "task": "t",
                                     "plan_revision": 2}, token=token)
    assert reply["error"] == {"error": "scheduler_disabled", "pending_nodes": 2}, reply


def test_adv3_a_request_without_request_id_is_invalid(live):
    token = live.root_token()
    for op, args in [("create_node", {"kind": "simple", "agent": "worker", "task": "t",
                                      "plan_revision": 0}),
                     ("scheduler_status", {})]:
        reply = _raw(live, {"op": op, "args": args, "token": token})
        assert code(reply) == "invalid", (op, reply)
    assert live.snapshot()["nodes"] == []


def test_adv3_an_unknown_op_is_unknown_op(live):
    reply = live.rpc("drop_all_nodes", {})
    assert code(reply) == "unknown_op", reply


def test_adv3_a_lone_surrogate_in_a_string_is_invalid(live):
    token = live.root_token()
    line = ('{"op":"create_node","token":"%s","request_id":"s1","args":{"kind":"simple",'
            '"agent":"worker","task":"bad \\ud800 text","plan_revision":0}}' % token)
    with live.connect() as conn:
        conn.sendall(line.encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            assert chunk
            buf += chunk
    reply = json.loads(buf.split(b"\n", 1)[0])
    assert code(reply) == "invalid", reply
    assert live.snapshot()["nodes"] == []


def test_adv3_a_mistyped_verdict_node_id_is_invalid_not_internal(live):
    own = live.create()
    token = live.issue("run-1", own["id"], {"read", "verdict"})
    for bad in ([own["id"]], {"id": own["id"]}):
        reply = live.rpc("give_verdict", {"node_id": bad, "verdict": "approved"}, token=token)
        assert code(reply) == "invalid", reply
