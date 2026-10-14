# TS: a faster test suite with the same guarantees, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.

**Source:**
- the user asked whether the full suite could take less time;
- the orchestrator measured it on main at fd2fae0 (scratchpad/durations);
- researcher ag-613241 read the code.

**Who does it (the user's decision):** claude opus as the implementer (implementer-deep pinned to `opus`), consulting `gemini-advisor` (agy gemini-3.1-pro-high).

**Ids:** `TS-R*`, which are never renumbered.

## Measurements (sequential, 16 cores available, pytest-xdist not installed)

| Chunk | Wall time |
|---|---|
| 1 of 4 | 685 s |
| 2 of 4 | 537 s |
| 3 of 4 | 841 s |
| 4 of 4 | 712 s |
| **Total** | **≈ 2775 s ≈ 46 min** |

- About 4,200 tests.
- The 240 slowest tests (≥ 1 s each) account for 1800 s, i.e. 65 % of the total.
- Five tests take about **60 s each**, which looks like a timeout waited out:
  - `test_consult_fresh_worktree.py::test_decided_lock_wait_timeout_result_carries_every_key`;
  - `test_phase0_r8f_leftovers.py::test_p0_r8f_20_an_inf_min_runtime_still_retries_a_crash_past_60s`;
  - `test_d2_view.py::test_tm_r1_follow_prints_new_events_then_final_status_when_the_run_ends`;
  - `test_d2_view.py::test_tm_r1_terminal_run_prints_everything_and_final_status_then_exits[flags1]`;
  - `test_d2_adversary.py::test_follower_drops_lines_when_file_grows_after_truncation`.
- Other large items:
  - `test_d1_adversary` adopted-run provenance, 36 s;
  - `test_h1h3_round2_clean`, 23 s;
  - `test_agent_survival` sv_r8, 21 s;
  - `test_sandbox_git_branch_delete`, 17–19 s of setup each;
  - a family of `test_d1_limit_notices` / `test_d1_review_findings` rows at 14–16 s each;
  - the watchdog and config-reload tests at 13–17 s.

## Behaviours

**TS-R1: the suite runs in parallel.**
- **The dependency.** `pytest-xdist` is a dev dependency, in pyproject and uv.lock.
- **The results.** `pytest -n auto` over the whole suite gives the same pass/fail/skip/xfail set as a serial run. Order-dependent or shared-state tests are fixed so that this holds.
- **The chunk script.** `scripts/test-chunk.sh` keeps working, and gains a way to pass `-n` through.
- **Documentation.** AGENTS.md documents the fast path.
- Verified by: a serial and a parallel run on the same commit produce the same outcome set. Attach both summaries to the result.

**TS-R2: no test waits out real time it could simulate.**
- **What to replace.** A test that sleeps, or waits for a watchdog, timeout, poll interval or cooldown in real time, uses instead:
  - an injected or fake clock;
  - an event or barrier;
  - or a test-sized interval set through existing configuration.
- **Target.** No single test above 5 s, apart from the exceptions listed and justified in the result, each with the reason it needs real time.
- **The five 60 s tests** must be understood first: they may be a real hang that hits a 60 s cap, which would be a finding and not a speed issue. Report which.
- Verified by: `--durations=30` before and after, attached.

**TS-R3: the guarantees do not weaken. This overrides every speed goal.**
- **What each modified test must keep:** the behaviour it asserts, and its requirement id in its name.
- **What must not be removed:** no assertion, no test, and no parametrised case, unless it is exactly duplicated. A removal is listed with the duplicate it matches.
- **Mutations.** A test that used real time to exercise a race or a timeout must still fail against the defect it guards. For the five largest changes, show this with a mutation: revert the guarded fix in scratch, check the test goes red, restore it.
- **No source changes to make a test faster**, except test seams: injectable clocks and intervals, with defaults unchanged in production.
- **Test edits are authorised** for this job only. Tests are otherwise read-only for implementers.
- Verified by:
  - a review by `reviewer`;
  - a mutation check by `robustness-tester` on the modified tests.

**TS-R4: the setup cost.**
- **Where it applies.** Where heavy per-test setup (git init, worktrees, config seeding) is identical across a module's tests.
- **What to use.** A module- or session-scoped fixture, **only** where the tests do not mutate the shared object. Isolation is worth more than seconds here.
- Verified by: the durations, and TS-R1's parity.

**TS-R5: the target.**
- **Main aim.** The full suite runs under **10 minutes** with `-n auto` on this 16-core machine.
- **Secondary aim.** A serial run drops substantially, **below 30 minutes**.
- **If the targets are missed,** report what is left and why.

## Known reds, which must stay exactly as they are

- The 72 in `tests/test_phase2_*`, by design.
- `test_h1_h2_review2.py::test_rf_r3_r1_adopted_opencode_filter_without_exit_status_is_refused`, which already failed before.
- PS's reds on main until PS merges. **Base the work on main after M merges.**

## Amendments of 2026-09-30, after the advisor (ag-d20e1e, turn 4)

**TS-R0: the work is split into runs.**
- **Run A: xdist, isolation and parity (TS-R1).**
  - It ends with outcome manifests: per node id, the outcome for the untouched base, for a serial run and for a parallel run, plus a second parallel run.
  - The orchestrator reviews them before Run B starts.
- **Run B: timing (TS-R2 and TS-R3).**
- **Run C: fixture reuse (TS-R4),** only if setup is still a measured bottleneck after B.

**TS-R1a: parity is measured against the base.**
- **What is compared.** The collected node ids and the per-id outcomes are compared with the **untouched base** commit, not only serial against parallel on the modified commit.
- **The known reds.** The list is captured as exact ids after M merges. It replaces the "72" estimate.

**TS-R3a: mutation coverage.**
- **Every changed guarantee.** Each changed test that guards a race, a timeout or a deadline gets a demonstrated kill. It must fail on its intended assertion, not on an unrelated exception.
- **Deadline behaviour.** Before-deadline, at-deadline and after-deadline behaviour is preserved, and so is real ordering under concurrency. A fake sleep must yield control, and shortening an interval must not replace testing the production default where the default is the behaviour under test.

**TS-R2a: representative real integration tests stay.**
- **What stays.** Some subprocess and concurrency tests keep their real-time integration form.
- **Not every 60 s test is a hang.** For example, `test_d2_view.py` ~356 waits out TM-R3's ~60 s grace period on purpose. Such a test is either kept or given a configurable grace period with the production default unchanged.

**TS-R4a: shared fixtures.**
- **Preferred.** Immutable seed data copied into per-test repositories, rather than shared writable repositories.
- **Required.** Teardown and background-process writes count as mutations.

**TS-R2b: how durations are measured.**
- **Comparable conditions:** the same machine load, the same worker count, and the same cache state.
- **Setup and teardown are included.**
- **The 5 s check covers every test,** so `--durations=0` is filtered, not only the top 30.

**Note.** `scripts/test-chunk.sh` already forwards extra pytest arguments, so `-n` needs no new interface.

## Amendments of 2026-10-14: Run D, budget readers (cause 1 of the suite-speed research)

Source: the researcher's report (nd-a2480ae2), cause 1. A profiling run of the
full suite at `-n 4` took 3163 s wall, about 12,650 worker-seconds. Every
`Runner.start` reads every enabled provider's budget (`budget.read_all`,
runner.py around lines 5294 and 4928). Today the `_no_host_cli_budget` fixture
in `tests/conftest.py` keeps that read away from the host CLI, but the shipped
script still **runs**, as a subprocess, to produce its no-CLI answer. The
estimated cost is 3,000 to 5,000 worker-seconds. That estimate was not
measured, so it is the first thing this run measures.

**TS-R6: no process is spawned for a default budget/models read.**
- **The rule.** In a test that is not marked `real_providers`, a `budget` or `models` action that would run a SHIPPED provider script against a CLI the test did not build under the basetemp spawns no process. "Shipped" means byte-identical to its twin in the shipped defaults, as the fixture already defines it.
- **The answer stays the same.** That action returns exactly what the script returns today when run with `MULTIAGENTS_BIN` empty: the same reading, the same exit status, the same error text. A precomputed or memoized answer is acceptable, provided it is that answer.
- **Unchanged cases.**
  - A test pointing at a fake script or fake CLI of its own still runs it.
  - A test marked `real_providers` still runs the real script.
  - `budget`'s own caching semantics are unchanged.
- Verified by:
  - a test that counts process spawns during a default `Runner.start` and expects zero from budget/models reads;
  - a test showing that a fake-CLI budget script is still executed;
  - a test that compares the replaced answer with a real no-CLI run of each shipped script, so the replacement cannot drift from the scripts.

**TS-R7: the routing guarantees do not move.**
- No test outcome changes.
- **Parity.** The full suite at `-n 4`, run from a clean worktree, gives per-id outcomes identical to the base, ae9e6014. Known load flakes are rerun alone to confirm them.
- Verified by: the orchestrator's full-suite comparison before merge.

**TS-R8: the gain is measured, not assumed.**
- **What is reported.** The summed `--durations=0` worker-seconds, before and after, under the conditions of TS-R2b.
- **A small gain is a finding.** A reduction under 1,000 worker-seconds is reported as a finding about the research estimate, not hidden.
- Verified by: the two duration files under `.multiagents/handoff/`.

**Scope.**
- The change lives in `tests/` (fixtures and support), so the `tester` makes it: implementers cannot edit existing test files.
- Any `src/` seam is not included. If one turns out to be needed, the agent stops with NEED_INFO.
- Causes 2 and 3 of the report (template repositories, an injectable clock) are not part of this run.
