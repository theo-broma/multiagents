"""M4 — composites: NC-R5 (sequence, group), the `ancestor` code of NC-R22/R52,
composite locks NC-R68.

Assumptions where the contract is silent (kept loose):
- after a sequence child fails, the later children are never launched and the
  sequence never becomes `approved`; whether it turns `done/failed` at once or
  stays open is not asserted (NC-R5: "done when its children are").
- a child blocked only by the implicit sequence edge shows `dependency` or
  `ancestor`; the tests assert only that it is not eligible and not `ready`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, blocked_codes  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    yield world
    world.close()


def gated(w, tag, **kw):
    return w.simple(tag, fx={"gate": f"g{tag}"}, **kw)


# ----------------------------------------------------------------- NC-R5

def test_nc_r5_a_sequence_launches_its_children_in_order(w):
    w.start_scheduler()
    a, b, c = gated(w, "A"), gated(w, "B"), gated(w, "C")
    seq = w.comp("sequence", [a, b, c])
    w.wait_running(a)
    w.quiet(2)
    assert w.fx.by_tag("B") == [] and w.fx.by_tag("C") == []
    view = w.get(b)
    assert view["eligible"] is False and view["ready"] is False
    assert w.get(seq)["state"] == "running"
    w.gate("gA")
    w.wait_running(b)
    w.quiet(1)
    assert w.fx.by_tag("C") == []
    w.gate("gB")
    w.wait_running(c)
    w.gate("gC")
    done = w.wait_state(seq, "done")
    assert done["outcome"] == "approved"
    assert [x["tag"] for x in w.fx.calls()] == ["A", "B", "C"]


def test_nc_r5_a_failed_sequence_child_stops_the_rest_and_is_never_approved(w):
    w.start_scheduler()
    a = w.simple("A", fx={"crash": True})
    b = w.simple("B")
    seq = w.comp("sequence", [a, b])
    assert w.wait_state(a, "done")["outcome"] == "failed"
    w.quiet(3)
    assert w.fx.by_tag("B") == []
    assert w.get(b)["state"] == "open"
    assert w.get(seq).get("outcome") != "approved"


def test_nc_r5_group_children_without_dependencies_run_in_parallel(w):
    w.start_scheduler()
    a, b = gated(w, "A"), gated(w, "B")
    grp = w.comp("group", [a, b])
    w.wait_running(a)
    w.wait_running(b)
    assert w.get(grp)["state"] == "running"
    w.gate("gA"); w.gate("gB")
    assert w.wait_state(grp, "done")["outcome"] == "approved"


def test_nc_r5_group_children_follow_their_own_depends_on(w):
    w.start_scheduler()
    a = gated(w, "A")
    b = w.simple("B", depends_on=[{"node": a, "require": "success"}])
    grp = w.comp("group", [a, b])
    w.wait_running(a)
    w.quiet(2)
    assert w.fx.by_tag("B") == []
    w.gate("gA")
    w.wait_state(b, "done")
    assert w.wait_state(grp, "done")["outcome"] == "approved"


def test_nc_r5_a_group_with_a_failed_child_is_done_failed(w):
    w.start_scheduler()
    a = w.simple("A", fx={"crash": True})
    b = w.simple("B")
    grp = w.comp("group", [a, b])
    assert w.wait_state(grp, "done")["outcome"] == "failed"
    assert w.get(b)["outcome"] == "completed"


def test_nc_r5_a_sequence_may_contain_a_group(w):
    w.start_scheduler()
    x, y = w.simple("X"), w.simple("Y")
    grp = w.comp("group", [x, y])
    z = w.simple("Z")
    seq = w.comp("sequence", [grp, z])
    assert w.wait_state(seq, "done")["outcome"] == "approved"
    calls = {c["tag"]: c["t"] for c in w.fx.calls()}
    assert calls["Z"] > calls["X"] and calls["Z"] > calls["Y"]


def test_nc_r5_r52_a_composites_dependency_gates_every_descendant_with_the_ancestor_code(w):
    w.start_scheduler()
    p = gated(w, "P")
    w.wait_running(p)
    c = w.simple("C")
    grp = w.comp("group", [c], depends_on=[{"node": p, "require": "success"}])
    view = w.until(lambda: "ancestor" in blocked_codes(w.get(c)) and w.get(c),
                   what="the ancestor code on the descendant")
    assert view["eligible"] is False and view["ready"] is False
    w.quiet(2)
    assert w.fx.by_tag("C") == []
    w.gate("gP")
    w.wait_state(c, "done")
    assert w.wait_state(grp, "done")["outcome"] == "approved"


# ----------------------------------------------------------------- NC-R68

def test_nc_r68_a_composite_lock_is_held_between_its_children_until_it_is_done(w):
    w.start_scheduler()
    p = gated(w, "P")
    w.wait_running(p)
    a = w.simple("A", locks=["L"], fx={})
    b = w.simple("B", depends_on=[{"node": p, "require": "success"}])
    seq = w.comp("sequence", [a, b], locks=["L"])
    w.wait_state(a, "done")                  # a ran under the composite's lock, own lock too
    x = w.simple("X", locks=["L"])
    view = w.until(lambda: "lock" in blocked_codes(w.get(x)) and w.get(x),
                   what="X blocked by the composite lock in the gap between children")
    assert view["state"] == "open"
    w.quiet(2)
    assert w.fx.by_tag("X") == []
    w.gate("gP")
    assert w.wait_state(seq, "done")["outcome"] == "approved"
    w.wait_state(x, "done")
    t = {c["tag"]: c["t"] for c in w.fx.calls()}
    assert t["X"] > t["B"]


def test_nc_r68_a_composite_lock_never_blocks_its_own_descendants(w):
    w.start_scheduler()
    a = gated(w, "A", locks=["L"])
    b = gated(w, "B", locks=["L"])
    grp = w.comp("group", [a, b], locks=["L"])
    w.wait_running(a)
    w.wait_running(b)
    w.gate("gA"); w.gate("gB")
    w.wait_state(grp, "done")


def test_nc_r68_the_composite_lock_is_free_before_any_descendant_launched(w):
    w.start_scheduler()
    h = gated(w, "H", locks=["b"])
    w.wait_running(h)
    c = w.simple("C", locks=["b"])
    grp = w.comp("group", [c], locks=["a"])
    view = w.until(lambda: "lock" in blocked_codes(w.get(c)) and w.get(c), what="C blocked on b")
    wnode = w.simple("W", locks=["a"], fx={"gate": "gW"})
    w.wait_running(wnode)                    # the composite does not sit on `a` while C waits for `b`
    assert w.fx.by_tag("C") == []
    w.gate("gW"); w.gate("gH")
    w.wait_state(wnode, "done")
    w.wait_state(c, "done")
    assert w.wait_state(grp, "done")["outcome"] == "approved"


def test_nc_r68_cancelling_the_composite_releases_its_lock(w):
    w.start_scheduler()
    a = w.simple("A", fx={"hang": True})
    grp = w.comp("group", [a], locks=["L"])
    w.wait_running(a)
    x = w.simple("X", locks=["L"])
    w.until(lambda: "lock" in blocked_codes(w.get(x)), what="X blocked")
    assert w.cancel(grp)["ok"]
    w.wait_state(x, "done", timeout=60)
    assert w.get(grp)["state"] == "cancelled"
