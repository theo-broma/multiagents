"""Adversary pass: protected paths whose names git allows but the readonly
globs do not match.

`config.matches_any` compiles `**` / `*` to regexes whose `.` stops at a
newline, so a tracked file named with an embedded newline under a protected
directory is not selected, and capture keeps an edit to it unrecorded. This
predates the provenance change (RP5 keeps the restore rules otherwise as
they were); it is a hole in the boundary RP protects. Capture itself handles
such names (it lists and restores with `-z` and literal pathspecs): the
other odd names here are restored.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rp_readonly_restore_provenance import done, h  # noqa: E402,F401

NEWLINE = "tests/a\nb.py"


def entry(h, ref, path):
    out = h.git("--literal-pathspecs", "ls-tree", "-z", ref, "--", path).rstrip("\0")
    return out.split("\t")[0] if out else None

ODD = ["tests/-n", "tests/*", "tests/:(glob)z"]


def test_rp5_an_edit_to_a_protected_file_with_a_newline_in_its_name_is_restored(h):
    seed = h.launch([])
    h.write(seed, {NEWLINE: "strict\n", **{p: "strict\n" for p in ODD}})
    seed_commit = done(h, h.finish(seed))["commit"]
    impl = h.launch(["tests/**"], inputs=[seed.id])
    assert entry(h, impl.input, NEWLINE) == entry(h, seed_commit, NEWLINE) is not None
    h.write(impl, {NEWLINE: "weakened\n", **{p: "weakened\n" for p in ODD}})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    for path in ODD:
        assert entry(h, generation["commit"], path) == entry(h, seed_commit, path) is not None, path
    assert entry(h, generation["commit"], NEWLINE) == entry(h, seed_commit, NEWLINE), \
        f"edit kept; reverted={result.get('readonly_reverted')!r}"
    assert NEWLINE in result["readonly_reverted"]
