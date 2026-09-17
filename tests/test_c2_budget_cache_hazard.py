"""Reproduction for F100 (context/review/C2-provider.md).

`budget._cache` is a plain module-level dict keyed by provider NAME alone —
not by config_dir, executor, or anything else that distinguishes one test's
provider from another's. Two tests that both happen to use the provider name
"claude" (a very likely collision, since it is the name every real characterizer
will reach for) can therefore read each other's cached `Budget`, entirely by
accident of collection order. `pytest-randomly` is active in this suite, so
that order is not even stable across runs.

This file clears the cache itself on the way in and out (see
tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated
for the same pattern) precisely BECAUSE nothing else does — that absence is
the finding.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402


def test_two_unrelated_tests_sharing_a_provider_name_bleed_through_the_cache(tmp_path):
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

        # What SHOULD happen: test two's own script runs and reports 0.1.
        # What ACTUALLY happens: budget.read_provider's cache is keyed only
        # on the string "claude", so test two silently receives test one's
        # cached Budget instead of ever invoking its own script.
        assert second.headroom == 0.9, (
            "reproduced the hazard: read_provider served test one's cached "
            "Budget to test two, which never ran its own script"
        )
    finally:
        h.invalidate_cache()
