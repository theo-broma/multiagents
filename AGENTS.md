# Test suite

Fast path: run the whole suite in one process with pytest-xdist, about
50–53 minutes wall with `-n 4` on the shared 16-core machine (measured
2026-10-14: 3163 s, about 12,650 worker-seconds). Use `-n 4`, not `-n auto`:
up to six agents run suites at the same time on these 16 cores, and `-n auto`
from each of them starves the others
into timeouts. A full run takes close to an hour.

**Agents never run the full suite, nor every chunk of it** (plan
2026-10-17-test-tiers).
- Implementers and reviewers run what their change touches:
  - the changed and added test files;
  - the test files that import the changed modules (find them with `rg`);
  - the test families of the subsystem they changed, for example every
    `test_ap_*` for an auth-proxy change.
- **Tier 2, before handing back** (user, 2026-10-18; plan 2026-10-17-test-tiers, risk
  threshold reached). Implementers also run:
  - the test files of the modules that import a changed module, one level
    deep, found with `rg`;
  - whenever the change touches `scheduler/`, `runner.py`, `server.py` or
    `executor/`, these cross-cutting families: `tests/test_core.py`,
    `tests/test_nc_m*.py`, `tests/test_st6*.py` and `tests/test_tb*.py`.
- They report exactly what they ran.
- The orchestrator runs ONE full suite per final candidate tree, including
  the tester's files, from a `/var/tmp` worktree with `multiagents bg run`.
  Any later change to the code or the tests invalidates that result.

    scripts/test-chunk.sh 1 1 -q -p no:cacheprovider -n 4 --basetemp=/var/tmp/pt-<hex-part-of-your-id>

or, without a `.venv`,
`PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 --basetemp=... tests/`.
The per-id outcomes match a serial run (TS-R1; manifests in
`context/ts/manifests/`). A test that patches a module global must use
`monkeypatch`, never bare assignment: under xdist the leak lands in an
unrelated test on the same worker.

The script cleans up the effective basetemp after pytest exits, including
failure and interruption. It resolves symlinks and removes only directories
directly under /var/tmp named pt-* and outside any git repository; other
paths are kept with a notice. Direct pytest invocations manage their own cleanup.

A parallel run writes about 1.5 GB of temp dirs, and `/tmp` is a 14 GB tmpfs
shared by every agent, so put `--basetemp` on real disk. It must be a path of
your own (pytest empties it at start) and **outside any git repository**: many
tests use `tmp_path` as a project that is not a repository, and a basetemp
inside the worktree turns 53 of them red. Leave the `ag-` prefix out of the
path: `test_core.py::test_uninstall_prunes_the_registrations_it_orphans`
asserts that `ag-1` is absent from a listing that includes the basetemp.

The orchestrator may also run its full suite chunk by chunk with
`scripts/test-chunk.sh K N`; chunking avoids a slow or timed-out single
pytest process.
Use `scripts/test-chunk.sh --list K N` to print a chunk's test-file list.

If this worktree's `.venv` lacks pytest, run:
`PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider $(scripts/test-chunk.sh --list K N)`.

Changes to the nc/c23/pc fixtures must be checked with `MULTIAGENTS_SL_FULL=1`
(whole-scope leak test SL-R3, about 13 min) before merging.

About 72 reds in `tests/test_phase2_*` are known and by design, pending the
deferred phase 2 review; they are not regressions. Never `pkill` pytest by
pattern: other agents run suites concurrently on this machine.

For root sessions, start commands expected to run longer than a few minutes
with `multiagents bg run --owner ROLE -- CMD ARG...` (ROLE is orchestrator or
initializer), instead of `run_in_background`. The Stop hook normally delivers
a notification when the command ends. The hook does not fire after a user
interrupt or an API error stop: at every wake also read `multiagents bg list`
for unacknowledged ends. Once an end has been handled, acknowledge it with
`multiagents bg ack ID`. Jobs survive root-session restarts; their log paths
are printed by `bg run` and included in notifications.
