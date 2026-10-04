"""Adversary (M1, NC-R9/R10/R59/R75/R79): a run capability's effects stay
inside its own subtree, for every op, whatever extra args it supplies.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from nc_harness import code, live, nc  # noqa: E402,F401


def _delegating(live, run_id="run-1", perms=frozenset({"read", "delegate"})):
    node = live.create(task="the delegating run's node")
    return node, live.issue(run_id, node["id"], set(perms))


def test_adv_run_cannot_detach_its_child_to_the_top_level_of_the_plan(live):
    """NC-R10: a run creates nodes whose parent is its own node or a descendant.
    Removing a child from a composite it created must not turn that child into
    a parentless, top-level node that the run created."""
    own, token = _delegating(live)
    group = live.create(token, kind="group")
    child = live.create(token, parent=group["id"], task="delegated work")
    assert live.get(child["id"])["parent"] == group["id"]

    reply = live.update_raw(group["id"], token=token, children=[])

    stray = live.get(child["id"])           # read as root
    top_level_by_run = [n for n in live.ok("list_nodes", {})["nodes"]
                        if n["parent"] is None and n["created_by"] == "run-1"]
    assert not top_level_by_run, (
        f"update_node({group['id']}, children=[]) by the run replied {reply} and left "
        f"{stray['id']} with parent={stray['parent']!r}, created_by={stray['created_by']!r}")


def test_adv_root_cancelling_a_runs_node_reaches_everything_the_run_delegated(live):
    """NC-R80: cancelling a node cancels its non-terminal descendants. Work a
    run delegated must not survive root cancelling that run's node, which is
    what happens once the run has detached it."""
    own, token = _delegating(live)
    group = live.create(token, kind="group")
    child = live.create(token, parent=group["id"], task="delegated work")
    live.update_raw(group["id"], token=token, children=[])
    reply = live.cancel_raw(own["id"])
    assert reply.get("ok") is True, reply
    survivor = live.get(child["id"])
    assert survivor["state"] == "cancelled", (
        f"root cancelled {own['id']} but {child['id']} (created_by="
        f"{survivor['created_by']}) is still {survivor['state']} with parent={survivor['parent']!r}")


def test_adv_give_verdict_authorises_the_node_id_it_names(live):
    """NC-R75: authorise before not_implemented. A run with `verdict` naming a
    node outside its subtree in `node_id` must be `forbidden`, even when an
    in-scope `id` is also supplied."""
    outside = live.create(task="someone else's node")
    own, token = _delegating(live, "rev-1", {"read", "verdict"})
    reply = live.rpc("give_verdict", {"id": own["id"], "node_id": outside["id"],
                                      "generation_seq": 1, "commit": "0" * 40,
                                      "verdict": "approved"}, token)
    assert code(reply) == "forbidden", reply


def test_adv_get_node_scope_checks_the_field_it_reads(live):
    """Scope must be checked on the id actually served."""
    outside = live.create(task="secret task text")
    own, token = _delegating(live)
    reply = live.rpc("get_node", {"node_id": own["id"], "id": outside["id"]}, token)
    assert code(reply) == "forbidden", reply
    assert "secret task text" not in str(reply)
