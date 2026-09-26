"""tree.json written by a newer multiagents must stay readable by an older one.

Incident: a newer server process added `adopted_at` to node records; an older
server process sharing the same tree.json then crashed on every read with
``TypeError: Node.__init__() got an unexpected keyword argument 'adopted_at'``.
Several processes of different versions share one tree.json (nested servers,
a long-lived orchestrator server across an upgrade), so a node record carrying
a field this version does not know is normal, not corruption.

Contract pinned here, through the public `Tree` API that runner.py and
server.py use (`Tree(tree_file, events_file)` then `get` / `active` /
`drivers` / `children_of` / `unseen`, and writes via `set_status` / `update` /
`add`):

- reading a node with unknown fields does not raise,
- every known field keeps its stored value,
- the node is not dropped,
- a subsequent write by this version does not raise, and the node still
  reads back afterwards.

Whether unknown fields survive a save is deliberately NOT asserted either way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents.tree import Node, Tree   # noqa: E402

UNKNOWN = {"future_field": 1, "future_dict": {"a": [1, 2]}, "future_none": None}


def _record(**over) -> dict:
    base = {
        "id": "ag-root01", "agent": "implementer", "provider": "claude",
        "model": "opus", "parent": None, "depth": 1, "task": "do the thing",
        "status": "running", "reason": "", "branch": "agents/impl/root01",
        "worktree": "/tmp/wt/root01", "session_id": "sess-123",
        "children": [], "steps": 7, "events": 11, "session": "S1",
        "summary": "halfway", "budget_tag": "phase-a",
    }
    base.update(over)
    return base


def _write_tree(tmp_path: Path, nodes: dict[str, dict]) -> Tree:
    tree_file = tmp_path / "tree.json"
    tree_file.write_text(json.dumps({
        "version": 1, "nodes": nodes, "deferred": [], "cooldowns": {},
        "pause": {}, "provider_health": {}, "questions": [], "tickets": [],
    }))
    return Tree(tree_file, tmp_path / "events.jsonl")


def _assert_known_fields(node: Node | None, record: dict) -> None:
    assert node is not None, "the node with unknown fields was lost"
    for key, value in record.items():
        if key in UNKNOWN:
            continue
        assert getattr(node, key) == value, f"{key} changed on load"


def test_get_reads_a_node_carrying_unknown_fields(tmp_path):
    rec = _record(**UNKNOWN)
    tree = _write_tree(tmp_path, {rec["id"]: rec})
    _assert_known_fields(tree.get(rec["id"]), rec)


def test_the_incident_field_shape_is_readable(tmp_path):
    # The exact shape of the incident: one extra float-valued timestamp field.
    rec = _record(adopted_at_v99=1758000000.5)
    tree = _write_tree(tmp_path, {rec["id"]: rec})
    node = tree.get(rec["id"])
    assert node is not None and node.session_id == "sess-123"


def test_list_readers_keep_nodes_with_unknown_fields(tmp_path):
    root = _record(children=["ag-kid001"], **UNKNOWN)
    kid = _record(id="ag-kid001", parent="ag-root01", depth=2,
                  branch="agents/impl/kid001", session_id="sess-kid",
                  status="done", unseen=True, **UNKNOWN)
    driver = _record(id="ag-drv001", role="orchestrator", branch="",
                     worktree="", session_id="sess-drv", **UNKNOWN)
    tree = _write_tree(tmp_path, {r["id"]: r for r in (root, kid, driver)})

    active = {n.id for n in tree.active()}
    assert "ag-root01" in active

    kids = tree.children_of("ag-root01")
    assert [n.id for n in kids] == ["ag-kid001"]
    _assert_known_fields(kids[0], kid)

    assert [n.id for n in tree.drivers()] == ["ag-drv001"]
    _assert_known_fields(tree.drivers()[0], driver)


def test_unknown_fields_on_one_node_do_not_hide_the_others(tmp_path):
    plain = _record(id="ag-plain1", session_id="sess-plain")
    odd = _record(id="ag-odd001", session_id="sess-odd", **UNKNOWN)
    tree = _write_tree(tmp_path, {plain["id"]: plain, odd["id"]: odd})
    assert {n.id for n in tree.active()} == {"ag-plain1", "ag-odd001"}
    _assert_known_fields(tree.get("ag-plain1"), plain)
    _assert_known_fields(tree.get("ag-odd001"), odd)


@pytest.mark.parametrize("write", [
    lambda t: t.set_status("ag-root01", "done"),
    lambda t: t.update("ag-root01", summary="finished"),
    lambda t: t.note_event("ag-root01", steps=9),
    lambda t: t.add(Node(id="ag-new001", agent="tester", provider="claude",
                         model="opus", parent="ag-root01", depth=2)),
], ids=["set_status", "update", "note_event", "add_child"])
def test_a_save_after_loading_unknown_fields_does_not_crash(tmp_path, write):
    rec = _record(**UNKNOWN)
    tree = _write_tree(tmp_path, {rec["id"]: rec})
    write(tree)
    node = tree.get(rec["id"])
    assert node is not None
    assert node.session_id == "sess-123" and node.branch == "agents/impl/root01"
    # And a fresh Tree over the same file (another process) reads it too.
    again = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl").get(rec["id"])
    assert again is not None and again.session_id == "sess-123"
