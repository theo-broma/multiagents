"""Readonly restore by provenance, second amendment: RP10-RP12, Decision 11.

Contract: `context/specs/readonly-restore-provenance.md`, section "Second
amendment (adversary pass 2, 2026-10-14, run ag-f45a34)". The adversary
committed no test file; its two reproductions are RP10's addition case and
RP11's disposed stale generation case below.

Capture tests use the harness of tests/test_rp_readonly_restore_provenance.py
(real git, `Engine.finished`, real attempt records).

Seams assumed beyond those of the RP1-RP9 suites:
- a generation's producing attempt is the stored attempt record whose
  `run_id` is the generation's `run_id`; "its readonly_paths cannot be read"
  is that record saved without the key (`save_attempt`), and "the agent's
  current configuration" is the project's agents.yaml (`readonly_paths` of
  the agent, rewritten after the run).
- "several records name the same commit" is a second generation record
  naming the commit, appended to another stored node with the `run_id` of a
  real attempt, as the RP1 suite sets `disposed`/`verdict` on a record.
- capture computes merge bases with `git merge-base`, found on PATH: "a git
  failure while computing merge bases" is a `git` wrapper first on PATH that
  fails every `merge-base` call (exit 128, a fatal message) while capture runs.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.scheduler.engine import attempts, save_attempt  # noqa: E402
from test_rp_amendment import owner_of, relaunch  # noqa: E402
from test_rp_readonly_restore_provenance import (SRC, TEST, crash_after_retention, done,  # noqa: E402,F401
                                                 h, merged)

NEW = "tests/test_new.py"


def resolved(h, launch, commit, path, side):
    """Merge `commit`, resolve the conflict on `path` to `side` (ours or
    theirs) and commit the merge."""
    h.sh(launch, f"git merge -q --no-ff --no-edit {commit} || true; "
                 f"git checkout --{side} -- {path} && git add -- {path} && git commit -q --no-edit")


def kept(h, launch, path, source):
    checkout_tip = h.tip(launch)
    generation = done(h, h.finish(launch))
    result = h.result(launch)
    assert result["commit"] == checkout_tip, \
        f"restored: kept={result.get('readonly_kept')} reverted={result.get('readonly_reverted')}"
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": path, "source": source}]
    assert generation["readonly_kept"] == [{"path": path, "source": source}]
    return generation


def restored(h, launch, path, expected_ref):
    generation = done(h, h.finish(launch))
    result = h.result(launch)
    assert h.entry(generation["commit"], path) == h.entry(expected_ref, path), \
        f"kept={result.get('readonly_kept')} reverted={result.get('readonly_reverted')}"
    assert result["readonly_reverted"] == [path]
    assert generation["readonly_reverted"] == [path]
    assert "readonly_kept" not in result and "readonly_kept" not in generation
    return generation


def capture_failed(h, launch):
    node = h.finish(launch)
    result = h.result(launch)
    assert node["generations"] == [], "a result was integrated"
    assert node["state"] == "held", node["state"]
    assert result.get("capture_error")
    assert h.world.tip(launch.id) in (None, launch.input)


def producer_attempt(h, commit, change):
    """Apply `change` to the stored attempt that produced generation `commit`."""
    run_id = next(g["run_id"] for n in h.nodes().values() for g in n["generations"] if g["commit"] == commit)
    with h.engine.store.transaction() as db:
        record = next(json.loads(json.dumps(a)) for a in attempts(db).values() if a["run_id"] == run_id)
        change(record)
        save_attempt(db, record)


def weak_addition_then_strict_input(h):
    """A non-exempt node adds NEW=weak (an addition, never restored at its own
    capture); the tester adds NEW=strict; the implementer's input is the
    tester's generation."""
    weak = h.generation({NEW: "weak\n"}, readonly=["tests/**"])
    assert h.blob(weak, NEW) == "weak", "precondition: additions are not restored"
    tester = h.generation({NEW: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    assert h.blob(impl.input, NEW) == "strict"
    return weak, tester, impl


# ---------------------------------------------------------------------- RP10

def test_rp10_an_addition_by_a_node_protected_there_does_not_vouch_over_the_input(h):
    """The adversary's reproduction: add/add resolved to the unvetted entry."""
    weak, tester, impl = weak_addition_then_strict_input(h)
    resolved(h, impl, weak, NEW, "theirs")
    assert h.sh(impl, f"cat {NEW}") == "weak"
    generation = restored(h, impl, NEW, tester)
    assert h.blob(generation["commit"], NEW) == "strict"


def test_rp10_the_producer_policy_is_the_attempts_at_launch_not_the_current_config(h):
    """After the weak node ran, its agent is made exempt in the config: the
    attempt's recorded `tests/**` still forbids its generation to vouch."""
    weak, tester, impl = weak_addition_then_strict_input(h)
    h.world.agents["guard"]["readonly_paths"] = []
    h.world.write_config()
    resolved(h, impl, weak, NEW, "theirs")
    restored(h, impl, NEW, tester)


def test_rp10_a_config_that_forbids_the_producer_now_does_not_withdraw_its_vouching(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    h.world.agents["guard"]["readonly_paths"] = ["**"]
    h.world.write_config()
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    kept(h, impl, TEST, tester)


def test_rp10_an_implementer_generation_still_vouches_for_src_under_an_adversarys_globstar(h):
    implementer = h.generation({SRC: "impl\n"}, readonly=["tests/**"])
    adversary = h.launch(["**"])
    merged(h, adversary, implementer)
    h.write(adversary, {"notes.txt": "n\n"})
    generation = kept(h, adversary, SRC, implementer)
    assert h.blob(generation["commit"], SRC) == "impl"


def test_rp10_an_implementer_generation_carrying_a_testers_test_does_not_supersede_the_tester(h):
    """The implementer's generation descends from the tester's and inherits
    its test. It may not vouch for tests/, so it is removed for that path
    before supersession, and the tester's generation still vouches."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    implementer = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    h.write(implementer, {SRC: "impl\n"})
    carrier = done(h, h.finish(implementer))["commit"]
    assert h.git("merge-base", "--is-ancestor", tester, carrier) == ""
    assert h.entry(carrier, TEST) == h.entry(tester, TEST)
    later = h.launch(["tests/**"])
    merged(h, later, carrier)
    assert h.sh(later, f"cat {TEST} {SRC}").split() == ["strict", "impl"]
    kept(h, later, TEST, tester)


def test_rp10_a_missing_producer_policy_needed_for_a_decision_fails_capture(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    producer_attempt(h, tester, lambda a: a.pop("readonly_paths"))
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    capture_failed(h, impl)


def test_rp10_a_missing_producer_policy_of_a_generation_not_merged_is_not_needed(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    producer_attempt(h, tester, lambda a: a.pop("readonly_paths"))
    impl = h.launch(["tests/**"])
    h.write(impl, {TEST: "own\n", SRC: "impl\n"})
    restored(h, impl, TEST, impl.input)


def test_rp10_a_commit_named_by_several_records_vouches_only_where_all_allow_it(h):
    """The tester's generation commit is also named by a generation record
    whose attempt protects tests/: it no longer vouches for the test."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    implementer = h.launch(["tests/**"])
    h.write(implementer, {SRC: "impl\n"})
    other = h.finish(implementer)
    record = done(h, other)
    h.update_node(implementer.id, generations=[*other["generations"], {
        **record, "seq": record["seq"] + 1, "commit": tester}])
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    restored(h, impl, TEST, impl.input)


def test_rp10_rp4_the_producer_policies_used_are_replayed_with_the_decision(h, monkeypatch):
    """Decided once with the tester allowed on tests/; the tester's attempt
    then loses its policy. The replay reuses the persisted decision rather
    than failing or deciding afresh."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    checkout_tip = h.tip(impl)
    ref = crash_after_retention(monkeypatch, impl)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        h.engine.finished(impl.attempt, impl.run)
    assert h.git("rev-parse", ref) == checkout_tip
    producer_attempt(h, tester, lambda a: a.update(readonly_paths=["**"]))
    kept(h, impl, TEST, tester)


# ---------------------------------------------------------------------- RP11

def test_rp11_a_disposed_stale_generation_resolved_against_the_input_is_restored(h):
    """The adversary's reproduction: input v0->strict, a disposed sibling
    v0->weak (allowed on tests/), merged and resolved to weak."""
    stale = h.generation({TEST: "weak\n"}, readonly=["src/**"])
    h.update_node(owner_of(h, stale), disposed="2026-10-14T00:00:00Z")
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    resolved(h, impl, stale, TEST, "theirs")
    assert h.sh(impl, f"cat {TEST}") == "weak"
    generation = restored(h, impl, TEST, tester)
    assert h.blob(generation["commit"], TEST) == "strict"


def test_rp11_a_base_commit_that_changed_the_inputs_test_is_restored_to_the_input(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    main = h.base_commit({TEST: "from main\n"})
    resolved(h, impl, main, TEST, "theirs")
    assert h.sh(impl, f"cat {TEST}") == "from main"
    restored(h, impl, TEST, impl.input)


def test_rp11_a_clean_concurrent_merge_with_the_base_is_restored_to_the_input_not_to_main(h):
    """Decision 11: main and the input both changed the file, on different
    lines; git merges them cleanly. Main does not vouch, so the restore
    target is the input's entry, not main's."""
    lines = "a\nb\nc\nd\ne\n"
    h.base_commit({TEST: lines})
    tester = h.generation({TEST: lines.replace("a\n", "A\n")}, readonly=["src/**"])
    impl = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    # The input's change sits on a base that already had the five lines.
    assert h.blob(impl.input, TEST).split() == ["A", "b", "c", "d", "e"]
    main = h.base_commit({TEST: lines.replace("e\n", "E\n")})
    merged(h, impl, main)
    assert h.sh(impl, f"cat {TEST}").split() == ["A", "b", "c", "d", "E"]
    restored(h, impl, TEST, impl.input)


def test_rp11_a_tester_round_two_descending_from_the_input_is_kept(h):
    first = h.launch(["src/**"])
    h.write(first, {TEST: "strict 1\n"})
    g1 = done(h, h.finish(first))["commit"]
    impl = h.launch(["tests/**"], inputs=[first.id])
    assert impl.input == g1
    second = relaunch(h, first, readonly=["src/**"])
    assert second.input == g1
    h.write(second, {TEST: "strict 2\n"})
    node = h.finish(second)
    assert node["state"] == "done" and len(node["generations"]) == 2, node.get("hold")
    g2 = node["generations"][-1]["commit"]
    merged(h, impl, g2)
    h.write(impl, {SRC: "impl\n"})
    generation = kept(h, impl, TEST, g2)
    assert h.blob(generation["commit"], TEST) == "strict 2"


# ---------------------------------------------------------------------- RP12

def test_rp12_a_source_with_no_merge_base_with_the_input_vouches_for_nothing(h):
    """The base branch is replaced by an unrelated root commit carrying the
    whole project with a changed test."""
    impl = h.launch(["tests/**"])
    start = h.git("rev-parse", h.world.base)
    orphan = h.plumb(start, {TEST: "orphan\n"}, "unrelated root", [])
    h.git("update-ref", "refs/heads/" + h.world.base, orphan, start)
    assert h.sh(impl, f"git merge-base {orphan} HEAD || echo none") == "none"
    h.sh(impl, f"git merge -q --no-edit --allow-unrelated-histories -X theirs {orphan}")
    assert h.sh(impl, f"cat {TEST}") == "orphan"
    restored(h, impl, TEST, impl.input)


@pytest.fixture
def failing_merge_base(tmp_path, monkeypatch):
    """Arm a `git` on PATH whose `merge-base` fails; returns the arming call."""
    real = shutil.which("git")
    bin_dir = tmp_path / "failing-git"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text("#!/bin/sh\n"
                       'for a in "$@"; do [ "$a" = merge-base ] && '
                       '{ echo "fatal: injected merge-base failure" >&2; exit 128; }; done\n'
                       f'exec {real} "$@"\n')
    wrapper.chmod(0o755)
    return lambda: monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_rp12_a_git_failure_computing_merge_bases_fails_capture(h, failing_merge_base):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    failing_merge_base()
    capture_failed(h, impl)


def test_rp12_an_older_base_candidate_is_superseded_by_a_newer_one_on_the_chain(h):
    """Main moves v0->b1, then b1->b2. The run merges main and writes b1
    back: the older base commit is an ancestor of the newer and is dropped,
    so only b2 vouches, and the path is restored to b2."""
    impl = h.launch(["tests/**"])
    older = h.base_commit({TEST: "b1\n"})
    newer = h.base_commit({TEST: "b2\n"})
    merged(h, impl, newer)
    h.write(impl, {TEST: "b1\n"}, "impl writes the older main version")
    generation = restored(h, impl, TEST, newer)
    assert h.blob(generation["commit"], TEST) == "b2"
    assert h.entry(older, TEST) != h.entry(newer, TEST)


def test_rp12_merging_the_newer_base_keeps_it_as_the_source(h):
    impl = h.launch(["tests/**"])
    h.base_commit({TEST: "b1\n"})
    newer = h.base_commit({TEST: "b2\n"})
    merged(h, impl, newer)
    kept(h, impl, TEST, newer)


@pytest.mark.parametrize("order", ["first_then_second", "second_then_first"])
def test_rp12_rp6_the_source_between_two_incomparable_generations_is_first_in_topo_order(h, order):
    first = h.generation({TEST: "shared\n"}, readonly=["src/**"])
    second = h.generation({TEST: "shared\n", "s.txt": "s\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    assert h.sh(impl, f"git merge-base --is-ancestor {first} {second} || echo no") == "no"
    assert h.sh(impl, f"git merge-base --is-ancestor {second} {first} || echo no") == "no"
    merged(h, impl, *((first, second) if order == "first_then_second" else (second, first)))
    tip = h.tip(impl)
    topo = h.sh(impl, f"git rev-list --topo-order {tip}").split()
    expected = next(c for c in topo if c in {first, second})
    kept(h, impl, TEST, expected)


@pytest.mark.parametrize("order", ["generation_then_base", "base_then_generation"])
def test_rp12_rp6_an_incomparable_base_commit_is_the_source_over_a_generation(h, order):
    impl = h.launch(["tests/**"])
    tester = h.generation({TEST: "shared\n"}, readonly=["src/**"])
    main = h.base_commit({TEST: "shared\n"})
    assert h.sh(impl, f"git merge-base --is-ancestor {tester} {main} || echo no") == "no"
    assert h.sh(impl, f"git merge-base --is-ancestor {main} {tester} || echo no") == "no"
    merged(h, impl, *((tester, main) if order == "generation_then_base" else (main, tester)))
    kept(h, impl, TEST, main)
