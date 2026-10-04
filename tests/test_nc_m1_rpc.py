"""NC-R8, NC-R9, NC-R10, NC-R13 (read side), NC-R58, NC-R60, NC-R71 (milestone
M1): the unix-socket protocol, capabilities, scope, and request-id idempotency.

NC-R11 (a same-uid run reading a sibling's token from /proc) is an accepted
residual and is deliberately not tested. Everything else about identity is:
forged, absent, revoked and mistyped tokens, supplied parent/run/caller ids,
the root token never reaching a container.

Assumptions (also in the run report):
  * a `forbidden` is also what a run gets for reading a node outside its subtree;
  * a run that gives no `parent` either is refused or lands under its own node,
    but never creates a top-level node;
  * NC-R59 ("may cancel nodes it created, never its own node") amends NC-R10's
    "cancel on that subtree"; the stricter text is tested;
  * a run learns the plan revision from `list_nodes`, like root.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import (ABSENT, ROOT, alive, code, current_revision, find_count,  # noqa: E402,F401
                        files_under, live, nc, problems, stored, tnode, tname)


@pytest.fixture
def delegating(live):
    """Root has a node N; run `run-1` owns it with read+delegate."""
    node = live.create(task="the delegating run's node")
    token = live.issue("run-1", node["id"], {"read", "delegate"})
    return live, node, token


# ================================================================== NC-R8: protocol

def test_nc_r8_a_reply_echoes_the_request_id_and_says_ok(live):
    reply = live.rpc("scheduler_status", request_id="my-id-123")
    assert reply["request_id"] == "my-id-123"
    assert reply["ok"] is True and "result" in reply


def test_nc_r8_an_error_reply_echoes_the_request_id_and_is_not_ok(live):
    reply = live.rpc("get_node", {"id": "nd-00000000"}, request_id="err-1")
    assert reply["request_id"] == "err-1" and reply["ok"] is False


def test_nc_r8_one_connection_carries_many_requests_answered_in_order(live):
    token = live.root_token()
    with live.connect() as conn:
        for i in range(3):
            conn.sendall(json.dumps({"op": "scheduler_status", "token": token, "args": {},
                                     "request_id": f"r{i}"}).encode() + b"\n")
        data = b""
        while data.count(b"\n") < 3:
            chunk = conn.recv(65536)
            assert chunk
            data += chunk
    replies = [json.loads(line) for line in data.splitlines()]
    assert [r["request_id"] for r in replies] == ["r0", "r1", "r2"]
    assert all(r["ok"] for r in replies)


def test_nc_r8_a_request_may_arrive_in_pieces(live):
    token = live.root_token()
    line = json.dumps({"op": "scheduler_status", "token": token, "args": {},
                       "request_id": "pieces"}).encode() + b"\n"
    with live.connect() as conn:
        for i in range(0, len(line), 7):
            conn.sendall(line[i:i + 7])
            time.sleep(0.005)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            assert chunk
            data += chunk
    assert json.loads(data)["request_id"] == "pieces"


@pytest.mark.parametrize("garbage", [
    b"not json at all\n", b"[]\n", b"null\n", b'"a string"\n', b"{}\n", b"\xff\xfe\xfd\n",
    b'{"op": 5, "token": 5, "args": 5, "request_id": 5}\n',
    b'{"op": "list_nodes"}\n',
])
def test_nc_r8_a_malformed_request_neither_kills_the_scheduler_nor_acts(live, garbage):
    live.create(task="witness")
    before = live.snapshot()
    pid = live.pid()
    with live.connect() as conn:
        conn.sendall(garbage)
        conn.settimeout(2)
        try:
            conn.recv(65536)
        except OSError:
            pass
    assert live.pid() == pid and alive(pid)
    assert live.snapshot() == before


def test_nc_r8_an_unknown_op_is_not_ok(live):
    reply = live.rpc("make_me_a_sandwich")
    assert reply["ok"] is False


def test_nc_r8_a_client_survives_a_scheduler_restart_by_reconnecting(live):
    node = live.create(task="before restart")
    live.restart()
    assert live.get(node["id"])["task"] == "before restart"
    live.restart(hard=True)                    # a stale socket file is replaced
    assert live.get(node["id"])["task"] == "before restart"


# ================================================================== NC-R60: idempotency

NEW = {"kind": "simple", "agent": "worker", "task": "once"}


def test_nc_r60_a_replayed_create_creates_one_node_and_returns_the_original_reply(live):
    base = live.plan_revision()
    first = live.create_raw({**NEW, "plan_revision": base}, request_id="req-A")
    second = live.create_raw({**NEW, "plan_revision": base}, request_id="req-A")
    assert first["ok"] is True and second["ok"] is True
    assert second["result"]["id"] == first["result"]["id"]
    assert second["result"]["revision"] == first["result"]["revision"]
    assert len(live.snapshot()["nodes"]) == 1


def test_nc_r60_the_replay_is_answered_even_though_the_plan_revision_moved(live):
    base = live.plan_revision()
    first = live.create_raw({**NEW, "plan_revision": base}, request_id="req-B")
    live.create(task="someone else moved the plan on")
    again = live.create_raw({**NEW, "plan_revision": base}, request_id="req-B")
    assert again["ok"] is True and again["result"]["id"] == first["result"]["id"]
    assert len(live.snapshot()["nodes"]) == 2


def test_nc_r60_a_replay_records_no_second_transition(live):
    base = live.plan_revision()
    for _ in range(3):
        live.create_raw({**NEW, "plan_revision": base}, request_id="req-C")
    assert live.names().count("created") == 1


def test_nc_r60_the_same_id_with_a_different_payload_is_refused(live):
    base = live.plan_revision()
    live.create_raw({**NEW, "plan_revision": base}, request_id="req-D")
    before = live.snapshot()
    reply = live.create_raw({**NEW, "task": "different", "plan_revision": base},
                            request_id="req-D")
    assert reply["ok"] is False and code(reply) == "request_id_reused", reply
    assert live.snapshot() == before


def test_nc_r60_the_same_id_for_a_different_op_is_refused(live):
    node = live.create()
    live.rpc("update_node", {"id": node["id"], "revision": node["revision"], "task": "x"},
             request_id="req-E")
    reply = live.rpc("cancel_node", {"id": node["id"], "revision": node["revision"] + 1},
                     request_id="req-E")
    assert reply["ok"] is False and code(reply) == "request_id_reused", reply
    assert live.get(node["id"])["state"] == "open"


def test_nc_r60_a_replayed_update_applies_once(live):
    node = live.create()
    args = {"id": node["id"], "revision": node["revision"], "task": "edited"}
    first = live.rpc("update_node", args, request_id="req-F")
    second = live.rpc("update_node", args, request_id="req-F")
    assert first["ok"] is True and second["ok"] is True
    assert second["result"]["revision"] == first["result"]["revision"]
    assert live.get(node["id"])["revision"] == node["revision"] + 1
    assert live.names().count("updated") == 1


def test_nc_r60_a_replayed_cancel_applies_once(live):
    node = live.create()
    args = {"id": node["id"], "revision": node["revision"]}
    first = live.rpc("cancel_node", args, request_id="req-G")
    second = live.rpc("cancel_node", args, request_id="req-G")
    assert first["ok"] is True and second["ok"] is True
    assert live.get(node["id"])["revision"] == node["revision"] + 1
    assert live.names().count("cancelled") == 1


def test_nc_r60_a_replayed_ack_is_harmless(live):
    live.create()
    nxt = live.ok("wait_for_nodes", {"timeout": 1})["next_cursor"]
    first = live.rpc("ack_nodes", {"cursor": nxt}, request_id="req-H")
    second = live.rpc("ack_nodes", {"cursor": nxt}, request_id="req-H")
    assert first["ok"] is True and second["ok"] is True


def test_nc_r60_concurrent_replays_of_one_request_create_exactly_one_node(live):
    base = live.plan_revision()
    replies = live.in_threads([
        (lambda: live.create_raw({**NEW, "plan_revision": base}, request_id="req-I"))
        for _ in range(8)])
    assert all(isinstance(r, dict) and r.get("ok") is True for r in replies), replies
    assert len({r["result"]["id"] for r in replies}) == 1
    assert len(live.snapshot()["nodes"]) == 1
    assert live.names().count("created") == 1


@pytest.mark.parametrize("hard", [False, True])
def test_nc_r60_deduplication_survives_a_scheduler_restart(live, hard):
    base = live.plan_revision()
    first = live.create_raw({**NEW, "plan_revision": base}, request_id="req-J")
    live.restart(hard=hard)
    again = live.create_raw({**NEW, "plan_revision": base}, request_id="req-J")
    assert again["ok"] is True and again["result"]["id"] == first["result"]["id"]
    assert len(live.snapshot()["nodes"]) == 1


def test_nc_r60_a_different_payload_is_still_refused_after_a_restart(live):
    base = live.plan_revision()
    live.create_raw({**NEW, "plan_revision": base}, request_id="req-K")
    live.restart(hard=True)
    reply = live.create_raw({**NEW, "task": "other", "plan_revision": base}, request_id="req-K")
    assert code(reply) == "request_id_reused"


def test_nc_r60_the_same_request_id_from_two_subjects_is_two_requests(delegating):
    live, node, token = delegating
    root_reply = live.create_raw({**NEW, "task": "root's"}, request_id="shared")
    run_reply = live.create_raw({**NEW, "task": "run's", "parent": node["id"]}, token,
                                request_id="shared")
    assert root_reply["ok"] is True and run_reply["ok"] is True, run_reply
    assert root_reply["result"]["id"] != run_reply["result"]["id"]
    assert run_reply["result"]["task"] == "run's"


def test_nc_r60_deduplication_follows_the_run_across_capability_renewal(delegating):
    live, node, token = delegating
    base = live.plan_revision()
    request = {**NEW, "parent": node["id"], "plan_revision": base}
    first = live.create_raw(request, token, request_id="renew")
    renewed = live.issue("run-1", node["id"], {"read", "delegate"})
    again = live.create_raw(request, renewed, request_id="renew")
    assert first["ok"] is True and again["ok"] is True, again
    assert again["result"]["id"] == first["result"]["id"]
    assert len([n for n in live.snapshot()["nodes"] if n["parent"] == node["id"]]) == 1


# ================================================================== NC-R9: identity

OPS = ["create_node", "update_node", "cancel_node", "get_node", "list_nodes",
       "wait_for_nodes", "ack_nodes", "scheduler_status", "register_template",
       "list_templates", "relaunch_node", "close_node", "merge_node"]


def args_for(op, node):
    return {"create_node": {**NEW, "plan_revision": 0},
            "update_node": {"id": node["id"], "revision": node["revision"], "task": "pwned"},
            "cancel_node": {"id": node["id"], "revision": node["revision"]},
            "get_node": {"id": node["id"]},
            "wait_for_nodes": {"timeout": 0.2, "cursor": 0},
            "ack_nodes": {"cursor": 10 ** 6},
            "register_template": {"yaml": "template: x\n"},
            "relaunch_node": {"id": node["id"], "revision": node["revision"]},
            "close_node": {"id": node["id"], "revision": node["revision"],
                           "outcome": "exhausted"},
            "merge_node": {"id": node["id"]}}.get(op, {})


@pytest.mark.parametrize("token", [ABSENT, None, "", "x", "0" * 64, 0, 7, ["a"], {"a": 1}, True])
def test_nc_r9_no_token_or_a_forged_one_is_unauthenticated_for_every_op(live, token):
    node = live.create(task="victim")
    before = live.snapshot()
    names = live.names()
    for op in OPS:
        reply = live.rpc(op, args_for(op, node), token=token)
        assert reply["ok"] is False and code(reply) == "unauthenticated", (op, reply)
    assert live.snapshot() == before
    assert live.names() == names


@pytest.mark.parametrize("mutate", [
    lambda t: t + "x", lambda t: t[:-1], lambda t: t[1:], lambda t: t.upper() if t.lower() == t
    else t.lower(), lambda t: t[::-1], lambda t: "A" * len(t), lambda t: t + t,
])
def test_nc_r9_a_near_miss_of_the_root_token_is_unauthenticated(live, mutate):
    token = live.root_token()
    forged = mutate(token)
    if forged == token:
        pytest.skip("mutation was the identity for this token")
    reply = live.rpc("scheduler_status", token=forged)
    assert reply["ok"] is False and code(reply) == "unauthenticated", reply


def test_nc_r9_a_token_in_args_is_not_a_token(live):
    reply = live.rpc("scheduler_status", {"token": live.root_token()}, token=ABSENT)
    assert reply["ok"] is False and code(reply) == "unauthenticated"


def test_nc_r9_a_missing_caller_id_never_means_root(live):
    node = live.create()
    for extra in ({"caller": None}, {"caller": ""}, {"run_id": None}):
        reply = live.rpc("cancel_node", {"id": node["id"], "revision": node["revision"], **extra},
                         token=ABSENT)
        assert code(reply) == "unauthenticated"
    assert live.get(node["id"])["state"] == "open"


def test_nc_r9_an_unauthenticated_wait_does_not_block_for_its_timeout(live):
    start = time.time()
    reply = live.rpc("wait_for_nodes", {"timeout": 5}, token="nope")
    assert code(reply) == "unauthenticated"
    assert time.time() - start < 3


def test_nc_r9_a_revoked_run_token_is_unauthenticated_everywhere(delegating):
    live, node, token = delegating
    assert live.rpc("get_node", {"id": node["id"]}, token)["ok"] is True
    live.revoke("run-1")
    for op in ("get_node", "list_nodes", "scheduler_status", "wait_for_nodes", "create_node"):
        args = {"get_node": {"id": node["id"]}, "wait_for_nodes": {"timeout": 0.2},
                "create_node": {**NEW, "parent": node["id"], "plan_revision": 0}}.get(op, {})
        reply = live.rpc(op, args, token)
        assert reply["ok"] is False and code(reply) == "unauthenticated", (op, reply)


def test_nc_r9_revoking_one_run_leaves_the_others_working(live):
    a = live.create(task="a")
    b = live.create(task="b")
    ta, tb = live.issue("run-a", a["id"]), live.issue("run-b", b["id"])
    live.revoke("run-a")
    assert code(live.rpc("get_node", {"id": a["id"]}, ta)) == "unauthenticated"
    assert live.rpc("get_node", {"id": b["id"]}, tb)["ok"] is True


def test_nc_r71_revoking_twice_or_an_unknown_run_is_harmless(live):
    node = live.create()
    live.issue("run-1", node["id"])
    live.revoke("run-1")
    live.revoke("run-1")
    live.revoke("never-issued")


@pytest.mark.parametrize("hard", [False, True])
def test_nc_r9_capabilities_persist_across_a_scheduler_restart(live, hard):
    node = live.create()
    live_token = live.issue("run-live", node["id"])
    dead_token = live.issue("run-dead", node["id"])
    live.revoke("run-dead")
    root = live.root_token()
    live.restart(hard=hard)
    assert live.root_token() == root
    assert live.rpc("get_node", {"id": node["id"]}, live_token)["ok"] is True
    assert code(live.rpc("get_node", {"id": node["id"]}, dead_token)) == "unauthenticated"
    assert live.rpc("scheduler_status", token=root)["ok"] is True


def test_nc_r9_a_run_token_is_long_random_and_distinct(live):
    node = live.create()
    tokens = [live.issue(f"run-{i}", node["id"]) for i in range(5)]
    assert all(isinstance(t, str) and len(t) >= 43 for t in tokens)      # >= 256 bits
    assert len(set(tokens)) == 5
    assert live.root_token() not in tokens


def test_nc_r9_the_registry_keeps_only_a_hash_of_a_run_token(live):
    node = live.create()
    token = live.issue("run-1", node["id"])
    live.restart()
    for path in files_under(live.state_dir):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), f"{path} holds the run token"
    live.revoke("run-1")
    for path in files_under(live.state_dir):
        if path.is_file():
            assert token.encode() not in path.read_bytes()


def test_nc_r9_the_root_token_is_stable_and_only_in_the_scheduler_dir(live):
    token = live.root_token()
    assert isinstance(token, str) and token and token == token.strip()
    holders = [p for p in files_under(live.state_dir)
               if p.is_file() and token.encode() in p.read_bytes()]
    assert holders, "the root capability is stored in the scheduler state dir"
    assert not [p for p in files_under(live.rpc_dir)
                if p.is_file() and token.encode() in p.read_bytes()]
    assert not any(token in v for v in os.environ.values())


def test_nc_r9_the_root_token_is_never_handed_to_a_client_in_a_reply(live):
    token = live.root_token()
    node = live.create()
    live.issue("run-1", node["id"])
    for op in ("scheduler_status", "list_nodes", "list_templates", "wait_for_nodes"):
        reply = live.rpc(op, {"timeout": 0.2} if op == "wait_for_nodes" else {})
        assert token not in json.dumps(reply)


@pytest.mark.parametrize("op", ["issue_run_capability", "revoke_run_capability",
                                "root_capability", "issue_capability", "revoke_capability"])
def test_nc_r71_the_capability_seams_are_not_reachable_over_the_rpc(live, op):
    node = live.create()
    reply = live.rpc(op, {"run_id": "run-x", "node_id": node["id"],
                          "permissions": ["read", "delegate"]})
    assert reply["ok"] is False
    # and no capability for run-x came into being
    assert code(live.rpc("get_node", {"id": node["id"]}, token="run-x")) == "unauthenticated"


def test_nc_r71_issue_returns_a_token_the_scheduler_accepts(live):
    node = live.create()
    token = live.issue("run-1", node["id"])
    reply = live.rpc("get_node", {"id": node["id"]}, token)
    assert reply["ok"] is True and reply["result"]["id"] == node["id"]


# ================================================================== NC-R9/R10: supplied ids ignored

def test_nc_r9_a_supplied_run_id_or_caller_does_not_change_who_the_run_is(delegating):
    live, node, token = delegating
    before = live.snapshot()
    reply = live.create_raw({**NEW, "parent": node["id"], "run_id": "run-OTHER",
                             "caller": "root", "created_by": "root"}, token)
    if reply.get("ok") is not True:             # refusing the unknown fields is allowed too
        assert live.snapshot() == before
        return
    made = live.get(reply["result"]["id"])
    assert "run-1" in str(made["created_by"])
    assert "run-OTHER" not in str(made["created_by"])
    assert "root" not in str(made["created_by"]).lower().replace("run-1", "")


def test_nc_r9_a_supplied_caller_cannot_widen_a_runs_scope(delegating):
    live, node, token = delegating
    outsider = live.create(task="not under the run")
    before = live.snapshot()
    reply = live.create_raw({**NEW, "parent": outsider["id"], "caller": "root",
                             "run_id": "root"}, token)
    assert reply["ok"] is False and code(reply) in ("forbidden", "invalid"), reply
    assert live.snapshot() == before


def test_nc_r9_a_run_naming_a_siblings_node_as_parent_is_forbidden(live):
    mine, theirs = live.create(task="mine"), live.create(task="sibling's")
    token = live.issue("run-1", mine["id"])
    live.issue("run-2", theirs["id"])
    before = live.snapshot()
    reply = live.create_raw({**NEW, "parent": theirs["id"]}, token)
    assert reply["ok"] is False and code(reply) == "forbidden", reply
    assert live.snapshot() == before


def test_nc_r10_a_run_cannot_create_a_top_level_node(delegating):
    live, node, token = delegating
    before = {n["id"] for n in live.snapshot()["nodes"]}
    reply = live.create_raw({**NEW, "parent": None}, token)
    reply2 = live.create_raw(dict(NEW), token)
    for r in (reply, reply2):
        if r.get("ok") is True:
            assert live.get(r["result"]["id"])["parent"] == node["id"]
        else:
            assert code(r) == "forbidden"
    after = live.snapshot()["nodes"]
    assert all(n["parent"] is not None for n in after if n["id"] not in before)


# ================================================================== NC-R10: scope

def test_nc_r10_a_run_creates_under_its_own_node_and_is_recorded_as_creator(delegating):
    live, node, token = delegating
    reply = live.create_raw({**NEW, "parent": node["id"]}, token)
    assert reply["ok"] is True, reply
    child = live.get(reply["result"]["id"])
    assert child["parent"] == node["id"]
    assert "run-1" in str(child["created_by"])
    assert live.get(node["id"])["children"] == [child["id"]]


def test_nc_r10_a_run_creates_under_a_descendant_it_created(delegating):
    live, node, token = delegating
    mid = live.create_raw({"kind": "sequence", "children": [], "parent": node["id"]}, token)
    if mid.get("ok") is not True:        # an empty composite may be refused; use a seeded one
        seed = live.create_raw({**NEW, "parent": node["id"]}, token)["result"]
        mid = live.create_raw({"kind": "group", "children": [seed["id"]],
                               "parent": node["id"]}, token)
    assert mid["ok"] is True, mid
    deep = live.create_raw({**NEW, "parent": mid["result"]["id"]}, token)
    assert deep["ok"] is True, deep


def test_nc_r10_a_run_cannot_adopt_a_node_outside_its_subtree_as_a_child(delegating):
    live, node, token = delegating
    outsider = live.create(task="not mine")
    before = live.snapshot()
    reply = live.create_raw({"kind": "sequence", "parent": node["id"],
                             "children": [outsider["id"]]}, token)
    assert reply["ok"] is False, reply
    assert live.snapshot() == before


def test_nc_r10_a_run_reads_its_own_subtree_only(delegating):
    live, node, token = delegating
    child = live.create_raw({**NEW, "parent": node["id"]}, token)["result"]
    outsider = live.create(task="elsewhere")
    assert live.rpc("get_node", {"id": node["id"]}, token)["ok"] is True
    assert live.rpc("get_node", {"id": child["id"]}, token)["ok"] is True
    reply = live.rpc("get_node", {"id": outsider["id"]}, token)
    assert reply["ok"] is False and code(reply) == "forbidden", reply
    listed = {n["id"] for n in live.ok("list_nodes", {}, token)["nodes"]}
    assert child["id"] in listed and outsider["id"] not in listed


def test_nc_r10_a_run_edits_and_cancels_what_it_created(delegating):
    live, node, token = delegating
    child = live.create_raw({**NEW, "parent": node["id"]}, token)["result"]
    edited = live.rpc("update_node", {"id": child["id"], "revision": child["revision"],
                                      "task": "retargeted", "urgent": True}, token)
    assert edited["ok"] is True, edited
    assert live.get(child["id"])["task"] == "retargeted"
    cancelled = live.rpc("cancel_node", {"id": child["id"],
                                         "revision": edited["result"]["revision"]}, token)
    assert cancelled["ok"] is True and live.get(child["id"])["state"] == "cancelled"


def test_nc_r59_a_run_may_not_edit_or_cancel_its_own_node(delegating):
    live, node, token = delegating
    before = live.snapshot()
    edit = live.rpc("update_node", {"id": node["id"], "revision": node["revision"],
                                    "task": "rewrite myself"}, token)
    cancel = live.rpc("cancel_node", {"id": node["id"], "revision": node["revision"]}, token)
    assert code(edit) == "forbidden" and code(cancel) == "forbidden", (edit, cancel)
    assert live.snapshot() == before


def test_nc_r59_a_run_may_not_edit_or_cancel_a_node_root_put_under_it(delegating):
    live, node, token = delegating
    rooted = live.create(parent=node["id"], task="root's child")
    before = live.snapshot()
    edit = live.rpc("update_node", {"id": rooted["id"], "revision": rooted["revision"],
                                    "task": "mine now"}, token)
    cancel = live.rpc("cancel_node", {"id": rooted["id"], "revision": rooted["revision"]}, token)
    assert code(edit) == "forbidden" and code(cancel) == "forbidden", (edit, cancel)
    assert live.snapshot() == before


def test_nc_r10_a_run_cannot_edit_or_cancel_outside_its_subtree(delegating):
    live, node, token = delegating
    outsider = live.create(task="elsewhere")
    before = live.snapshot()
    edit = live.rpc("update_node", {"id": outsider["id"], "revision": outsider["revision"],
                                    "task": "x"}, token)
    cancel = live.rpc("cancel_node", {"id": outsider["id"], "revision": outsider["revision"]},
                      token)
    assert code(edit) == "forbidden" and code(cancel) == "forbidden"
    assert live.snapshot() == before


def test_nc_r59_a_run_may_edit_a_node_it_created_only_while_it_is_open(delegating):
    live, node, token = delegating
    child = live.create_raw({**NEW, "parent": node["id"]}, token)["result"]
    cancelled = live.rpc("cancel_node", {"id": child["id"], "revision": child["revision"]},
                         token)["result"]
    before = live.snapshot()
    reply = live.rpc("update_node", {"id": child["id"], "revision": cancelled["revision"],
                                     "task": "zombie"}, token)
    assert reply["ok"] is False
    assert live.snapshot() == before


@pytest.mark.parametrize("op", ["relaunch_node", "close_node", "merge_node", "register_template",
                                "ack_nodes"])
def test_nc_r10_a_run_may_not_use_the_roots_operations(delegating, op):
    live, node, token = delegating
    before = live.snapshot()
    reply = live.rpc(op, args_for(op, node), token)
    assert reply["ok"] is False and code(reply) == "forbidden", reply
    assert live.snapshot() == before


def test_nc_r10_a_run_cannot_move_roots_acknowledged_cursor(delegating):
    live, node, token = delegating
    nxt = live.ok("wait_for_nodes", {"timeout": 0.5})["next_cursor"]
    reply = live.rpc("ack_nodes", {"cursor": nxt}, token)
    assert code(reply) == "forbidden"
    assert live.ok("wait_for_nodes", {"timeout": 0.5})["transitions"], \
        "nothing was acknowledged, so nothing may disappear for root"


def test_nc_r10_root_may_act_on_every_node_including_a_runs(delegating):
    live, node, token = delegating
    child = live.create_raw({**NEW, "parent": node["id"]}, token)["result"]
    assert live.update_raw(child["id"], child["revision"], task="root edit")["ok"] is True
    assert live.cancel_raw(child["id"])["ok"] is True
    assert live.update_raw(node["id"], None, task="root edits the run's own node")["ok"] is True


def test_nc_r13_a_run_may_ask_for_scheduler_status(delegating):
    live, node, token = delegating
    reply = live.rpc("scheduler_status", {}, token)
    assert reply["ok"] is True and isinstance(reply["result"].get("pid"), int)


# ================================================================== NC-R58: permissions

def test_nc_r58_read_is_always_granted_even_with_no_permissions(live):
    node = live.create()
    token = live.issue("run-ro", node["id"], set())
    assert live.rpc("get_node", {"id": node["id"]}, token)["ok"] is True
    assert live.rpc("list_nodes", {}, token)["ok"] is True
    assert live.rpc("wait_for_nodes", {"timeout": 0.2}, token)["ok"] is True
    assert live.rpc("scheduler_status", {}, token)["ok"] is True


def test_nc_r58_without_delegate_a_run_creates_nothing(live):
    node = live.create()
    token = live.issue("run-ro", node["id"], {"read"})
    before = live.snapshot()
    reply = live.create_raw({**NEW, "parent": node["id"]}, token)
    assert reply["ok"] is False and code(reply) == "forbidden", reply
    assert live.snapshot() == before


def test_nc_r58_with_delegate_a_run_creates(live):
    node = live.create()
    token = live.issue("run-d", node["id"], {"read", "delegate"})
    assert live.create_raw({**NEW, "parent": node["id"]}, token)["ok"] is True


def test_nc_r58_a_run_without_the_verdict_permission_cannot_give_one(live):
    node = live.create()
    token = live.issue("run-d", node["id"], {"read", "delegate"})
    before = live.snapshot()
    reply = live.rpc("give_verdict", {"generation_seq": 1, "verdict": "approved",
                                      "findings": []}, token)
    assert reply["ok"] is False and code(reply) == "forbidden", reply
    assert live.snapshot() == before


def test_nc_r58_root_cannot_give_a_verdict(live):
    node = live.create()
    before = live.snapshot()
    reply = live.rpc("give_verdict", {"generation_seq": 1, "verdict": "approved",
                                      "findings": []})
    assert reply["ok"] is False
    assert live.snapshot() == before


def test_nc_r58_a_run_with_only_read_still_cannot_use_root_operations(live):
    node = live.create()
    token = live.issue("run-ro", node["id"], {"read"})
    reply = live.rpc("close_node", {"id": node["id"], "revision": node["revision"],
                                    "outcome": "failed"}, token)
    assert code(reply) == "forbidden"


# ================================================================== NC-R13: reads

def test_nc_r13_get_node_returns_the_node_and_an_unknown_id_is_an_error(live):
    node = live.create(task="find me")
    assert live.get(node["id"])["task"] == "find me"
    reply = live.rpc("get_node", {"id": "nd-00000000"})
    assert reply["ok"] is False


def test_nc_r13_list_nodes_lists_everything_and_the_plan_revision(live):
    ids = {live.create(task=f"t{i}")["id"] for i in range(3)}
    result = live.ok("list_nodes", {})
    assert {n["id"] for n in result["nodes"]} == ids
    assert isinstance(result["plan_revision"], int)


def test_nc_r13_list_nodes_filters_by_state(live):
    keep, gone = live.create(task="keep"), live.create(task="gone")
    live.cancel_raw(gone["id"])
    open_ = {n["id"] for n in live.ok("list_nodes", {"state": "open"})["nodes"]}
    cancelled = {n["id"] for n in live.ok("list_nodes", {"state": "cancelled"})["nodes"]}
    assert open_ == {keep["id"]} and cancelled == {gone["id"]}


def test_nc_r13_list_nodes_filters_by_parent(live):
    a, b, c = (live.create(task=t) for t in "abc")
    comp = live.create(kind="sequence", children=[a["id"], b["id"]])
    got = {n["id"] for n in live.ok("list_nodes", {"parent": comp["id"]})["nodes"]}
    assert got == {a["id"], b["id"]}
    assert c["id"] not in got


def test_nc_r13_list_nodes_on_an_empty_plan(live):
    result = live.ok("list_nodes", {})
    assert result["nodes"] == [] and isinstance(result["plan_revision"], int)


def test_nc_r13_scheduler_status_names_the_live_pid_and_counts_nodes_by_state(live):
    pid = live.ok("scheduler_status", {})["pid"]
    assert isinstance(pid, int) and alive(pid)
    a, b, c = (live.create(task=t) for t in "abc")
    live.cancel_raw(c["id"])
    status = live.ok("scheduler_status", {})
    assert find_count(status, "open") == 2
    assert find_count(status, "cancelled") == 1


def test_nc_r13_scheduler_status_reports_a_new_pid_after_a_restart(live):
    first = live.pid()
    live.restart(hard=True)
    second = live.pid()
    assert second != first and alive(second) and not alive(first)
