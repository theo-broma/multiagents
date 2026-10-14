"""Adversary pass 3: coverage for RP10/RP11 branches no earlier test held.

These PASS on d64ba44d. Each kills a mutation of
src/multiagents/scheduler/results.py or engine.py that the RP suites
(tests/test_rp_*.py) and the RP5 tests let survive; the mutation is named in
each docstring.
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
from test_rp_second_amendment import capture_failed, restored  # noqa: E402


@pytest.mark.parametrize("input_side", ["b1", "b2"])
def test_rp11_criss_cross_input_matching_one_merge_base_only_does_not_let_the_source_vouch(h, input_side):
    """RP11: the input's entry differs from the entry at one merge base of
    the source and the input (two merge bases, criss-cross). The base commit
    y changed the test from both bases, so RP7 alone would let it vouch.
    Kills: RP11 checked with any() over the merge bases, or against the
    first merge base only (either order of `merge-base --all`)."""
    base = "refs/heads/" + h.world.base
    root = h.git("rev-parse", base)
    b1 = h.plumb(root, {"b1.txt": "1\n"}, "b1", [root])
    b2 = h.plumb(root, {TEST: "v1\n"}, "b2", [root])
    x = h.plumb(b1, {TEST: "v0\n" if input_side == "b1" else "v1\n", "b2.txt": "x\n"}, "x", [b1, b2])
    y = h.plumb(b2, {TEST: "v2\n", "b1.txt": "1\n"}, "y", [b2, b1])
    h.git("update-ref", base, x, root)
    impl = h.launch(["tests/**"])
    assert impl.input == x
    h.git("update-ref", base, h.plumb(y, {}, "z", [y, x]), x)
    assert sorted(h.git("merge-base", "--all", x, y).split()) == sorted([b1, b2])
    h.sh(impl, f"git merge -q --no-ff --no-commit {y} || true; printf 'v2\\n' > {TEST}; git add -A; git commit -q -m m")
    assert h.sh(impl, f"cat {TEST}") == "v2"
    restored(h, impl, TEST, impl.input)


def test_rp10_rp6_vouching_is_per_path_not_over_every_selected_source(h):
    """An adversary (`**`) merges an implementer generation that descends
    from a tester's and carries its test. The implementer vouches for src/,
    the tester for tests/. Kills: vouching drawn from the union of all
    paths' sources, which names the implementer as the test's source."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    implementer = h.launch(["tests/**"], inputs=[owner_of(h, tester)])
    h.write(implementer, {SRC: "impl\n"})
    carrier = done(h, h.finish(implementer))["commit"]
    adversary = h.launch(["**"])
    merged(h, adversary, carrier)
    h.finish(adversary)
    result = h.result(adversary)
    assert sorted(result["readonly_kept"], key=lambda k: k["path"]) == [
        {"path": SRC, "source": carrier}, {"path": TEST, "source": tester}], result


def test_rp10_rp4_the_producer_policies_used_are_persisted_with_the_decision(h):
    """Kills: `producer_policies` dropped from the persisted decision."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    h.finish(impl)
    with h.engine.store.transaction(write=False) as db:
        record = json.loads(json.dumps(attempts(db)[impl.attempt["attempt_id"]]))
    policies = record["readonly_capture"]["producer_policies"]
    assert list(policies) == [tester]
    assert list(policies[tester].values()) == [[["src/**"]]]


def test_rp10_a_generation_whose_producing_attempt_record_is_gone_fails_capture(h):
    """Kills: a run_id with no attempt record read as an exempt producer."""
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    run_id = next(g["run_id"] for n in h.nodes().values() for g in n["generations"] if g["commit"] == tester)
    with h.engine.store.transaction() as db:
        attempt_id = next(a["attempt_id"] for a in attempts(db).values() if a["run_id"] == run_id)
        db.execute("DELETE FROM attempts WHERE id=?", (attempt_id,))
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    capture_failed(h, impl)
