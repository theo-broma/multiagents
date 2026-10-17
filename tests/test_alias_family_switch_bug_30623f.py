"""bug-30623f: an alias session id never crosses provider families, pinned or not.

An unpinned node of a `session:` alias carries no `pins`, so `pin_changed`
says nothing changed. When the alias's provider is unavailable and the agent's
fallback belongs to another family, the old family's session id must not be
resumed there: the node starts fresh, the alias records reason
`provider_changed`, and the old session is archived.
Fixtures are those of test_alias_pin_admission_bug_30623f.py.
"""
from __future__ import annotations

import time

import pytest

from multiagents.tree import Tree
from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry

BOUND = 10
COOLDOWN = 30 * 86400    # far beyond the wait-for-reset window, so the work moves instead of waiting

PAIR = """\
template: alias-auto
version: 1
params: {}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: 3
  children:
    - {key: wrk, kind: simple, agent: wk, task: implement}
    - {key: rev, kind: simple, agent: rv, task: review, session: B, pins: {model: fxr/m1}}
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr", family="review")
    world.fxb = world.provider("fxb", family="review")      # same family
    world.fxc = world.provider("fxc", family="other")       # another family
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True, models={"fxb": "fxb/m1", "fxc": "fxc/m1"})
    yield world
    world.close()


def first_round(w):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"),
                commit_entry("b.txt", "two\n", "r2", tag="W2", gate="work"))
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], tag="R1"))
    w.fxr.queue(verdict_entry("approved", tag="R2"))
    w.fxb.queue(verdict_entry("approved", tag="B2"))
    w.fxc.queue(verdict_entry("approved", tag="C2"))
    w.start_scheduler()
    w.register_ok(PAIR)
    top = w.instantiate_ok("alias-auto")
    wk, rv = w.children(top)
    w.wait_spawn("W2", w.fxw)
    # The first run needed a pin to land on fxr; the node is unpinned from here on.
    assert w.root_op("update_node", rv, pins={}).get("ok")
    bound = next(b for b in w.status()["aliases"] if b["alias"] == "B")
    return top, rv, bound


def test_automatic_switch_to_another_family_clears_the_alias_session(w):
    top, rv, before = first_round(w)
    assert before["session_id"]
    # fxr is cooling down and so is its same-family sibling: the fallback in
    # another family serves. (A DISABLED own provider is refused before routing.)
    tree = Tree(w.paths.tree_file, w.paths.events_file)
    tree.set_cooldown("fxr", time.time() + COOLDOWN, "test cooling down")
    tree.set_cooldown("fxb", time.time() + COOLDOWN, "test cooling down")
    w.gate("work", w.fxw)
    w.until(lambda: w.fxc.calls() or None, timeout=BOUND,
            what="the other-family fallback to run the alias node")
    calls = w.fxc.calls()
    assert calls[0]["resume"] is None
    assert calls[0]["session"] != before["session_id"]
    after = next(b for b in w.status()["aliases"] if b["id"] == before["id"])
    assert after["provider"] == "fxc" and after["family"] == "other"
    assert after["reason"] == "provider_changed"
    assert after["session_id"] != before["session_id"]
    assert any(a["session_id"] == before["session_id"] for a in after["archives"])
    # Provider receipt precedes the scheduler committing the new launch.
    runs = w.until(
        lambda: (runs if len(runs := w.get(rv)["runs"]) >= 2
                 and "reason" in runs[-1] else None),
        timeout=BOUND, what="the other-family launch to be recorded",
    )
    assert runs[-1]["reason"] == "provider_changed"
