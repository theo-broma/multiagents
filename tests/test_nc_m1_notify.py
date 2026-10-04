"""NC-R14, NC-R15 (milestone M1): the durable notification log, its mirror in
`.multiagents/events.jsonl`, and `wait_for_nodes` / `ack_nodes`.

Only the transitions M1 can produce are exercised: `created`, `updated`,
`cancelled`, `scheduler_started`, `scheduler_stopped`.

Assumptions (also in the run report):
  * a transition is an object with an integer `seq`, its name under one of
    transition/kind/type/event/name, and its node id under node/node_id/id;
  * `wait_for_nodes(cursor=k)` returns the transitions with seq > k and moves
    nothing; `timeout` is in seconds and may be fractional;
  * the scheduler's own start and stop are in the log (NC-R14 lists them).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import ROOT, code, live, nc, tname, tnode  # noqa: E402,F401

NEW = {"kind": "simple", "agent": "worker", "task": "t"}


def seqs(transitions):
    return [t["seq"] for t in transitions]


def wait(live, **args):
    return live.ok("wait_for_nodes", {"timeout": 0.5, **args})


def eventually(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ================================================================== NC-R14

def test_nc_r14_starting_the_scheduler_is_the_first_transition(live):
    assert live.names()[:1] == ["scheduler_started"]


def test_nc_r14_each_write_appears_once_in_order_with_its_node(live):
    a = live.create(task="a")
    b = live.create(task="b")
    live.update(a["id"], task="a2")
    live.ok("cancel_node", {"id": b["id"], "revision": b["revision"]})
    transitions = live.transitions()
    assert [tname(t) for t in transitions] == [
        "scheduler_started", "created", "created", "updated", "cancelled"]
    assert [tnode(t) for t in transitions[1:]] == [a["id"], b["id"], a["id"], b["id"]]


def test_nc_r14_seq_is_strictly_increasing_and_unique(live):
    for i in range(5):
        live.create(task=f"t{i}")
    got = seqs(live.transitions())
    assert got == sorted(set(got)) and len(got) == 6
    assert all(isinstance(s, int) and not isinstance(s, bool) for s in got)


def test_nc_r14_every_transition_is_mirrored_in_events_jsonl_once_and_in_order(live):
    a = live.create(task="a")
    live.update(a["id"], task="a2")
    live.ok("cancel_node", {"id": a["id"], "revision": a["revision"] + 1})
    expected = [tname(t) for t in live.transitions()]
    assert expected == ["scheduler_started", "created", "updated", "cancelled"]
    assert eventually(lambda: live.event_kinds() == expected), live.event_kinds()


def test_nc_r14_events_use_the_node_dot_kind_spelling(live):
    live.create()
    assert eventually(lambda: "created" in live.event_kinds())
    raw = live.events.read_text()
    assert '"node.created"' in raw and '"node.scheduler_started"' in raw


def test_nc_r14_a_refused_write_records_nothing_anywhere(live):
    live.create_raw({**NEW, "agent": "ghost"})                       # invalid
    node = live.create()
    live.update_raw(node["id"], node["revision"] + 9, task="x")      # conflict
    live.rpc("get_node", {"id": node["id"]}, token="forged")         # unauthenticated
    assert live.names() == ["scheduler_started", "created"]
    assert eventually(lambda: live.event_kinds() == ["scheduler_started", "created"])


def test_nc_r14_reads_record_nothing(live):
    live.create()
    for _ in range(3):
        live.ok("list_nodes", {})
        live.ok("scheduler_status", {})
    assert live.names() == ["scheduler_started", "created"]


def test_nc_r14_a_clean_stop_is_recorded_and_the_next_start_follows_it(live):
    live.create()
    live.restart()
    assert live.names() == ["scheduler_started", "created", "scheduler_stopped",
                            "scheduler_started"]


def test_nc_r14_a_killed_scheduler_records_no_stop(live):
    live.create()
    live.restart(hard=True)
    names = live.names()
    assert "scheduler_stopped" not in names
    assert names == ["scheduler_started", "created", "scheduler_started"]


def test_nc_r14_seq_keeps_climbing_across_restarts(live):
    live.create()
    before = max(seqs(live.transitions()))
    live.restart(hard=True)
    live.create()
    after = seqs(live.transitions())
    assert after == sorted(set(after))
    assert max(after) > before
    assert all(s > before for s in after[after.index(before) + 1:])


def test_nc_r14_the_log_survives_a_restart_whole(live):
    for i in range(3):
        live.create(task=f"t{i}")
    first = live.transitions()
    live.restart(hard=True)
    again = live.transitions()
    assert again[:len(first)] == first


# ================================================================== NC-R15

def test_nc_r15_wait_returns_the_documented_shape(live):
    live.create()
    result = wait(live)
    assert set(result) >= {"transitions", "next_cursor", "capacity", "scheduler"}
    assert isinstance(result["next_cursor"], int)
    assert result["next_cursor"] >= max(seqs(result["transitions"]))


def test_nc_r15_transitions_made_while_nobody_waited_are_all_returned(live):
    ids = [live.create(task=f"t{i}")["id"] for i in range(4)]
    result = wait(live)
    assert [tnode(t) for t in result["transitions"] if tname(t) == "created"] == ids


def test_nc_r15_unacked_transitions_are_redelivered(live):
    live.create()
    first = wait(live)
    second = wait(live)
    assert second["transitions"] == first["transitions"]
    assert second["next_cursor"] == first["next_cursor"]


def test_nc_r15_acked_transitions_are_not_delivered_again(live):
    live.create()
    first = wait(live)
    live.ok("ack_nodes", {"cursor": first["next_cursor"]})
    assert wait(live, timeout=0.3)["transitions"] == []
    live.create(task="later")
    later = wait(live)
    assert [tname(t) for t in later["transitions"]] == ["created"]


def test_nc_r15_ack_is_partial_up_to_the_cursor_given(live):
    live.create(task="a")
    live.create(task="b")
    all_ = wait(live)["transitions"]
    keep = all_[-1]["seq"]
    live.ok("ack_nodes", {"cursor": all_[-2]["seq"]})
    assert seqs(wait(live)["transitions"]) == [keep]


def test_nc_r15_ack_never_moves_backwards(live):
    live.create(task="a")
    live.create(task="b")
    top = wait(live)["next_cursor"]
    live.ok("ack_nodes", {"cursor": top})
    live.rpc("ack_nodes", {"cursor": 1})          # refused or a no-op: both are allowed
    assert wait(live, timeout=0.3)["transitions"] == []


@pytest.mark.parametrize("bad", ["x", None, 1.5, [1], {"a": 1}])
def test_nc_r15_a_malformed_cursor_is_refused_and_acknowledges_nothing(live, bad):
    live.create()
    before = wait(live)["transitions"]
    reply = live.rpc("ack_nodes", {"cursor": bad})
    assert reply["ok"] is False, reply
    assert wait(live)["transitions"] == before


def test_nc_r15_acking_twice_is_idempotent(live):
    live.create()
    top = wait(live)["next_cursor"]
    live.ok("ack_nodes", {"cursor": top})
    live.ok("ack_nodes", {"cursor": top})
    assert wait(live, timeout=0.3)["transitions"] == []


@pytest.mark.parametrize("hard", [False, True])
def test_nc_r15_the_cursor_is_durable_across_a_scheduler_restart(live, hard):
    live.create(task="seen")
    live.ok("ack_nodes", {"cursor": wait(live)["next_cursor"]})
    live.create(task="unseen")
    live.restart(hard=hard)
    names = [tname(t) for t in wait(live)["transitions"]]
    assert names.count("created") == 1                    # only "unseen" is redelivered
    assert "scheduler_started" in names


def test_nc_r15_a_reconnecting_orchestrator_loses_nothing(live):
    ids = [live.create(task=f"t{i}")["id"] for i in range(3)]
    live.restart(hard=True)            # the orchestrator was not connected for any of it
    created = [tnode(t) for t in wait(live)["transitions"] if tname(t) == "created"]
    assert created == ids


def test_nc_r15_wait_blocks_up_to_the_timeout_when_there_is_nothing(live):
    live.ok("ack_nodes", {"cursor": wait(live)["next_cursor"]})
    start = time.time()
    result = live.ok("wait_for_nodes", {"timeout": 2})
    elapsed = time.time() - start
    assert result["transitions"] == []
    assert 1.5 <= elapsed < 6, elapsed


def test_nc_r15_wait_returns_as_soon_as_a_transition_arrives(live):
    live.ok("ack_nodes", {"cursor": wait(live)["next_cursor"]})
    threading.Timer(0.6, lambda: live.create(task="wakes the waiter")).start()
    start = time.time()
    result = live.ok("wait_for_nodes", {"timeout": 20})
    assert time.time() - start < 10
    assert [tname(t) for t in result["transitions"]] == ["created"]


def test_nc_r15_wait_with_something_pending_does_not_block(live):
    live.create()
    start = time.time()
    live.ok("wait_for_nodes", {"timeout": 20})
    assert time.time() - start < 5


def test_nc_r15_an_explicit_cursor_reads_past_the_acknowledged_point_and_moves_nothing(live):
    live.create(task="a")
    top = wait(live)["next_cursor"]
    live.ok("ack_nodes", {"cursor": top})
    everything = live.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.3})["transitions"]
    assert [tname(t) for t in everything] == ["scheduler_started", "created"]
    assert wait(live, timeout=0.3)["transitions"] == []


def test_nc_r15_an_explicit_cursor_returns_only_what_follows_it(live):
    live.create(task="a")
    live.create(task="b")
    all_ = live.transitions()
    got = live.ok("wait_for_nodes", {"cursor": all_[1]["seq"], "timeout": 0.3})["transitions"]
    assert seqs(got) == seqs(all_[2:])


def test_nc_r15_node_ids_narrow_the_transitions_to_those_nodes(live):
    a, b = live.create(task="a"), live.create(task="b")
    live.update(a["id"], task="a2")
    got = live.ok("wait_for_nodes", {"cursor": 0, "node_ids": [a["id"]],
                                     "timeout": 0.3})["transitions"]
    assert [tname(t) for t in got] == ["created", "updated"]
    assert {tnode(t) for t in got} == {a["id"]}


def test_nc_r15_a_runs_wait_is_scoped_to_its_subtree_and_leaves_roots_cursor_alone(live):
    mine = live.create(task="the run's node")
    outsider = live.create(task="elsewhere")
    token = live.issue("run-1", mine["id"])
    child = live.create(token=token, parent=mine["id"], task="child")
    live.update(outsider["id"], task="changed")
    got = live.ok("wait_for_nodes", {"cursor": 0, "timeout": 0.3}, token)["transitions"]
    nodes = {tnode(t) for t in got}
    assert child["id"] in nodes
    assert outsider["id"] not in nodes
    # the run's wait did not acknowledge anything for root
    root_view = wait(live)["transitions"]
    assert outsider["id"] in {tnode(t) for t in root_view}


def test_nc_r15_a_runs_wait_does_not_block_forever_on_an_empty_subtree(live):
    node = live.create()
    token = live.issue("run-1", node["id"])
    start = time.time()
    live.ok("wait_for_nodes", {"cursor": 10 ** 6, "timeout": 1}, token)
    assert time.time() - start < 6


def test_nc_r15_nothing_waits_for_an_ack(live):
    live.create(task="a")
    wait(live)
    start = time.time()
    for i in range(3):
        live.create(task=f"more{i}")
    assert time.time() - start < 15          # writes proceed with un-acked transitions pending
    assert len(live.snapshot()["nodes"]) == 4
