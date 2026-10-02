# Test suite

Fast path: run the whole suite in one process with pytest-xdist, about
3.5 minutes on the 16-core machine with `-n auto` instead of about 45
serially. Use `-n 4`, not `-n auto`: up to six agents run suites at the same
time on these 16 cores, and `-n auto` from each of them starves the others
into timeouts.

    scripts/test-chunk.sh 1 1 -q -p no:cacheprovider -n 4 --basetemp=/var/tmp/<your-agent-id>-pytest
    rm -rf /var/tmp/<your-agent-id>-pytest

or, without a `.venv`,
`PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 --basetemp=... tests/`.
The per-id outcomes match a serial run (TS-R1; manifests in
`context/ts/manifests/`). A test that patches a module global must use
`monkeypatch`, never bare assignment: under xdist the leak lands in an
unrelated test on the same worker.

A parallel run writes about 1.5 GB of temp dirs, and `/tmp` is a 14 GB tmpfs
shared by every agent, so put `--basetemp` on real disk. It must be a path of
your own (pytest empties it at start) and **outside any git repository**: many
tests use `tmp_path` as a project that is not a repository, and a basetemp
inside the worktree turns 53 of them red.

Otherwise run the full suite chunk by chunk with `scripts/test-chunk.sh K N`; chunking
avoids the slow or timed-out single pytest process that agents otherwise hit.
Use `scripts/test-chunk.sh --list K N` to print a chunk's test-file list.

If this worktree's `.venv` lacks pytest, run:
`PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider $(scripts/test-chunk.sh --list K N)`.

About 72 reds in `tests/test_phase2_*` are known and by design, pending the
deferred phase 2 review; they are not regressions. Never `pkill` pytest by
pattern: other agents run suites concurrently on this machine.
