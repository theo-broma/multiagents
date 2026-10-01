# TS Run B: timing (TS-R2, TS-R2a, TS-R2b, TS-R3, TS-R3a)

Base: main at 6d6cda8. All runs: `-n auto` (16 workers), `--basetemp` under /var/tmp.

| File | What |
|---|---|
| base-main.tsv | per-id outcomes of 6d6cda8, run in the worktree (parity reference, TS-R1a) |
| after1.tsv, after2.tsv | per-id outcomes of the final tree, two runs |
| durations-before.txt | 6d6cda8, idle machine (load 0.2), 211 s wall |
| durations-before2.txt | 6d6cda8 exported outside git, loaded machine (load ~8), 236 s wall; timing only (not a git checkout, so 1 more red and 2 more skips than base-main) |
| durations-after2.txt | final tree right after before2, same load, 163 s wall |
| mutations.txt | TS-R3a kills, produced by mutate.py |

Per-test totals are setup + call + teardown. Parity: every base id present, every outcome
identical; one new id, `test_d2_view.py::test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal` (passed).

## Review r1 (ag-dc89fc), P2 fixed

The conftest host-CLI guard now decides on MULTIAGENTS_BIN as the shipped script
will see it (after the provider's `env:`/`credential_env` and the caller's
`extra_env`), not on the provider's `bin`. Pinned by
`tests/test_ts_conftest_host_cli_guard.py` (5 cases; 3 fail against the old guard).
after3.tsv: full `-n auto` run after the fix, 4545 ids, 161 s; every base id present
with the same outcome; new ids: the TM-R3 linger pin and the 5 guard tests, all passed.

## Review r2 (ag-7236bf), P2 fixed

A relative effective binary, and a bare name found through a relative or empty
PATH entry, now resolve against the action's working directory (`cwd`, else the
process's) before the basetemp check. The `models_cmd` guard uses the same
resolution, so it now also catches bare names. Three more cases in
`tests/test_ts_conftest_host_cli_guard.py` (8 total; the two refusal cases fail
against the r1 guard). after4.tsv: full `-n auto` run, 4548 ids, 166 s; every base
id present with the same outcome; 9 new ids, all passed.
