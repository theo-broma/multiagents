"""Readonly restore by provenance, amendment after the adversary pass: RP7-RP9.

Contract: `context/specs/readonly-restore-provenance.md`, section "Amendment
after the adversary pass" (RP7, RP8, RP9, Decision 10). The adversary's own
cases are in tests/test_rp_adversary_stale_source.py and
tests/test_rp_adversary_path_names.py and are not repeated here.

Capture tests use the harness of tests/test_rp_readonly_restore_provenance.py
(real git, `Engine.finished`). RP8 is tested on `config.matches_any`, the
entry point the existing config tests in tests/test_core.py use.

Seams assumed beyond those of the RP1-RP6 suite:
- a second round of the same node is a second launched attempt on its
  stored record, prepared by `Results.prepare` from the node's recorded
  branch tip; its capture appends generation 2.
- "a pre-RP7 persisted decision" is reached through RP4's replay: a decision
  persisted while the trusted set made it right, then records that RP7's
  supersession would read differently. The persisted format is not in the
  contract, so a decision written by the pre-RP7 code itself cannot be
  forged here.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.config import matches_any  # noqa: E402
from multiagents.scheduler.engine import save_attempt  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402
from test_rp_readonly_restore_provenance import (SRC, TEST, crash_after_retention, done,  # noqa: E402,F401
                                                 h, merged)

OTHER = "tests/test_other.py"


def owner_of(h, commit):
    return next(n["id"] for n in h.nodes().values() if any(g["commit"] == commit for g in n["generations"]))


def relaunch(h, launch, readonly=()):
    """The node's next round: a new attempt on the same stored node."""
    nodes = h.nodes()
    node = nodes[launch.id]
    prepared = h.results.prepare(node, nodes)
    run_id = "ag-" + uuid.uuid4().hex[:6]
    attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
               "run_id": run_id, "node_id": node["id"], "state": "launched",
               "locks": [], "at": now(), "readonly_paths": list(readonly), **prepared}
    node.update(state="running", runs=[*node.get("runs", []), {
        "run_id": run_id, "attempt_id": attempt["attempt_id"], "input_commit": prepared["input_commit"]}])
    with h.engine.store.transaction() as db:
        h.engine.store.save_node(db, node)
        save_attempt(db, attempt)
    checkout = h.world.paths.worktree(run_id)
    branch = "agents/guard/" + run_id[3:]
    h.results.create_checkout(checkout, branch, prepared["input_commit"])
    run = Node(id=run_id, agent="guard", provider="fx", model="fx/m1", parent=None, depth=1,
               branch=branch, worktree=str(checkout), status="done", node_id=node["id"],
               attempt_id=attempt["attempt_id"])
    h.engine.runner.tree.add(run)
    h.engine.runner.authority.add(run)
    return type(launch)(id=node["id"], attempt=attempt, run=run, checkout=checkout,
                        input=prepared["input_commit"])


def restored_to(h, launch, expected_ref, paths=(TEST,)):
    generation = done(h, h.finish(launch))
    result = h.result(launch)
    for path in paths:
        assert h.entry(generation["commit"], path) == h.entry(expected_ref, path), \
            f"{path}: kept={result.get('readonly_kept')} reverted={result.get('readonly_reverted')}"
    assert sorted(result["readonly_reverted"]) == sorted(paths)
    return generation, result


# ----------------------------------------------------------------------- RP7

def test_rp7_a_stale_base_does_not_steer_the_restore_target(h):
    """The base moved on with an unrelated commit; it still carries v0, the
    entry the input's tester replaced. An own edit to a third version is
    restored to the input's entry, not to the stale base's."""
    tester = h.generation({TEST: "strict test from tester\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    main = h.base_commit({"unrelated.txt": "u\n"})
    merged(h, impl, main)
    assert h.sh(impl, f"cat {TEST}") == "strict test from tester"
    h.write(impl, {TEST: "v2 by impl\n"})
    generation, result = restored_to(h, impl, impl.input)
    assert h.blob(generation["commit"], TEST) == "strict test from tester"
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp7_a_stale_sibling_generation_does_not_steer_the_restore_target(h):
    sibling = h.generation({"sibling.txt": "s\n"}, readonly=["tests/**"])
    tester = h.generation({TEST: "strict test from tester\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    merged(h, impl, sibling)
    h.write(impl, {TEST: "v2 by impl\n"})
    generation, result = restored_to(h, impl, impl.input)
    assert h.blob(generation["commit"], TEST) == "strict test from tester"
    assert "readonly_kept" not in result


def test_rp7_a_trusted_revert_supersedes_the_trusted_change_it_reverts(h):
    """Input v0. Generation A changes v0->v1; generation B descends from A
    and reverts to v0 (B's own readonly_paths do not cover the test,
    Decision 10). The run merges B and edits to v2. A is an ancestor of B,
    so it is dropped; B brought nothing on this path relative to the input;
    no source vouches, and the path goes back to v0, not to A's v1."""
    a = h.generation({TEST: "v1\n"}, readonly=["src/**"])
    reverter = h.launch(["src/**"], inputs=[owner_of(h, a)])
    h.write(reverter, {TEST: "v0\n"}, "revert v1")
    b = done(h, h.finish(reverter))["commit"]
    assert h.git("merge-base", "--is-ancestor", a, b) == ""
    impl = h.launch(["tests/**"])
    assert h.blob(impl.input, TEST) == "v0"
    merged(h, impl, b)
    assert h.sh(impl, f"cat {TEST}") == "v0"
    h.write(impl, {TEST: "v2 by impl\n"})
    generation, result = restored_to(h, impl, impl.input)
    assert h.blob(generation["commit"], TEST) == "v0"
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp7_a_trusted_revert_does_not_vouch_for_the_old_entry_it_restores(h):
    """Same history as above, but the run itself writes v1 back after
    merging B: A (superseded) must not vouch for it."""
    a = h.generation({TEST: "v1\n"}, readonly=["src/**"])
    reverter = h.launch(["src/**"], inputs=[owner_of(h, a)])
    h.write(reverter, {TEST: "v0\n"}, "revert v1")
    b = done(h, h.finish(reverter))["commit"]
    impl = h.launch(["tests/**"])
    merged(h, impl, b)
    h.write(impl, {TEST: "v1\n"}, "impl puts v1 back")
    generation, result = restored_to(h, impl, impl.input)
    assert h.blob(generation["commit"], TEST) == "v0"
    assert "readonly_kept" not in result


def criss_cross(h):
    """Two merge bases between the input I and a trusted source S.

    X changes TEST (v1); Y changes test_other (o1). I = merge(X, Y) taking
    neither change; S = merge(Y, X) on the base's first-parent chain taking
    both, and also changing SRC. `git merge-base --all I S` is {X, Y}.
    """
    start = h.git("rev-parse", h.world.base)
    x = h.plumb(start, {TEST: "v1\n"}, "X", [start])
    y = h.plumb(start, {OTHER: "o1\n"}, "Y", [start])
    maker = h.launch([])
    h.sh(maker, f"git reset -q --hard {x} && git merge -q --no-ff --no-commit {y} || true; "
                f"git checkout {start} -- {TEST} {OTHER} && git commit -q -m I")
    i = done(h, h.finish(maker))["commit"]
    assert h.blob(i, TEST) == "v0" and h.blob(i, OTHER) == "other"
    impl = h.launch(["**"], inputs=[maker.id])
    assert impl.input == i
    s = h.plumb(x, {OTHER: "o1\n", SRC: "s2\n"}, "S", [y, x])
    h.git("update-ref", "refs/heads/" + h.world.base, s, start)
    assert sorted(h.git("merge-base", "--all", i, s).split()) == sorted([x, y])
    return impl, s


def test_rp7_criss_cross_a_source_vouches_only_where_it_differs_from_every_merge_base(h):
    impl, s = criss_cross(h)
    merged(h, impl, s)
    # The recursive merge keeps the input's side on both test paths and
    # brings S's SRC.
    assert h.sh(impl, f"cat {TEST} {OTHER} {SRC}").split() == ["v0", "other", "s2"]
    # S's TEST equals merge base X's, its test_other equals merge base Y's:
    # it brought neither. Whichever single merge base an implementation
    # picks, one of these two edits would be vouched for.
    h.write(impl, {TEST: "v1\n", OTHER: "o1\n"}, "impl writes the versions of S")
    generation, result = restored_to(h, impl, impl.input, paths=(TEST, OTHER))
    # SRC differs from both merge bases: S brought it, and it is kept.
    assert h.blob(generation["commit"], SRC) == "s2"
    assert result["readonly_kept"] == [{"path": SRC, "source": s}]
    assert generation["readonly_kept"] == [{"path": SRC, "source": s}]


def test_rp7_an_earlier_generation_of_a_multi_generation_node_merged_alone_vouches(h):
    first = h.launch(["src/**"])
    h.write(first, {TEST: "t1\n"})
    g1 = done(h, h.finish(first))["commit"]
    second = relaunch(h, first, readonly=["src/**"])
    assert second.input == g1
    h.write(second, {TEST: "t2\n"})
    node = h.finish(second)
    assert node["state"] == "done" and [g["commit"] for g in node["generations"]][0] == g1
    assert len(node["generations"]) == 2
    impl = h.launch(["tests/**"])
    merged(h, impl, g1)
    h.write(impl, {SRC: "impl\n"})
    checkout_tip = h.tip(impl)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "t1"
    assert result["commit"] == checkout_tip
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": g1}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": g1}]


def test_rp7_rp4_a_persisted_decision_is_replayed_when_a_later_generation_would_supersede_its_source(
        h, monkeypatch):
    """The first capture keeps the tester's t1 (the only eligible source).
    Before the replay, the commit the run also merged - a descendant of the
    tester's generation that moved the test to t2 - becomes a recorded
    generation. Decided afresh, RP7 would drop the tester's generation as
    superseded and restore to t2; the persisted decision is replayed."""
    tester = h.generation({TEST: "t1\n"}, readonly=["src/**"])
    live = h.launch([], inputs=[owner_of(h, tester)])
    candidate = h.write(live, {TEST: "t2\n"})
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    h.sh(impl, f"git fetch -q {live.checkout} {live.run.branch} && git merge -q --no-ff --no-edit FETCH_HEAD")
    h.write(impl, {TEST: "t1\n"}, "impl keeps the tester version")
    checkout_tip = h.tip(impl)
    ref = crash_after_retention(monkeypatch, impl)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        h.engine.finished(impl.attempt, impl.run)
    retained = h.git("rev-parse", ref)
    assert retained == checkout_tip, "the first capture did not keep the tester's version"
    assert done(h, h.finish(live))["commit"] == candidate
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert result["commit"] == retained == generation["commit"]
    assert h.blob(generation["commit"], TEST) == "t1"
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]


# ----------------------------------------------------------------------- RP8

EMBEDDED = "tests/a\nb.py"
TERMINAL = "tests/x.py\n"
TRAILING = "tests/x.py "
LEADING = " tests/x.py"
NEWLINE_DIR = "tests/d\nir/y.py"


@pytest.mark.parametrize("path", [EMBEDDED, TERMINAL, TRAILING], ids=["embedded_nl", "terminal_nl", "trailing_sp"])
@pytest.mark.parametrize("pattern", ["tests/*", "tests/**", "**", "tests/", "tests/**/*", "*.py*"])
def test_rp8_star_globstar_and_directory_patterns_match_any_file_name(path, pattern):
    assert matches_any([pattern], "tests/ab.py"), "precondition: the pattern selects an ordinary name"
    assert matches_any([pattern], path)


def test_rp8_question_mark_matches_a_newline_or_a_space():
    assert matches_any(["tests/a?b.py"], EMBEDDED)
    assert matches_any(["tests/x.py?"], TERMINAL)
    assert matches_any(["tests/x.py?"], TRAILING)
    assert matches_any(["?tests/x.py"], LEADING)
    # `?` is one character and still not a slash.
    assert not matches_any(["tests/a?b.py"], "tests/a\n\nb.py")
    assert not matches_any(["tests/a?b.py"], "tests/a/b.py")


def test_rp8_a_newline_inside_a_directory_name_is_matched_through_it():
    for pattern in ("tests/", "tests/**", "tests/*/y.py", "tests/d?ir/", "**/y.py", "y.py"):
        assert matches_any([pattern], NEWLINE_DIR), pattern


@pytest.mark.parametrize("path", [EMBEDDED, TERMINAL, TRAILING, LEADING, NEWLINE_DIR])
def test_rp8_an_ordered_negation_unselects_odd_names_too(path):
    assert not matches_any(["**", "!**"], path)
    assert matches_any(["!**", "**"], path)


def test_rp8_negations_apply_to_odd_names_in_order():
    assert not matches_any(["**", "!tests/**"], EMBEDDED)
    assert not matches_any(["**", "!tests/"], NEWLINE_DIR)
    assert matches_any(["**", "!tests/**", "tests/a?b.py"], EMBEDDED)
    assert not matches_any(["tests/**", "!tests/x.py?"], TERMINAL)


def test_rp8_the_path_is_never_trimmed_before_matching():
    """A file named `tests/keep.py` plus a newline or a space is not
    `tests/keep.py`: a negation carving out the latter leaves it protected."""
    for path in ("tests/keep.py\n", "tests/keep.py ", "tests/keep.py\t"):
        assert matches_any(["tests/**", "!tests/keep.py"], path), repr(path)
    assert not matches_any(["tests/keep.py"], " tests/keep.py")
    assert not matches_any(["tests/keep.py"], "tests/keep.py\n")


def test_rp8_trimming_of_configured_patterns_is_unchanged():
    assert matches_any(["  tests/**  "], "tests/ab.py")
    assert matches_any(["tests/**", " ! tests/x.py "], "tests/y.py")
    assert not matches_any(["tests/**", " ! tests/x.py "], "tests/x.py")


# ----------------------------------------------------------------------- RP9

def test_rp9_an_unresolvable_base_with_no_protected_change_is_captured_as_today(h):
    impl = h.launch(["tests/**"])
    h.write(impl, {SRC: "impl\n", "tests/test_new.py": "added\n"})
    checkout_tip = h.tip(impl)
    h.world.project["git"] = {"base_branch": "no-such-branch"}
    h.world.write_config()
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert result["commit"] == checkout_tip == generation["commit"]
    assert not result.get("capture_error")
    assert not result.get("readonly_reverted")
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp9_rp5_a_replay_whose_restored_tree_changed_is_refused(h, monkeypatch):
    impl = h.launch(["tests/**"])
    h.write(impl, {TEST: "edited\n", SRC: "impl\n"})
    ref = crash_after_retention(monkeypatch, impl)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        h.engine.finished(impl.attempt, impl.run)
    retained = h.git("rev-parse", ref)
    assert h.entry(retained, TEST) == h.entry(impl.input, TEST)
    # The checkout moves on after the decision: its restored tree is no
    # longer the retained one.
    h.write(impl, {SRC: "changed after capture\n"})
    node = h.finish(impl)
    result = h.result(impl)
    assert node["generations"] == [], "a changed result was integrated"
    assert node["state"] != "done"
    assert result.get("capture_error")
    assert h.world.tip(impl.id) in (None, impl.input)
    assert h.git("rev-parse", ref) == retained
