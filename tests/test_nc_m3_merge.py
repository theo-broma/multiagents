"""M3 — `merge_node` (NC-R39), retention, publication and disposal (NC-R64).

Assumptions where the contract is silent (kept loose):
- error CODES are asserted only where the contract names them
  (`not_approved`, `forbidden`, `unauthenticated`, `published`); other refusals
  are asserted as "not ok, and nothing changed".
- `dispose_node {id, revision}`; a disposed node stays readable and reports the
  word `disposed` somewhere in its record.
- `merge_node` squashes by default: main gains exactly one single-parent commit.
- `published` is a field of the node holding the merge commit.
- `test_nc_r64_relaunch_*` and the cancel-keeps-results test need `relaunch_node`
  (M4) and group launch (M4) respectively.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import err_code, run_id_of  # noqa: E402


# Upper bound on every poll-until-condition wait (run to start, node to settle,
# scheduler restart to converge). A red test hits its assertion within this
# bound instead of the harness defaults of 30-60 s.
WAIT_BOUND = 8.0
# Same, for the few waits that span several runs or a scheduler restart (the
# test asked for 60 s).
LONG_BOUND = 20.0


def bound(world: GitWorld) -> None:
    """Cap each `world.until` deadline (the base of wait_state, done,
    wait_running, wait_spawn) at WAIT_BOUND (LONG_BOUND where 60 s or more was asked)."""
    until = world.until

    def capped(pred, timeout: float = 30, *args, **kw):
        return until(pred, min(timeout, LONG_BOUND if timeout >= 60 else WAIT_BOUND), *args, **kw)

    world.until = capped


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    bound(world)
    yield world
    world.close()


def merge(w: GitWorld, node: str, token="root", **extra) -> dict:
    return w.rpc("merge_node", {"id": node, **extra}, token)


def dispose(w: GitWorld, node: str, revision: int | None = None, token="root") -> dict:
    if revision is None:
        revision = w.get(node)["revision"]
    return w.rpc("dispose_node", {"id": node, "revision": revision}, token)


def done_node(w: GitWorld, files=None, tag="A", **kw) -> str:
    node = w.coder(tag, files or {"a.txt": "a\n"}, **kw)
    w.done(node)
    return node


def parents(w: GitWorld, commit: str) -> list[str]:
    return w.git("rev-list", "--parents", "-n", "1", commit).split()[1:]


# ----------------------------------------------------------------- NC-R39

def test_nc_r39_merge_node_squashes_the_branch_into_main(w):
    w.start_scheduler()
    node = done_node(w, {"a.txt": "a\n", "b.txt": "b\n"})
    before = w.main_tip()
    reply = merge(w, node)
    assert reply.get("ok") is True, reply
    after = w.main_tip()
    assert after != before
    assert parents(w, after) == [before], "not a squash: main did not gain one single-parent commit"
    assert w.blob(after, "a.txt") == "a" and w.blob(after, "b.txt") == "b"
    assert w.git("status", "--porcelain") == ""


def test_nc_r64_merge_marks_the_node_published_and_keeps_branch_and_node(w):
    w.start_scheduler()
    node = done_node(w)
    tip = w.tip(node)
    assert merge(w, node).get("ok") is True
    got = w.get(node)
    assert got["published"] == w.main_tip()
    assert w.tip(node) == tip, "the node branch was moved or removed by the merge"
    assert got["state"] == "done"
    assert w.generations(node)


def test_nc_r39_a_node_that_is_not_done_is_refused_and_main_is_unchanged(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    w.wait_running(node)
    before = w.main_tip()
    assert merge(w, node).get("ok") is False
    assert w.main_tip() == before
    w.gate("ga")


def test_nc_r39_a_failed_node_is_refused_not_approved_unless_forced(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, fx={"crash": True})
    w.done(node)
    before = w.main_tip()
    reply = merge(w, node)
    assert err_code(reply) == "not_approved", reply
    assert w.main_tip() == before
    assert "published" not in w.get(node) or not w.get(node)["published"]


def test_nc_r39_an_unknown_node_is_refused(w):
    w.start_scheduler()
    before = w.main_tip()
    assert merge(w, "nd-00000000").get("ok") is False
    assert w.main_tip() == before


def test_nc_r39_only_root_may_merge(w):
    from multiagents.scheduler import issue_run_capability
    w.start_scheduler()
    node = done_node(w)
    token = issue_run_capability(w.root, "run-x", node, {"read", "delegate"})
    before = w.main_tip()
    assert err_code(merge(w, node, token)) == "forbidden"
    assert err_code(merge(w, node, None)) == "unauthenticated"
    assert err_code(merge(w, node, "not-a-token")) == "unauthenticated"
    assert w.main_tip() == before


def test_nc_r39_a_conflict_with_main_is_reported_and_nothing_is_half_merged(w):
    w.start_scheduler()
    node = done_node(w, {"a.txt": "from the node\n"})
    before = w.commit_on_main("a.txt", "from main\n", "main took a.txt")
    reply = merge(w, node)
    assert reply.get("ok") is False
    assert w.main_tip() == before
    assert w.blob(before, "a.txt") == "from main"
    assert w.git("status", "--porcelain") == ""
    assert not (w.root / ".git" / "MERGE_HEAD").exists()
    assert "<<<<<<<" not in (w.root / "a.txt").read_text()
    got = w.get(node)
    assert not got.get("published")
    assert got["state"] == "done"
    assert w.tip(node) is not None


def test_nc_r39_a_retry_with_the_same_request_id_merges_once(w):
    w.start_scheduler()
    node = done_node(w)
    before = w.main_tip()
    first = w.rpc("merge_node", {"id": node}, request_id="mrg-1")
    second = w.rpc("merge_node", {"id": node}, request_id="mrg-1")
    assert first.get("ok") is True and second == first
    assert parents(w, w.main_tip()) == [before]


def test_nc_r64_merging_an_already_published_node_again_adds_no_commit(w):
    w.start_scheduler()
    node = done_node(w)
    assert merge(w, node).get("ok") is True
    tip = w.main_tip()
    merge(w, node)
    assert w.main_tip() == tip


def test_nc_r39_main_keeps_commits_made_after_the_node_was_done(w):
    w.start_scheduler()
    node = done_node(w, {"a.txt": "a\n"})
    w.commit_on_main("other.txt", "o\n", "unrelated main work")
    assert merge(w, node).get("ok") is True
    assert {"a.txt", "other.txt"} <= w.files(w.main_tip())


def test_nc_r39_the_readonly_revert_is_already_in_what_is_merged(w):
    w.commit_on_main("protected.txt", "original\n", "protected base")
    w.start_scheduler()
    node = done_node(w, {"protected.txt": "tampered\n", "ok.txt": "ok\n"}, agent="guard")
    assert merge(w, node).get("ok") is True
    assert w.blob(w.main_tip(), "protected.txt") == "original"
    assert w.blob(w.main_tip(), "ok.txt") == "ok"


# ----------------------------------------------------------------- NC-R64

def test_nc_r64_dispose_removes_the_branch_and_every_ref_of_the_node(w):
    w.start_scheduler()
    node = done_node(w)
    assert w.tip(node)
    reply = dispose(w, node)
    assert reply.get("ok") is True, reply
    assert w.tip(node) is None
    assert [r for r in w.all_refs() if node in r] == []
    assert "disposed" in json.dumps(w.get(node))


def test_nc_r64_dispose_of_a_published_node_keeps_what_main_received(w):
    w.start_scheduler()
    node = done_node(w)
    assert merge(w, node).get("ok") is True
    assert dispose(w, node).get("ok") is True
    assert w.blob(w.main_tip(), "a.txt") == "a"


def test_nc_r64_dispose_is_refused_while_the_node_is_active(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    w.wait_running(node)
    assert dispose(w, node).get("ok") is False
    assert w.tip(node) is not None
    w.gate("ga")
    assert w.done(node)["outcome"] == "completed"


def test_nc_r64_dispose_is_refused_while_another_node_names_it_as_input(w):
    w.start_scheduler()
    a = done_node(w)
    blocker = w.simple("BLK", fx={"gate": "never"})
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}],
                depends_on=[{"node": blocker}])
    w.wait_running(blocker)
    before = w.all_refs()
    assert dispose(w, a).get("ok") is False
    assert w.all_refs() == before
    w.gate("never")
    w.done(b)
    assert "a.txt" in w.files(w.tip(b))


def test_nc_r64_dispose_needs_the_current_revision(w):
    w.start_scheduler()
    node = done_node(w)
    rev = w.get(node)["revision"]
    reply = dispose(w, node, rev + 5)
    assert err_code(reply) == "conflict"
    assert w.tip(node) is not None


def test_nc_r64_only_root_may_dispose(w):
    from multiagents.scheduler import issue_run_capability
    w.start_scheduler()
    node = done_node(w)
    token = issue_run_capability(w.root, "run-x", node, {"read", "delegate"})
    assert err_code(dispose(w, node, token=token)) == "forbidden"
    assert err_code(dispose(w, node, token=None)) == "unauthenticated"
    assert w.tip(node) is not None


def test_nc_r64_relaunch_of_a_published_node_is_refused_published(w):
    """Needs `relaunch_node` (M4)."""
    w.start_scheduler()
    node = done_node(w)
    assert merge(w, node).get("ok") is True
    rev = w.get(node)["revision"]
    reply = w.rpc("relaunch_node", {"id": node, "revision": rev})
    assert err_code(reply) == "published", reply
    assert w.get(node)["state"] == "done"


def test_nc_r64_cancel_keeps_the_results_already_integrated(w):
    """Needs group launch (M4)."""
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"})
    b = w.simple("B", fx={"hang": True})
    g = w.group([a, b])
    w.done(a)
    run_b = run_id_of(w.wait_running(b)["active_run"])
    w.until(lambda: "a.txt" in w.files(w.tip(g)), what="A integrated")
    assert w.cancel(g).get("ok") is True
    w.until(lambda: w.get(g)["state"] == "cancelled", what="the group to be cancelled")
    assert "a.txt" in w.files(w.tip(g))
    assert w.generations(a) or w.generations(g)
    assert w.tree_nodes()[run_b]["status"] != "running"
