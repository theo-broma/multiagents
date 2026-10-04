"""M3 — node branches, recorded results, conflict-safe integration:
NC-R33, NC-R65 (write restrictions), NC-R63, and the git adversarial items of
NC-R47 (H1/H3).

Contract: `context/specs/phase7-part1-contract.md`, revision sections included.
The scheduler is a real process; runs are fixture-agent processes that commit
files (`tests/nc_fixture`, `gitworld.py`); every observation is host git on the
project repository, the NC-R8 socket, or `tree.json`.

Assumptions where the contract is silent (kept loose):
- the branch is `refs/heads/nodes/<id>` and the node's `generations` entry
  carries `{seq, commit, run_id, verdict}` (NC-R4); `seq` starts at 1.
- a run that exits 0 after committing ends `done/completed` (NC-R84).
- tests with a `_group` suffix integrate two runs under ONE top-level node and
  therefore need a `group` composite to launch its independent children
  (NC-R5, nominally M4). There is no way to have two integrations into one
  branch without a composite; they are M3 acceptance only once group launch
  works.
- what a node does when its result is refused (not a descendant of the input,
  a forged ref) is not specified beyond "never integrated"; those tests assert
  the invariant (no forged content in `nodes/*`, no generation for it, node
  settled) and nothing about the hold reason.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import err_code, run_id_of  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    yield world
    world.close()


def settled(w: GitWorld, node_id: str, timeout: float = 45) -> dict:
    return w.until(lambda: w.get(node_id)["state"] in ("done", "held", "cancelled")
                   and w.get(node_id), timeout, what=f"{node_id} to settle")


# ----------------------------------------------------------------- NC-R33

def test_nc_r33_no_branch_exists_for_a_node_that_has_not_launched(w):
    w.start_scheduler()
    blocker = w.simple("BLK", fx={"gate": "never"})
    waiting = w.coder("W", {"w.txt": "w\n"}, depends_on=[{"node": blocker}])
    w.wait_running(blocker)
    w.quiet(2)
    assert w.tip(waiting) is None
    assert w.get(waiting)["state"] == "open"
    assert w.main_tip() == w.git("rev-parse", w.base)
    w.gate("never")


def test_nc_r33_the_branch_is_cut_from_main_at_first_launch(w):
    w.start_scheduler()
    main_before = w.main_tip()
    node = w.coder("A", {"a.txt": "a\n"}, fx={"gate": "ga"})
    w.wait_running(node)
    assert w.tip(node) == main_before
    w.gate("ga")
    w.done(node)


def test_nc_r33_the_run_works_on_a_checkout_of_the_node_branch_tip(w):
    w.start_scheduler()
    main_before = w.main_tip()
    node = w.coder("A", {"a.txt": "a\n"},
                   fx={"shell": ["git rev-parse HEAD > $FX_DIR/head.A",
                                 "git rev-parse --show-toplevel > $FX_DIR/top.A"]})
    w.done(node)
    assert w.fx_file("head.A").strip() == main_before
    assert Path(w.fx_file("top.A").strip()).resolve() != w.root.resolve()


def test_nc_r33_a_committed_result_is_integrated_into_the_node_branch_as_a_generation(w):
    w.start_scheduler()
    main_before = w.main_tip()
    node = w.coder("A", {"a.txt": "hello\n"})
    done = w.done(node)
    assert done["outcome"] == "completed"
    gens = done["generations"]
    assert len(gens) == 1
    gen = gens[0]
    assert gen["seq"] == 1
    assert gen["run_id"] == done["runs"][0]["run_id"]
    assert gen["verdict"] is None
    assert w.tip(node) == gen["commit"]
    assert w.blob(gen["commit"], "a.txt") == "hello"
    assert w.is_ancestor(main_before, gen["commit"])


def test_nc_r33_main_is_never_written_by_the_scheduler(w):
    w.start_scheduler()
    main_before = w.main_tip()
    nodes = [w.coder(t, {f"{t}.txt": t}) for t in ("A", "B", "C")]
    for n in nodes:
        w.done(n)
    assert w.main_tip() == main_before
    assert not (w.root / "A.txt").exists()
    assert w.git("status", "--porcelain") == ""


def test_nc_r33_a_failed_run_integrates_nothing_and_leaves_the_branch_where_it_was(w):
    w.start_scheduler()
    main_before = w.main_tip()
    node = w.coder("A", {"a.txt": "a"}, fx={"crash": True})
    done = w.done(node)
    assert done["outcome"] == "failed"
    assert done["generations"] == []
    assert w.tip(node) in (None, main_before)


def test_nc_r33_integration_is_not_approval_a_new_generation_has_no_verdict_yet(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"})
    w.done(node)
    assert [g["verdict"] for g in w.generations(node)] == [None]


def test_nc_r33_integration_is_recorded_once_in_the_log_after_run_finished(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"})
    w.done(node)
    seq = [t for t in w.transitions(node) if t in ("run_finished", "integrated", "done")]
    assert seq == ["run_finished", "integrated", "done"]


def test_nc_r33_the_sibling_node_branches_are_independent(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"})
    b = w.coder("B", {"b.txt": "b"})
    w.done(a)
    w.done(b)
    assert w.tip(a) != w.tip(b)
    assert "b.txt" not in w.files(w.tip(a)) and "a.txt" not in w.files(w.tip(b))


def test_nc_r33_the_generation_commit_is_the_run_branch_tip_not_a_rewrite(w):
    """The commit a run made is what is integrated (fast-forward when the
    branch did not move): its message and content survive."""
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, fx={"commit": "the message of A"})
    w.done(node)
    commit = w.generations(node)[0]["commit"]
    assert w.git("log", "-1", "--format=%s", commit) == "the message of A"


# --- parallel children under one top-level node (needs group launch) -------

def test_nc_r33_group_two_children_touching_different_files_are_both_integrated(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"}, fx={"gate": "go"})
    b = w.coder("B", {"b.txt": "b\n"}, fx={"gate": "go"})
    g = w.group([a, b])
    w.wait_running(a)
    w.wait_running(b)                       # both launched: independent children
    w.gate("go")
    w.done(g)
    tip = w.tip(g)
    assert {"a.txt", "b.txt"} <= w.files(tip)
    assert w.get(g)["outcome"] == "approved"
    assert len(w.generations(a)) + len(w.generations(b)) + len(w.generations(g)) >= 2


def test_nc_r33_group_conflicting_children_leave_the_branch_unchanged_and_hold_only_that_node(w):
    w.commit_on_main("shared.txt", "base\n", "shared base")
    w.start_scheduler()
    a = w.coder("A", {"shared.txt": "from A\n"}, fx={"gate": "ga"})
    b = w.coder("B", {"shared.txt": "from B\n"}, fx={"gate": "gb"})
    sibling = w.coder("S", {"s.txt": "s\n"}, fx={"gate": "gs"})
    g = w.group([a, b, sibling])
    for n in (a, b, sibling):
        w.wait_running(n)
    w.gate("ga")
    w.until(lambda: w.generations(a) or w.generations(g), what="A integrated")
    tip_after_a = w.tip(g)
    w.gate("gb")
    held = w.until(lambda: (n := w.get(b))["state"] == "held" and n, what="B held on conflict")
    assert held["hold"]["reason"] == "integration_conflict"
    assert w.tip(g) == tip_after_a, "a conflicting integration moved the branch"
    assert w.blob(w.tip(g), "shared.txt") == "from A"
    assert w.get(sibling)["state"] == "running", "a conflict stopped an unrelated sibling"
    w.gate("gs")
    w.wait_state(sibling, "done")
    assert "integration_conflict" in w.transitions(b)
    assert w.main_tip() == w.git("rev-parse", w.base)


def test_nc_r33_group_non_overlapping_edits_of_one_file_merge_with_a_host_merge_commit(w):
    base = "".join(f"line {i}\n" for i in range(1, 41))
    w.commit_on_main("big.txt", base, "big base")
    w.start_scheduler()
    first = base.replace("line 2\n", "line 2 A\n")
    second = base.replace("line 38\n", "line 38 B\n")
    a = w.coder("A", {"big.txt": first}, fx={"gate": "ga"})
    b = w.coder("B", {"big.txt": second}, fx={"gate": "gb"})
    g = w.group([a, b])
    w.wait_running(a)
    w.wait_running(b)
    w.gate("ga")
    w.until(lambda: len(w.generations(a)) + len(w.generations(g)) >= 1, what="first integrated")
    w.gate("gb")
    w.done(g)
    text = w.blob(w.tip(g), "big.txt")
    assert "line 2 A" in text and "line 38 B" in text


# ----------------------------------------------------------------- NC-R65

def test_nc_r65_readonly_paths_are_restored_from_the_input_commit_and_recorded(w):
    w.commit_on_main("protected.txt", "original\n", "protected base")
    w.start_scheduler()
    node = w.coder("A", {"protected.txt": "tampered\n", "ok.txt": "fine\n"}, agent="guard")
    done = w.done(node)
    tip = w.tip(node)
    assert w.blob(tip, "protected.txt") == "original"
    assert w.blob(tip, "ok.txt") == "fine"
    assert "protected.txt" in json.dumps(done["generations"][0])


def test_nc_r65_a_result_that_touches_no_protected_path_records_no_reverted_path(w):
    w.commit_on_main("protected.txt", "original\n", "protected base")
    w.start_scheduler()
    node = w.coder("A", {"ok.txt": "fine\n"}, agent="guard")
    done = w.done(node)
    assert "protected.txt" not in json.dumps(done["generations"][0])


def test_nc_r65_a_run_cannot_move_a_node_branch_to_a_commit_of_its_choosing(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, commit=False,
                   fx={"shell": [
                       "git checkout -q --orphan evil && echo evil > evil.txt "
                       "&& git add -A && git commit -q -m evil && "
                       "for r in $(git for-each-ref --format='%(refname)' refs/heads/nodes); do "
                       "git update-ref $r $(git rev-parse evil); done"]})
    settled(w, node)
    evil = w.git("rev-parse", "--verify", "-q", "refs/heads/evil", check=False)
    for ref, sha in w.all_refs().items():
        if ref.startswith("refs/heads/nodes/"):
            assert "evil.txt" not in w.files(sha), f"{ref} was redirected by a run"
    assert evil not in [g["commit"] for g in w.generations(node)]


def test_nc_r65_a_run_cannot_create_a_node_branch_for_a_node_that_does_not_exist(w):
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"}, commit=False,
                   fx={"shell": ["git branch nodes/nd-deadbeef"]})
    settled(w, node)
    assert w.tip("nd-deadbeef") is None


# ------------------------------------------------- NC-R47 / NC-R33 (H1/H3)

FORGE_ORPHAN_HEAD = (
    "git checkout -q --orphan evil && echo evil > evil.txt && git add -A "
    "&& git commit -q -m evil")


def _no_trace_of_evil(w: GitWorld, node: str) -> None:
    for ref, sha in w.all_refs().items():
        if ref.startswith("refs/heads/nodes/"):
            assert "evil.txt" not in w.files(sha), f"{ref} carries forged content"
    for g in w.generations(node):
        assert "evil.txt" not in w.files(g["commit"])
    assert "evil.txt" not in w.files(w.main_tip())


def test_nc_r47_a_head_forged_to_an_orphan_branch_is_not_integrated(w):
    w.start_scheduler()
    node = w.coder("A", commit=False, fx={"shell": [FORGE_ORPHAN_HEAD]})
    settled(w, node)
    _no_trace_of_evil(w, node)


def test_nc_r47_a_run_branch_rewritten_to_an_unrelated_commit_is_not_integrated(w):
    w.start_scheduler()
    node = w.coder("A", commit=False, fx={"shell": [
        "B=$(git symbolic-ref --short HEAD)",
        FORGE_ORPHAN_HEAD + " && git update-ref refs/heads/$B evil"]})
    settled(w, node)
    _no_trace_of_evil(w, node)
    assert w.get(node)["state"] in ("held", "done")


def test_nc_r47_a_worktree_dot_git_pointed_at_another_repository_is_not_followed(w):
    w.start_scheduler()
    forge = (
        'set -e; E="$FX_DIR/evilrepo"; rm -rf "$E"; mkdir "$E"; '
        'git init -q "$E"; echo evil > "$E/evil.txt"; git -C "$E" add -A; '
        'git -C "$E" commit -q -m evil; '
        'git -C "$E" branch -q -f "$(git symbolic-ref --short HEAD)" HEAD || true; '
        'printf "gitdir: $E/.git\\n" > .git')
    node = w.coder("A", commit=False, fx={"shell": [forge]})
    settled(w, node)
    _no_trace_of_evil(w, node)


# ----------------------------------------------------------------- NC-R63

def test_nc_r63_merge_agent_and_discard_agent_refuse_a_managed_run(w):
    from nc_fixture.world import call_tool
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"})
    done = w.done(node)
    run_id = done["runs"][0]["run_id"]
    tip = w.tip(node)
    refs = w.all_refs()
    for name in ("merge_agent", "discard_agent"):
        out = call_tool(w, name, run_id)
        assert out.get("error") == "managed_run", (name, out)
    assert w.all_refs() == refs
    assert w.tip(node) == tip
    assert w.main_tip() == w.git("rev-parse", w.base)
    assert "a.txt" not in w.files(w.main_tip())


def test_nc_r63_collecting_a_run_does_not_remove_the_node_branch_or_its_commits(w):
    from nc_fixture.world import call_tool
    w.start_scheduler()
    node = w.coder("A", {"a.txt": "a"})
    done = w.done(node)
    commit = done["generations"][0]["commit"]
    call_tool(w, "collect_agent", done["runs"][0]["run_id"])
    assert w.tip(node) == commit
    w.git("reflog", "expire", "--expire=now", "--all")
    w.git("gc", "-q", "--prune=now")
    assert w.exists(commit)


def test_nc_r63_a_generation_commit_survives_gc_when_the_branch_has_moved_on(w):
    """Generations are kept by host-written refs, not only by the branch tip:
    after a second integration the first generation's commit is still there."""
    w.start_scheduler()
    first = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    second = w.coder("B", {"b.txt": "b"}, fx={"gate": "gb"})
    g = w.group([first, second])
    w.wait_running(first)
    w.wait_running(second)
    w.gate("ga")
    w.until(lambda: w.tip(g) != w.main_tip(), what="first integration")
    seen = w.tip(g)
    w.gate("gb")
    w.done(g)
    assert w.tip(g) != seen
    w.git("reflog", "expire", "--expire=now", "--all")
    w.git("gc", "-q", "--prune=now")
    assert w.exists(seen)
