"""NC-R99 — `list_nodes` returns nodes in creation order, and that order is
stable: updating a node (state, revision, any field) never moves it; nodes
created later always come after nodes created earlier; a scoped call returns
the same relative order restricted to its subtree.

Black box: only the NC-R8 socket and the NC fixtures. "Creation order" is the
order of the `create_node` calls, which the tests make themselves (ids are
never assumed to sort). Nodes are kept un-launched by naming a lock held by a
gated running node, so a node can be edited while `ready`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.agent import task  # noqa: E402
from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import unwrap  # noqa: E402
from test_nc_m6_acceptance import bounded_waits, run_token  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = bounded_waits(GitWorld(tmp_path, monkeypatch))
    world.start_scheduler()
    yield world
    world.close()


def ids(world, token="root"):
    return [n["id"] for n in world.list(token)]


def waiting(world, n=4):
    """`holder` plus n nodes queued behind its lock; ids in creation order."""
    holder = world.hold_lock("L")
    return [holder] + [world.simple(f"Q{i}", locks=["L"]) for i in range(n)]


def test_nc_r99_nodes_come_back_in_creation_order(w):
    made = waiting(w, 5)
    assert ids(w) == made


def test_nc_r99_the_order_does_not_follow_the_id_or_the_task_text(w):
    holder = w.hold_lock("L")
    # tasks created in reverse alphabetical order
    made = [holder] + [w.simple(tag, locks=["L"]) for tag in ("ZZ", "MM", "AA")]
    assert ids(w) == made


def test_nc_r99_listing_twice_gives_the_same_order(w):
    waiting(w, 3)
    assert ids(w) == ids(w)


def test_nc_r99_an_update_to_an_early_node_leaves_it_in_place(w):
    made = waiting(w, 4)
    target = made[1]
    node = w.get(target)
    reply = w.rpc("update_node", {"id": target, "revision": node["revision"],
                                  "task": task("EDITED"), "urgent": True})
    assert reply.get("ok"), reply
    assert w.get(target)["revision"] > node["revision"]
    assert ids(w) == made


def test_nc_r99_repeated_updates_to_the_first_node_never_move_it(w):
    made = waiting(w, 3)
    target = made[1]
    for i in range(3):
        node = w.get(target)
        reply = w.rpc("update_node", {"id": target, "revision": node["revision"],
                                      "task": task(f"EDIT{i}")})
        assert reply.get("ok"), reply
        assert ids(w) == made


def test_nc_r99_cancelling_a_node_leaves_it_in_place(w):
    made = waiting(w, 4)
    assert w.cancel(made[2])["ok"]
    assert w.state(made[2]) == "cancelled"
    assert ids(w) == made


def test_nc_r99_a_state_change_leaves_the_node_in_place(w):
    holder = w.hold_lock("L")
    queued = [w.simple(f"Q{i}", locks=["L"], fx={"gate": f"q{i}"}) for i in range(3)]
    made = [holder] + queued
    assert ids(w) == made
    # the holder finishes (running -> done) and the first queued node launches
    w.gate("holder")
    w.wait_state(holder, "done")
    w.wait_running(queued[0])
    assert ids(w) == made
    w.gate("q0")
    w.wait_state(queued[0], "done")
    w.wait_running(queued[1])
    assert ids(w) == made


def test_nc_r99_nodes_created_after_updates_come_last(w):
    made = waiting(w, 3)
    node = w.get(made[1])
    assert w.rpc("update_node", {"id": made[1], "revision": node["revision"],
                                 "task": task("EDITED")}).get("ok")
    assert w.cancel(made[2])["ok"]
    later = [w.simple(f"LATE{i}", locks=["L"]) for i in range(2)]
    assert ids(w) == made + later
    # and updating an early node after the new ones exist still moves nothing
    node = w.get(made[3])
    assert w.rpc("update_node", {"id": made[3], "revision": node["revision"],
                                 "task": task("EDITED2")}).get("ok")
    assert ids(w) == made + later


def test_nc_r99_the_order_survives_a_scheduler_restart(w):
    made = waiting(w, 3)
    node = w.get(made[1])
    assert w.rpc("update_node", {"id": made[1], "revision": node["revision"],
                                 "task": task("EDITED")}).get("ok")
    w.restart_scheduler()
    assert ids(w) == made


def test_nc_r99_a_run_scoped_list_keeps_the_relative_order_of_its_subtree(w):
    parent = w.simple("DELEG", "spawner", fx={"gate": "g"})
    other = [w.simple("O0", fx={"gate": "o0"})]
    w.wait_running(parent)
    token = run_token(w.wait_spawn("DELEG"))
    kids = []
    for i in range(3):
        reply = w.create({"kind": "simple", "agent": "worker", "task": task(f"K{i}", fx={"gate": f"k{i}"}),
                          "parent": parent}, token)
        assert reply.get("ok"), reply
        kids.append(unwrap(reply["result"])["id"])
        other.append(w.simple(f"O{i + 1}", fx={"gate": f"o{i + 1}"}))   # interleaved
    scoped = ids(w, token)
    assert [i for i in scoped if i in kids] == kids
    full = ids(w)
    assert full.index(parent) < full.index(other[0]) < full.index(kids[0])
    assert [i for i in full if i in set(scoped)] == scoped

    # updates (to the first child, by the run; to the others, by state change)
    # do not reorder the scoped list or the full one
    first = w.get(kids[0], token)
    edit = w.rpc("update_node", {"id": kids[0], "revision": first["revision"],
                                 "task": task("K0b", fx={"gate": "k0"})}, token)
    assert edit.get("ok"), edit
    w.wait_running(kids[1])
    w.gate("k1")
    w.wait_state(kids[1], "done")
    assert ids(w, token) == scoped
    assert [i for i in ids(w) if i in set(scoped)] == scoped
    assert ids(w) == full


def test_nc_r99_a_state_filtered_list_keeps_creation_order(w):
    made = waiting(w, 4)
    assert w.cancel(made[1])["ok"]
    assert w.cancel(made[3])["ok"]
    got = [n["id"] for n in w.list(state="cancelled")]
    assert got == [made[1], made[3]]
