# TS Run B independent robustness check

Target: `agents/implementer-deep/6e59b2` at `01cffbc`. Contract: TS-R3/TS-R3a. The existing worktree was left untouched; all implementation mutations were made in a fresh archive inside this worktree and restored individually. No fixes were made.

Eight distinct changed test node IDs completed against independently chosen mutations (seven test functions, including both terminal-view parametrizations). Three author mutants were also reproduced. The author list already includes almost all changed timing tests; independence here means choosing different defects, rather than copying that list.

## Findings

1. **Same-prefix stream replacement silently disappears under a surviving tail-check mutant.** `src/multiagents/viewer.py:72`: change `return chk.read(len(tail)) == tail` to `return True`. The changed `test_follower_drops_lines_when_file_grows_after_truncation` passes. With a same-inode overwrite that retains the first 64 bytes and grows the stream, the mutant prints only the old record and final status, omitting the replacement. New generated cases use common text prefixes of 64, 128 and 256 bytes; all three fail on the intended truncation assertion under this mutant and pass on Run B.

2. **The changed crash-retry test misses a one-second threshold shift.** `src/multiagents/driver.py:770`: change `ran_for < survived` to `ran_for < survived + 1`. `test_p0_r8f_20_an_inf_min_runtime_still_retries_a_crash_past_60s` remains green. New exact-clock inputs 59.875, 60.0 and 60.125 seconds show that the mutant launches once at both 60.0 and 60.125, where two launches are expected. Both fail on the launch-count assertion. The existing fake-clock test advances 60 seconds after a real 1.5-second child, so it is still beyond the shifted threshold.

3. **The new production-default linger test does not exercise the exact deadline.** `src/multiagents/viewer.py:196`: change `> LINGER_SECONDS` to `>= LINGER_SECONDS`. `test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal` remains green. Its repeated floating-point additions to 1,000,000 do not land on exactly 60 seconds. The new rounded clock reaches 59.8, 60.0 and 60.2 exactly: the mutant exits at 60.0 rather than remaining through that poll, failing the intended at-deadline assertion.

These are demonstrated test-coverage defects, not claims that the unmutated production code has those defects. The new tests pass on Run B. No pre-existing test was edited.

## Mutant → changed test → result

Paths below are relative to `tests/`. Killed means a baseline passed and the mutant failed on the intended assertion; a timeout is never counted as a kill.

| Mutant | Test | Result |
|---|---|---|
| N1 watch default 5 -> 0.01 | `test_phase0_watchdog.py::test_p0_r2_9_wall_timeout_reaches_the_tree` | BLOCKED: unmutated baseline timed out (18 s) |
| N2 consult slack default 60 -> 0 | `test_consult_fresh_worktree.py::test_decided_lock_wait_timeout_result_carries_every_key` | BLOCKED: unmutated baseline timed out (18 s) |
| N3 wall deadline one second late | `test_d1_limit_notices.py::test_ln_c1_ln_c2_ln_c6_agent_watchdog_source[timeout-timeout]` | BLOCKED: unmutated baseline timed out (18 s) |
| N4 silence deadline one second late | `test_d1_limit_notices.py::test_ln_c1_ln_c2_ln_c6_agent_watchdog_source[silence_timeout-silence_timeout]` | BLOCKED: unmutated baseline timed out (18 s) |
| N5 crash retry threshold one second late | `test_phase0_r8f_leftovers.py::test_p0_r8f_20_an_inf_min_runtime_still_retries_a_crash_past_60s` | SURVIVED |
| N6 viewer at deadline exits early | `test_d2_view.py::test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal` | SURVIVED |
| N7 silent steer reports confirmed | `test_core.py::test_a_steer_says_whether_anything_answered` | KILLED on intended assertion |
| N8 second agent never asked to wrap up | `test_burn_rate_baseline.py::test_br_r5_each_agent_is_asked_once_not_once_per_provider` | BLOCKED: unmutated baseline timed out (18 s) |
| A1 author mirror outside lock | `test_d1_adversary.py::test_adv_a_stale_mirror_cannot_resurrect_a_cleared_notice` | KILLED on intended assertion |
| A2 author fingerprint ignored | `test_d2_adversary.py::test_follower_drops_lines_when_file_grows_after_truncation` | KILLED on intended assertion |
| A3 author viewer default 30 | `test_d2_view.py::test_tm_r3_the_view_lingers_60s_after_the_run_is_terminal` | KILLED on intended assertion |
| N9/10 final status printed before buffered stream | `test_d2_view.py::test_tm_r1_terminal_run_prints_everything_and_final_status_then_exits[flags0]` | KILLED on intended assertion |
| N9/10 final status printed before buffered stream | `test_d2_view.py::test_tm_r1_terminal_run_prints_everything_and_final_status_then_exits[flags1]` | KILLED on intended assertion |
| N11 poll replays old stream lines | `test_d2_view.py::test_tm_r1_follow_prints_new_events_then_final_status_when_the_run_ends` | KILLED on intended assertion |
| N12 fingerprint size default zero | `test_d2_adversary.py::test_follower_drops_lines_when_file_grows_after_truncation` | SURVIVED (equivalent for this input: zero retains the full tail via Python `[-0:]`; not a finding) |
| N13 notice state lock removed | `test_d1_adversary.py::test_adv_a_stale_mirror_cannot_resurrect_a_cleared_notice` | KILLED on intended assertion |
| N14 tail fingerprint ignored | `test_d2_adversary.py::test_follower_drops_lines_when_file_grows_after_truncation` | SURVIVED |

N9/10 moves the terminal-status output before buffered stream text and suppresses its later duplicate; both parametrizations fail their output-order assertion. N11 seeks the existing stream handle to zero on every poll; the followed-view test fails the once-only line count. N13 replaces the notices state `LOCK_EX` acquisition with `pass`; the two real threads leave a cleared notice active, failing `assert not alerts`. All three author spot-checks fail on the same intended assertions as the supplied evidence.

## Added tests and deadline coverage

`tests/test_ts_run_b_mutants.py` contains 11 node IDs. All 11 pass on Run B. The new tests kill N1–N6 plus N14 (N5 kills two parametrizations; N14 kills all three prefixes). The additional N1–N4 tests exercise production poll/slack defaults and watchdog before/at/after-deadline behaviour, but the corresponding old integration-test baselines timed out, so those old-test mutants cannot be classified as survived or killed here.

Boundary points: watchdog 0.875 / 1.0 / 1.125 seconds; driver 59.875 / 60.0 / 60.125 seconds; viewer 59.8 / 60.0 / 60.2 seconds. Explicit floating-point values are exactly representable for the watchdog/driver inputs; the viewer rounds each advance to six decimal places. Existing subprocess output ordering and real-thread notice serialization were retained and demonstrated by intended-assertion kills. No random seed is required; generated prefix lengths are explicitly enumerated.

Reproduce on Run B after copying the new file:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q -p no:cacheprovider --basetemp=/var/tmp/ag-8d5797-pytest tests/test_ts_run_b_mutants.py
rm -rf /var/tmp/ag-8d5797-pytest
```

In this sandbox `/var/tmp` is read-only. The actual runs used distinct `/tmp/ag-8d5797-*` basetemps outside the repository, for targeted runs only. Both Python 3.14 (the author worktree venv) and Python 3.12 encountered runner-baseline hangs; the successful checks and final validation used Python 3.12. The final validation combines the 11 new cases and eight independently checked changed cases: **19 passed in 9.09 s**. Raw outputs and source locations are in `robustness-independent-results.json` and `robustness-regression-results.json`.

## Delivery limitations

`git add tests/test_ts_run_b_mutants.py` failed: it could not create `/home/theobroma/projects/multiagents/.git/worktrees/ag-8d5797/index.lock` on a read-only filesystem. Therefore no commit could be created in this sandbox. The new files are ready for the parent to stage and commit. This conflicts with the generated task instruction that commits are freely available; it is reported rather than silently worked around.

Five old integration-test baselines were inconclusive due to sandbox hangs. Source mutations were restored and all added regression tests completed before delivery. This report does not certify every changed race/timeout test; it rejects the demonstrated coverage gaps.

VERDICT(rejected, 3): three demonstrated mutation-coverage defects; unmutated Run B passes the new regressions
