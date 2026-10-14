"""Adversary pass 3 on readonly restore by provenance: RP10's fail-closed rule.

RP10: "When a generation is needed for a decision and its producing
attempt's readonly_paths cannot be read, capture fails." Capture fails today
when ANY generation reachable from the tip and not from the input lacks its
producer policy, including one that changed no protected path and so can
neither vouch for nor supersede anything: the run is held for a decision
that never needed that generation.

Seams as in tests/test_rp_second_amendment.py ("cannot be read" is the
producing attempt record saved without `readonly_paths`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rp_readonly_restore_provenance import SRC, TEST, h, merged  # noqa: E402,F401
from test_rp_second_amendment import producer_attempt, restored  # noqa: E402


@pytest.mark.parametrize("files", [{"docs/x.md": "d\n"}, {SRC: "impl\n"}], ids=["docs_only", "src_only"])
def test_rp10_a_merged_generation_that_touches_no_changed_protected_path_is_not_needed(h, files):
    """The merged generation changed nothing at tests/test_x.py and has no
    eligible ancestor in the run's history: it cannot vouch for, or
    supersede a source on, the only changed path. The run's own test edit is
    restored as today (RP2); capture must not fail."""
    other = h.generation(files, readonly=["tests/**"])
    producer_attempt(h, other, lambda a: a.pop("readonly_paths"))
    impl = h.launch(["tests/**"])
    merged(h, impl, other)
    h.write(impl, {TEST: "own\n"})
    restored(h, impl, TEST, impl.input)
