"""RB: publication and relaunch respect the node's real baseline (RB-R1, RB-R2).

Contract: `context/specs/rb-publish-baseline.md`.

RB-R1 is driven at the completion boundary exactly as
`tests/test_rp_readonly_restore_provenance.py` does (its harness is reused):
the "agent" is git run in a real managed checkout, `Engine.finished` captures
and integrates, and every observation is host git or the stored records.

RB-R2 is driven through a running scheduler with the fake providers of
`tests/test_rr_recovery.py`. The prepared input is observed as the provider's
`head` at spawn and by host git on the node's branch ref. The conflict hold
uses the reason `recovery_conflict` that a conflicting recovery already has.

Seam assumed (named): to give a node a branch tip that differs from what the
dead run's checkout started from, the conflict test advances the node's branch
ref and its stored `branch_tip` together on the host, the way the RR tests
edit stored attempt records.

RB-R3 needs no test of its own: the RP and RR files stay green.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import commit_entry, unwrap  # noqa: E402
from test_lr_loop_resume import make_world  # noqa: E402
from test_rp_readonly_restore_provenance import (  # noqa: E402,F401
    SRC, TEST, Harness, done, h, merged)
from test_rp_second_amendment import resolved  # noqa: E402

WAIT = 5
DEAD = dict(exit=1, stderr="Error: invalid task syntax\n")


# ===================================================================== RB-R1

def publish_tree(h, node_id):
    """The tree a merge of the node's branch onto the base branch leaves (the
    same three-way merge `publish_node` makes; the clean-worktree check of
    the real operation is not what is under test)."""
    tip = h.node(node_id)["branch_tip"]
    return h.git("merge-tree", "--write-tree", "refs/heads/" + h.world.base, tip).splitlines()[0]


def test_rb_r1_newer_base_content_merged_by_the_run_is_kept_and_survives_publication(h):
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "newer on main\n"}, "main edits the test")
    merged(h, impl, main)
    h.write(impl, {SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "newer on main"
    assert not result.get("readonly_reverted")
    assert "readonly_reverted" not in generation
    # Merging the node onto the base branch must not undo main's test.
    h.base_commit({"docs.txt": "later main work\n"}, "unrelated later commit")
    tree = publish_tree(h, impl.id)
    assert h.blob(tree, TEST) == "newer on main"
    assert h.blob(tree, SRC) == "impl"


def test_rb_r1_every_step_of_a_multi_commit_main_history_is_judged_as_main(h):
    impl = h.launch(["tests/**"])
    h.base_commit({TEST: "v1\n"}, "first")
    h.base_commit({SRC: "m1\n"}, "second, other file")
    last = h.base_commit({TEST: "v2\n"}, "third")
    merged(h, impl, last)
    h.write(impl, {SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    assert h.blob(generation["commit"], TEST) == "v2"
    assert not h.result(impl).get("readonly_reverted")


def squashed_input(h, text):
    """An input whose content for TEST is `text`, as a tester generation
    wrote it. Returns the node id the run declares as its input."""
    tester = h.launch(["src/**"])
    h.write(tester, {TEST: text})
    assert done(h, h.finish(tester))
    return tester.id


def test_rb_r1_an_input_whose_content_is_itself_an_older_base_commits_does_not_pin_the_path(h):
    """K10 shape, reconstructed: the input's TEST equals what an earlier base
    commit holds (the input's own node was squash-published, so the base
    commit is not in the input's ancestry and the merge base still has the
    old text). The input carries no version of its own; main's newer commit,
    merged by the run, is kept."""
    node = squashed_input(h, "v1\n")
    impl = h.launch(["tests/**"], inputs=[node])
    assert h.blob(impl.input, TEST) == "v1"
    h.base_commit({TEST: "v1\n"}, "main publishes the same text (squash)")
    newer = h.base_commit({TEST: "v2\n"}, "main edits the test again")
    resolved(h, impl, newer, TEST, "theirs")
    assert h.sh(impl, f"cat {TEST}") == "v2"
    h.write(impl, {SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "v2"
    assert not result.get("readonly_reverted")
    assert "readonly_reverted" not in generation


def test_rb_r1_a_version_of_the_inputs_own_is_not_overridden_by_a_different_base_commit(h):
    """Guard (RP-R11 / Decision 11): the tester's text matches no base
    commit, so main's newer text, even merged, is restored to the input's."""
    node = squashed_input(h, "strict\n")
    impl = h.launch(["tests/**"], inputs=[node])
    h.base_commit({TEST: "main one\n"})
    newer = h.base_commit({TEST: "main two\n"})
    resolved(h, impl, newer, TEST, "theirs")
    assert h.sh(impl, f"cat {TEST}") == "main two"
    h.write(impl, {SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert h.result(impl)["readonly_reverted"] == [TEST]


def test_rb_r1_an_input_matching_a_base_commit_the_run_edits_is_still_reverted(h):
    """An agent's own edit is never kept, even when the input's content is
    base content."""
    node = squashed_input(h, "v1\n")
    impl = h.launch(["tests/**"], inputs=[node])
    h.base_commit({TEST: "v1\n"})
    h.write(impl, {TEST: "edited by impl\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert h.result(impl)["readonly_reverted"] == [TEST]


def test_rb_r1_a_fast_forward_onto_a_newer_base_commit_is_kept(h):
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "ff from main\n"})
    h.sh(impl, f"git merge -q --ff-only {main}")
    generation = done(h, h.finish(impl))
    assert h.blob(generation["commit"], TEST) == "ff from main"
    assert not h.result(impl).get("readonly_reverted")


def test_rb_r1_an_edit_matching_no_base_commit_is_still_restored_and_recorded(h):
    impl = h.launch(["tests/**"])
    h.write(impl, {TEST: "edited by impl\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert h.blob(generation["commit"], SRC) == "impl"
    assert result["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]


def test_rb_r1_an_own_edit_after_merging_newer_main_is_restored_to_mains_version(h):
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "newer on main\n"})
    merged(h, impl, main)
    h.write(impl, {TEST: "edited by impl\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "newer on main"
    assert h.blob(generation["commit"], SRC) == "impl"
    assert result["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]


def test_rb_r1_content_equal_to_an_unmerged_base_commit_is_not_vouched_for(h):
    """Only a base commit that is an ancestor of the result counts: copying
    the text of a main commit the run never merged is an edit."""
    impl = h.launch(["tests/**"])
    h.base_commit({TEST: "on main, never merged\n"})
    h.write(impl, {TEST: "on main, never merged\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert h.result(impl)["readonly_reverted"] == [TEST]


def test_rb_r1_a_rollback_to_an_older_base_chain_version_is_restored(h):
    """RB-R1: ancestor content alone cannot authorise a rollback."""
    h.base_commit({TEST: "older base\n"})
    newer = h.base_commit({TEST: "newer base\n"})
    impl = h.launch(["tests/**"])
    assert h.entry(impl.input, TEST) == h.entry(newer, TEST)
    h.write(impl, {TEST: "older base\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    assert h.entry(generation["commit"], TEST) == h.entry(newer, TEST)
    assert h.result(impl)["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]


def test_rb_r1_an_unmerged_tip_copy_after_a_base_merge_is_restored(h):
    """RB-R1: restoration uses the eligible merged source, not the tip."""
    impl = h.launch(["tests/**"])
    eligible = h.base_commit({TEST: "merged base\n"})
    merged(h, impl, eligible)
    tip = h.base_commit({TEST: "unmerged tip\n"})
    h.write(impl, {TEST: "unmerged tip\n", SRC: "impl\n"})
    assert h.sh(impl, f"if git merge-base --is-ancestor {tip} HEAD; then echo merged; else echo unmerged; fi") == "unmerged"
    generation = done(h, h.finish(impl))
    assert h.entry(generation["commit"], TEST) == h.entry(eligible, TEST)
    assert h.result(impl)["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]


def test_rb_r1_kept_and_edited_paths_in_one_run_are_judged_separately(h):
    other = "tests/test_other.py"
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "newer on main\n"})
    merged(h, impl, main)
    h.write(impl, {other: "edited by impl\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "newer on main"
    assert h.entry(generation["commit"], other) == h.entry(impl.input, other)
    assert result["readonly_reverted"] == [other]


# ===================================================================== RB-R2

@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    yield world
    world.close()


def node_with_tip(w, *, inputs):
    """A node that has integrated one generation (writing shared.txt), so it
    has a recorded branch tip. With `inputs`, it declares a done dependency."""
    w.fxw.queue(commit_entry("input.txt", "dep\n", "dep work"))
    w.start_scheduler()
    dep = w.simple("DEP", "wk")
    w.wait_state(dep, "done", timeout=WAIT)
    w.fxw.queue(commit_entry("shared.txt", "one\n", "first generation"))
    n = w.simple("N", "wk", **({"inputs": [{"node": dep}]} if inputs else {}))
    node = w.wait_state(n, "done", timeout=WAIT)
    assert node["outcome"] != "failed" and len(node["generations"]) == 1, node
    tip = node["branch_tip"]
    assert tip and w.git("rev-parse", f"refs/heads/nodes/{n}").stdout.strip() == tip
    return n, tip


def descends(w, ancestor, commit):
    return w.git("merge-base", "--is-ancestor", ancestor, commit).returncode == 0


def kill_run(w, n, calls):
    """Relaunch `n` into a final failure whose work (wip.txt) is captured."""
    w.fxw.queue(commit_entry("wip.txt", "saved work\n", "dead run work", **DEAD))
    assert w.root_op("relaunch_node", n).get("ok")
    w.until(lambda: len(w.fxw.calls()) == calls, WAIT, what="dead run")
    w.until(lambda: w.get(n)["state"] == "done" and w.get(n)["outcome"] == "failed", WAIT,
            what="dead run settled")


@pytest.mark.parametrize("inputs", [True, False], ids=["declared_inputs", "no_inputs"])
def test_rb_r2_relaunch_input_descends_from_the_branch_tip_without_a_recovery(w, inputs):
    n, tip = node_with_tip(w, inputs=inputs)
    calls = len(w.fxw.calls())
    w.fxw.queue(commit_entry("more.txt", "more\n", "second generation"))
    assert w.root_op("relaunch_node", n).get("ok")
    w.until(lambda: len(w.fxw.calls()) == calls + 1, WAIT, what="relaunch")
    head = w.fxw.calls()[-1]["head"]
    assert descends(w, tip, head), "relaunch input does not contain the node's branch tip"
    node = w.wait_state(n, "done", timeout=WAIT)
    assert node["outcome"] != "failed" and len(node["generations"]) == 2, node


def test_rb_r2_recovery_relaunch_with_inputs_descends_from_the_tip_and_integrates(w):
    n, tip = node_with_tip(w, inputs=True)
    kill_run(w, n, calls=3)
    calls = len(w.fxw.calls())
    # The same file the first generation wrote: without the tip in the input,
    # integrating it onto the branch is an add/add conflict.
    w.fxw.queue(commit_entry("shared.txt", "two\n", "recovered work"))
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok"), reply
    assert unwrap(reply["result"]).get("recovered_from"), reply
    w.until(lambda: len(w.fxw.calls()) == calls + 1, WAIT, what="recovered launch")
    call = w.fxw.calls()[-1]
    assert descends(w, tip, call["head"]), "recovered input does not contain the node's branch tip"
    assert (Path(call["cwd"]) / "wip.txt").read_text() == "saved work\n"
    node = w.wait_state(n, "done", timeout=WAIT)
    assert node["hold"] is None and node["outcome"] != "failed", node
    assert len(node["generations"]) == 2
    final = node["branch_tip"]
    assert final != tip and descends(w, tip, final)
    assert w.show(f"{final}:shared.txt") == "two\n"
    assert w.show(f"{final}:wip.txt") == "saved work\n"
    assert w.show(f"{final}:input.txt") == "dep\n"


def test_rb_r2_recovery_conflicting_with_the_branch_tip_holds_and_never_launches(w):
    n, tip = node_with_tip(w, inputs=True)
    kill_run(w, n, calls=3)
    attempt = w.get(n)["runs"][-1]["attempt_id"]
    # The node's branch moves on, with a change that collides with the
    # captured work (both add wip.txt).
    import os
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def git(*a, **kw):
            return subprocess.run(["git", "-C", str(w.root), *a], capture_output=True, text=True,
                                  env=env, check=True, **kw).stdout.strip()
        git("read-tree", tip)
        blob = subprocess.run(["git", "-C", str(w.root), "hash-object", "-w", "--stdin"],
                              input="upstream\n", capture_output=True, text=True, check=True).stdout.strip()
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},wip.txt")
        tree = git("write-tree")
        advanced = git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit-tree",
                       tree, "-p", tip, "-m", "branch moves on")
    from multiagents.scheduler.rpc import Service
    from multiagents.tree import now
    ref = f"refs/heads/nodes/{n}"
    assert w.git("update-ref", ref, advanced, tip).returncode == 0
    store = Service(w.root, now()).store
    with store.transaction() as db:
        record = json.loads(json.dumps(store.node_records(db)[n]))
        record["branch_tip"] = advanced
        store.save_node(db, record)
    calls = len(w.fxw.calls())
    reply = w.root_op("relaunch_node", n)
    assert reply.get("ok"), reply
    held = w.wait_held(n, "recovery_conflict", timeout=WAIT)
    assert attempt in json.dumps(held["hold"]["detail"]), held["hold"]
    assert len(w.fxw.calls()) == calls, "a conflicting recovery must not launch a provider"
    assert w.git("rev-parse", ref).stdout.strip() == advanced, "the held node's branch must not move"
