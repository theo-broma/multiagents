"""F100's proof test (context/review/C2-provider.md), inverted now it is fixed.

This file was written to demonstrate the finding: `budget._cache` was keyed on
provider NAME alone — not on config_dir, executor, or anything else that
distinguishes one caller's provider from another's — so two callers that both
happened to use the name "claude" (a very likely collision, since it is the
name every real characterizer will reach for) read each other's cached
`Budget`, entirely by accident of collection order. `pytest-randomly` is active
in this suite, so that order is not even stable across runs.

F100 was fixed under context/specs/phase1-budget-cache.md (R10), so the fact
this file records has changed: the bleed no longer happens, and the test below
now asserts the opposite of what it originally did. Same provider name, same
two config_dirs, same two scripts — inverted assertion, and renamed to say what
is now true. This is a deliberate inversion, not a weakened test: the second
caller is checked to get its OWN config's value, which is a strictly stronger
claim than the contaminated one it used to make.

This file still clears the cache itself on the way in and out (see
tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated
for the same pattern), because that is this suite's convention, not because the
key is still wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


def test_two_unrelated_tests_sharing_a_provider_name_each_read_their_own_config(tmp_path):
    h.invalidate_cache()
    try:
        # "Test one": some earlier test in the suite reads budget for a
        # provider it names "claude", using its own script and its own
        # config_dir.
        first_dir = tmp_path / "first-test-config"
        provider = h.make_provider("claude")
        h.case_script(
            first_dir, "claude.sh",
            'budget) printf \'{"known": true, "headroom": 0.9}\'; exit 0 ;;',
        )
        first = h.read_provider("claude", provider, h.FakeExecutor(), first_dir)
        assert first.headroom == 0.9

        # "Test two": a completely unrelated later test, in a different
        # tmp_path, ALSO happens to name its provider "claude" — the obvious
        # choice, since that is the real shipped provider name. It never
        # touched test one's script or config_dir.
        second_dir = tmp_path / "second-test-config"
        h.case_script(
            second_dir, "claude.sh",
            'budget) printf \'{"known": true, "headroom": 0.1}\'; exit 0 ;;',
        )
        second = h.read_provider("claude", provider, h.FakeExecutor(), second_dir)

        # Test two's own script runs and reports 0.1. Before F100 was fixed
        # this was 0.9 — test one's cached Budget, served to a caller whose
        # own script never ran.
        assert second.headroom == 0.1, (
            "a second caller sharing a provider name but reading a different "
            "config_dir must get its own script's answer, not the first "
            "caller's cached Budget"
        )
        # And test one's value is not clobbered either: going back to the
        # first config_dir still reports what that config says.
        again = h.read_provider("claude", provider, h.FakeExecutor(), first_dir)
        assert again.headroom == 0.9
    finally:
        h.invalidate_cache()
