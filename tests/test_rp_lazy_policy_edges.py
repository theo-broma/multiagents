"""RP13: base authority and non-vouching supersession still apply lazily."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rp_amendment import owner_of  # noqa: E402
from test_rp_lazy_producer_policy import persisted, unreadable  # noqa: E402
from test_rp_readonly_restore_provenance import TEST, done, h, merged  # noqa: E402,F401
from test_rp_second_amendment import capture_failed, kept  # noqa: E402


def test_rp13_a_base_first_parent_commit_also_recorded_as_a_generation_needs_no_policy(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    unreadable(h, tester, "missing_record")
    h.git("update-ref", "refs/heads/" + h.world.base, tester, impl.input)
    merged(h, impl, tester)
    kept(h, impl, TEST, tester)
    assert not persisted(h, impl)["producer_policies"]


def test_rp13_a_revert_that_cannot_vouch_still_needs_policy_to_supersede(h):
    tester = h.generation({TEST: "strict\n"}, readonly=["src/**"])
    reverter = h.launch(["src/**"], inputs=[owner_of(h, tester)])
    h.write(reverter, {TEST: "v0\n"})
    reverted = done(h, h.finish(reverter))["commit"]
    unreadable(h, reverted, "no_readonly_paths")
    impl = h.launch(["tests/**"])
    merged(h, impl, reverted)
    h.write(impl, {TEST: "own\n"})
    capture_failed(h, impl)
