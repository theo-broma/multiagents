"""`_qh_nodes` parses `tree.json` once, whatever the node count.

The cost being pinned here was measured by the user on a live tree of 938
nodes, 4.3 MB: one `_qh_nodes` call cost 18.65s of CPU against 0.018s for a
single read, and it runs on the adoption pass every five seconds, so two
servers burned a core each. The cause was the shape of the call — one
`tree.read()`, then `tree.get(key)` for every key, and each of those re-reads
and re-parses the whole file.

What is pinned, then:

* the number of `Tree.read` calls is one, at 1 node and at 40 — the count is a
  property of the implementation, not of the tree's size, so a reintroduction
  is caught at the smallest size it can be;
* the nodes themselves are unchanged, in file order, and a falsy or malformed
  entry is skipped rather than raised.

`_qh_nodes` is exercised through the production `QuotaHandover._qh_nodes`
bound to an object holding nothing but a `Tree`: the method reads `self.tree`
and nothing else, so a full `Runner` would add a config and a budget reader
without adding any of the behaviour under test.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents.paths import ProjectPaths                    # noqa: E402
from multiagents.quota_handover import QuotaHandover           # noqa: E402
from multiagents.tree import Node, Tree                        # noqa: E402


class _Handover:
    """`_qh_nodes` and the tree it reads; nothing else is needed to call it."""

    _qh_nodes = QuotaHandover._qh_nodes

    def __init__(self, tree: Tree):
        self.tree = tree


def _project(tmp_path):
    paths = ProjectPaths(tmp_path / "project")
    paths.ensure()
    return Tree(paths.tree_file, paths.events_file)


def _entry(agent_id: str, status: str = "running", **fields) -> dict:
    return {**asdict(Node(id=agent_id, agent="worker", provider="alpha", model="model-a",
                          parent="", depth=0, status=status)), **fields}


def _write(tree: Tree, nodes: dict) -> None:
    """`tree.json` verbatim, so the entries keep the order they are given in.

    A transaction would sort the keys on the way out (`sort_keys=True`), which
    would make "the same order" untestable: a sorted tree cannot tell a
    faithful implementation from one that sorts for itself.
    """
    tree.path.parent.mkdir(parents=True, exist_ok=True)
    tree.path.write_text(json.dumps({"nodes": nodes}, indent=2))


def _old_qh_nodes(tree: Tree) -> list:
    """The implementation being replaced, for comparison.

    `tree.read()` once, then `tree.get` per key. A key whose entry cannot be
    built raised out of the whole call; it is counted as skipped here, which is
    what the replacement does — and reports, where this raised.
    """
    out = []
    for key in tree.read()["nodes"]:
        try:
            node = tree.get(key)
        except (TypeError, ValueError):
            node = None
        if node is not None:
            out.append(node)
    return out


def _events(tree: Tree, kind: str) -> list[dict]:
    if not tree.events_path.exists():
        return []
    return [json.loads(line) for line in tree.events_path.read_text().splitlines()
            if json.loads(line)["kind"] == kind]


def _counted_reads(monkeypatch) -> list[int]:
    """Count `Tree.read` calls; the real one still runs.

    Monkeypatched on the class, not assigned onto an instance: under xdist a
    bare assignment to a module global leaks into whichever test lands on the
    same worker next.
    """
    reads = []
    real = Tree.read

    def counting(self):
        reads.append(1)
        return real(self)

    monkeypatch.setattr(Tree, "read", counting)
    return reads


@pytest.mark.parametrize("count", [1, 2, 40])
def test_qh_nodes_reads_the_tree_once_however_many_nodes_it_holds(tmp_path, monkeypatch, count):
    tree = _project(tmp_path)
    _write(tree, {f"ag-{i:04d}": _entry(f"ag-{i:04d}") for i in range(count)})
    reads = _counted_reads(monkeypatch)

    nodes = _Handover(tree)._qh_nodes()

    assert len(reads) == 1, (
        f"_qh_nodes parsed tree.json {len(reads)} times for {count} node(s); one parse "
        f"is the whole point — every extra one is a full re-read of the file"
    )
    assert [n.id for n in nodes] == [f"ag-{i:04d}" for i in range(count)]


def test_qh_nodes_costs_one_read_per_call_not_one_per_node(tmp_path, monkeypatch):
    """The reads do not scale with the tree, which is what the fix is about.

    Asserted as an equality between two calls on trees of different sizes
    rather than as a count: an implementation that reads the file once for the
    first node and once per node after that would pass a single-size count.
    """
    reads = _counted_reads(monkeypatch)
    sizes = {}
    for count in (1, 8):
        tree = _project(tmp_path / f"n{count}")
        _write(tree, {f"ag-{i:04d}": _entry(f"ag-{i:04d}") for i in range(count)})
        before = len(reads)
        nodes = _Handover(tree)._qh_nodes()
        sizes[count] = (len(reads) - before, len(nodes))
    assert sizes == {1: (1, 1), 8: (1, 8)}


def test_qh_nodes_returns_the_same_nodes_in_the_same_order(tmp_path):
    """File order, and the skipped entries are skipped either way.

    The order is what decides which run is promoted first, so a sort or a set
    here would silently reorder the promotion pass. The three entries that are
    not nodes — an empty mapping, a `None` written from the container, and a
    mapping missing the fields `Node` requires — must all be left out.
    """
    tree = _project(tmp_path)
    _write(tree, {
        "ag-c": _entry("ag-c", status="stuck"),
        "ag-empty": {},
        "ag-none": None,
        "ag-malformed": {"agent": "worker", "provider": "alpha"},
        "ag-a": _entry("ag-a", status="running"),
    })
    expected = _old_qh_nodes(tree)
    assert [n.id for n in expected] == ["ag-c", "ag-a"], "the fixture no longer exercises the skip"

    nodes = _Handover(tree)._qh_nodes()

    assert [n.id for n in nodes] == [n.id for n in expected]
    assert [asdict(n) for n in nodes] == [asdict(n) for n in expected]


def test_qh_nodes_reports_a_malformed_entry_instead_of_raising_it_out(tmp_path):
    """`tree.get` raises on an entry `Node` cannot be built from; the pass
    that iterates the whole tree must not die of one entry. Reporting it is
    what `Tree._nodes` does, and the event names the entry."""
    tree = _project(tmp_path)
    _write(tree, {"ag-good": _entry("ag-good"),
                  "ag-malformed": {"agent": "worker", "provider": "alpha"},
                  "ag-other": _entry("ag-other")})
    with pytest.raises(TypeError):
        tree.get("ag-malformed")     # the replaced behaviour: raised, not reported

    nodes = _Handover(tree)._qh_nodes()

    assert [n.id for n in nodes] == ["ag-good", "ag-other"]
    reported = _events(tree, "malformed_entry")
    assert [(e["node"], e["action"]) for e in reported] == [("ag-malformed", "quota_handover")]
