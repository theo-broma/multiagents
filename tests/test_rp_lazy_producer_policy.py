"""Readonly restore by provenance, RP13 (F206): a producer policy is read
only where it is needed.

Contract: `context/specs/readonly-restore-provenance.md`, RP13 and
Decision 12. The adversary's reproduction is
tests/test_rp_adversary3_unneeded_policy.py (commit 12cf46a6).

Capture tests use the harness of tests/test_rp_readonly_restore_provenance.py
(real git, `Engine.finished`, real attempt records).

Seams, as the earlier RP suites assume them:
- a generation's producer policy is the `readonly_paths` of the stored
  attempt record whose `run_id` is the generation's `run_id`. RP13's three
  kinds of "unreadable" are: that record deleted from the store
  (`missing_record`), saved without the key (`no_readonly_paths`), and saved
  with a value that is not a list of strings (`not_a_list`: the bare string
  "src/**", which iterates as characters).
- the persisted decision (RP4) is `readonly_capture` on the run's attempt
  record, its policies under `producer_policies` keyed by generation commit,
  as tests/test_rp_adversary3_coverage.py reads it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagents.scheduler.engine import attempts  # noqa: E402
from test_rp_amendment import owner_of  # noqa: E402
from test_rp_readonly_restore_provenance import SRC, TEST, done, h, merged  # noqa: E402,F401
from test_rp_second_amendment import capture_failed, kept, producer_attempt, restored  # noqa: E402

OTHER = "tests/test_other.py"
UNREADABLE = ["missing_record", "no_readonly_paths", "not_a_list"]


def unreadable(h, commit, kind):
    """Make the producer policy of generation `commit` unreadable."""
    if kind == "no_readonly_paths":
        producer_attempt(h, commit, lambda a: a.pop("readonly_paths"))
    elif kind == "not_a_list":
        producer_attempt(h, commit, lambda a: a.update(readonly_paths="src/**"))
    else:
        run_id = next(g["run_id"] for n in h.nodes().values() for g in n["generations"]
                      if g["commit"] == commit)
        with h.engine.store.transaction() as db:
            attempt_id = next(a["attempt_id"] for a in attempts(db).values() if a["run_id"] == run_id)
            db.execute("DELETE FROM attempts WHERE id=?", (attempt_id,))


def persisted(h, launch):
    with h.engine.store.transaction(write=False) as db:
        return json.loads(json.dumps(attempts(db)[launch.attempt["attempt_id"]]))["readonly_capture"]


# ------------------------------------------------------- not needed: captured

@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unreadable_policy_of_a_src_only_generation_does_not_fail_capture(h, kind):
    """The merged generation changed only src/m.py, which the run does not
    protect; the result changed the protected test itself. The decision on
    the test is RP2's without the generation: restored to the input."""
    other = h.generation({SRC: "impl\n"}, readonly=["tests/**"])
    unreadable(h, other, kind)
    impl = h.launch(["tests/**"])
    merged(h, impl, other)
    h.write(impl, {TEST: "own\n"})
    generation = restored(h, impl, TEST, impl.input)
    assert h.blob(generation["commit"], SRC) == "impl"
    assert not persisted(h, impl).get("producer_policies"), "an unused policy was recorded"


@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unneeded_unreadable_policy_beside_a_vouching_tester_keeps_rp1s_decision(h, kind):
    """A tester generation vouches for the test (RP1) and a second, unrelated
    generation with an unreadable policy changed only src/. The test is kept
    from the tester, and the persisted decision records the tester's policy
    and no other (RP4)."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    other = h.generation({SRC: "impl\n"}, readonly=["tests/**"])
    unreadable(h, other, kind)
    impl = h.launch(["tests/**"])
    merged(h, impl, tester, other)
    generation = kept(h, impl, TEST, tester)
    assert h.blob(generation["commit"], SRC) == "impl"
    policies = persisted(h, impl)["producer_policies"]
    assert list(policies) == [tester]
    assert list(policies[tester].values()) == [[["src/**"]]]


@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unreadable_policy_on_a_protected_path_the_result_did_not_change_is_not_needed(h, kind):
    """The generation changed tests/test_x.py, but the run wrote the input's
    version back, so that path is not changed by the result. The only changed
    path is tests/test_other.py, where the generation's entry equals its
    merge base's and can neither vouch nor supersede: restored to the input."""
    other = h.generation({TEST: "g\n", SRC: "impl\n"}, readonly=[])
    unreadable(h, other, kind)
    impl = h.launch(["tests/**"])
    merged(h, impl, other)
    h.write(impl, {TEST: "v0\n", OTHER: "own\n"})
    generation = restored(h, impl, OTHER, impl.input)
    assert h.blob(generation["commit"], TEST) == "v0"
    assert h.blob(generation["commit"], SRC) == "impl"
    assert not persisted(h, impl).get("producer_policies"), "an unused policy was recorded"


# ------------------------------------------------------------ needed: fails

@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unreadable_policy_of_a_generation_that_could_vouch_for_the_test_fails_capture(h, kind):
    """The same src/ generation, now also carrying a version of the test it
    brought (produced exempt, so the version is in its tree): it could vouch
    for the changed test, so its policy is needed (RP10)."""
    other = h.generation({SRC: "impl\n", TEST: "g\n"}, readonly=[])
    assert h.blob(other, TEST) == "g"
    unreadable(h, other, kind)
    impl = h.launch(["tests/**"])
    merged(h, impl, other)
    capture_failed(h, impl)


@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unreadable_policy_after_an_own_edit_of_a_test_it_could_vouch_for_fails_capture(h, kind):
    """The run edits the test after merging the generation: whether the
    generation vouches decides the restore target (RP2), so its policy is
    needed."""
    other = h.generation({SRC: "impl\n", TEST: "g\n"}, readonly=[])
    unreadable(h, other, kind)
    impl = h.launch(["tests/**"])
    merged(h, impl, other)
    h.write(impl, {TEST: "own\n"})
    capture_failed(h, impl)


@pytest.mark.parametrize("kind", UNREADABLE)
def test_rp13_an_unreadable_policy_of_a_generation_that_could_supersede_the_tester_fails_capture(h, kind):
    """The src/ generation descends from the tester's and carries its test:
    if allowed on tests/ it would supersede the tester there (RP7/RP10), so
    its policy is needed even though the tester alone would keep the test."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    implementer = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    h.write(implementer, {SRC: "impl\n"})
    carrier = done(h, h.finish(implementer))["commit"]
    assert h.entry(carrier, TEST) == h.entry(tester, TEST)
    unreadable(h, carrier, kind)
    later = h.launch(["tests/**"])
    merged(h, later, carrier)
    capture_failed(h, later)
