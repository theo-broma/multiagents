# Test suite

Run the full suite chunk by chunk with `scripts/test-chunk.sh K N`; chunking
avoids the slow or timed-out single pytest process that agents otherwise hit.
Use `scripts/test-chunk.sh --list K N` to print a chunk's test-file list.

If this worktree's `.venv` lacks pytest, run:
`PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider $(scripts/test-chunk.sh --list K N)`.

About 72 reds in `tests/test_phase2_*` are known and by design, pending the
deferred phase 2 review; they are not regressions. Never `pkill` pytest by
pattern: other agents run suites concurrently on this machine.
