"""M6 — two checks the acceptance suite means but its fixture cannot express.

`World.rpc(token="root")` presents the real root capability, so the
acceptance suite's forged `"root"` never reaches the wire as a literal; here it
does (NC-R9). And NC-R45's "b launches after a is done" is read on the
notification log's `seq`, which is the only order the contract gives across
nodes (NC-R14).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import err_code  # noqa: E402


def test_nc_r9_the_literal_root_on_the_wire_is_unauthenticated(tmp_path, monkeypatch):
    w = GitWorld(tmp_path, monkeypatch)
    try:
        w.start_scheduler()
        w.simple("N", "worker", fx={"gate": "g"})
        for bad in ("root", "ROOT", "None", "null"):
            assert err_code(w.raw("list_nodes", bad)) == "unauthenticated", bad
        assert w.raw("list_nodes", w.root_token())["ok"]
    finally:
        w.close()


def test_nc_r45_a_dependent_launches_after_its_dependency_is_done_by_seq(tmp_path, monkeypatch):
    w = GitWorld(tmp_path, monkeypatch)
    try:
        w.start_scheduler()
        a = w.coder("A", {"a.txt": "a\n"}, agent="coder")
        b = w.coder("B", {"b.txt": "b\n"}, agent="coder", depends_on=[{"node": a}])
        w.until(lambda: w.get(b)["state"] == "done", timeout=60, what="b done")
        seq = {(t["node_id"], t["kind"].removeprefix("node.")): t["seq"]
               for t in w.ok("wait_for_nodes", {"timeout": 1})["transitions"]}
        assert seq[(a, "done")] < seq[(b, "launched")]
    finally:
        w.close()
