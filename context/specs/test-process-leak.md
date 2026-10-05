# Tests must not leak scheduler processes (SL)

Source: plan `context/plans/2026-10-05-scheduler-first-real-use.md`, Apply now §3.
Observed: before the 2026-10-05 container recreate, the project container held
dozens of `scheduler start --foreground` and `scheduler.worker` processes started
by tests (some ~7 h old), from the M2, M5, NC-R97 and adversary test modules.

Scope: the phase 7 part 1 suite, i.e. `tests/test_nc_*.py`, `tests/test_c23_*.py`,
`tests/test_pc_*.py`. Production code is out of scope unless a test cannot stop
what it starts without it (then say so and stop: that is a decision).

## Behaviours

**SL-R1.** Every scheduler process (`scheduler start --foreground`, or any other
scheduler entry point) and every `scheduler.worker` process that a test in scope
starts, directly or through code it calls, has exited by the end of that test's
teardown — whether the test passed, failed, errored or was interrupted by a
timeout inside the test.
Verified by: SL-R3 run against the whole scope.

**SL-R2.** A process a test is *expected* to leave running for the duration of the
test (e.g. a scheduler under test) is stopped by a fixture finalizer or
equivalent that runs on failure, not by a statement at the end of the test body.
Its descendants (workers it spawned) are stopped too.
Verified by: SL-R4.

**SL-R3.** A regression test counts the scheduler and worker processes that
belong to the test session before and after running the scoped tests, and fails,
naming the leaked pids and their command lines, if the count after is higher.
"Belong to the test session" must exclude processes the test session did not
start — in particular the real host scheduler of the project that runs the
suite, and other agents' concurrent suites on the same machine. (How to tell
them apart — e.g. a marker in the environment or the project path under the test's
temp dir — is the developer's choice.)
Verified by: the test itself, red on a deliberately leaking test, green on main
after the fix.

**SL-R4.** A test that fails mid-way (an assertion raised while the scheduler is
running) still leaves no process behind.
Verified by: a test using a deliberately failing inner test (e.g. via pytester)
or an equivalent mechanism.

**SL-R5.** The fix does not change the per-id outcomes of the scoped suite
(1555 passed, 1 skipped on main @ 436dfed) and does not lengthen it by more than
10 %.
Verified by: full scoped run with `-n 4`.

## Out of scope
- Processes leaked by tests outside the scope above (report them if seen).
- Killing processes by name pattern: never `pkill` by pattern, other agents run
  suites on this machine.
