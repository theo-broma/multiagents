"""Adversary pass on readonly restore by provenance (RP1-RP3, Decision 3).

A trusted source reachable from the tip and not from the input may still be
*older* than the input on a protected path: the base branch after it moved,
or a sibling generation cut before the input's own change. Its tree then
carries the pre-change entry (or no entry at all), and a run that writes
that entry back - an edit of its own, after a merge that did not bring it -
matches the source and is kept.

Decision 3 says entry equality "tells 'merged unchanged' from 'merged then
edited'"; RP3 says nothing else is trusted. In each case below the merge
leaves the protected path at the input's entry, and the run's own commit
changes it afterwards, so the change is an own change (RP2) that must be
restored. They fail now: capture keeps the edit and records it as
`readonly_kept`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rp_readonly_restore_provenance import TEST, done, h, merged  # noqa: E402,F401


def given_tester_input(h, files):
    tester = h.generation(files, readonly=["src/**"])
    owner = next(n["id"] for n in h.nodes().values() if any(g["commit"] == tester for g in n["generations"]))
    return tester, owner


def assert_merge_left_the_input_entry(h, impl, path):
    assert h.sh(impl, f"git ls-tree HEAD -- {path}").split("\t")[0] == (h.entry(impl.input, path) or "")


def test_rp2_rp3_reverting_the_inputs_test_to_the_moved_base_version_is_restored(h):
    """The implementer's input is the tester's generation. The base branch
    moves on with an unrelated commit (it still carries the old test). The
    implementer merges the base branch - the merge keeps the tester's test -
    then writes the old test back. Capture keeps it with source = base."""
    tester, owner = given_tester_input(h, {TEST: "strict test from tester\n"})
    impl = h.launch(["tests/**"], inputs=[owner])
    assert h.blob(impl.input, TEST) == "strict test from tester"
    main = h.base_commit({"unrelated.txt": "u\n"})
    merged(h, impl, main)
    assert_merge_left_the_input_entry(h, impl, TEST)
    h.write(impl, {TEST: "v0\n"}, "implementer puts back the weak test")
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "strict test from tester", \
        f"own edit kept: {result.get('readonly_kept')}"
    assert result["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result


def test_rp2_rp3_deleting_a_test_the_input_added_is_restored(h):
    """The tester adds a new test; the base branch moves on without it. The
    implementer merges the base branch and deletes the tester's new test:
    the base commit has no entry for it, the tip has none, so they 'match'."""
    new = "tests/test_new.py"
    tester, owner = given_tester_input(h, {new: "new strict test\n"})
    impl = h.launch(["tests/**"], inputs=[owner])
    main = h.base_commit({"unrelated.txt": "u\n"})
    merged(h, impl, main)
    assert_merge_left_the_input_entry(h, impl, new)
    h.sh(impl, f"git rm -q {new} && git commit -q -m 'drop the inconvenient test'")
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.entry(generation["commit"], new) == h.entry(tester, new), \
        f"own deletion kept: {result.get('readonly_kept')}"
    assert result["readonly_reverted"] == [new]
    assert "readonly_kept" not in result


def test_rp2_rp3_reverting_via_an_unrelated_sibling_generation_is_restored(h):
    """No base move needed: any recorded generation that is not an ancestor
    of the input and still carries the old entry vouches for the revert."""
    sibling = h.generation({"sibling.txt": "s\n"}, readonly=["tests/**"])
    assert h.blob(sibling, TEST) == "v0"
    tester, owner = given_tester_input(h, {TEST: "strict test from tester\n"})
    impl = h.launch(["tests/**"], inputs=[owner])
    merged(h, impl, sibling)
    assert_merge_left_the_input_entry(h, impl, TEST)
    h.write(impl, {TEST: "v0\n"}, "implementer puts back the weak test")
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "strict test from tester", \
        f"own edit kept: {result.get('readonly_kept')}"
    assert result["readonly_reverted"] == [TEST]

