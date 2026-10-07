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

Decision (orchestrator, 2026-10-07): the whole-scope SL-R3 test runs the scoped suite in an inner pytest and takes about 805 s. That is too costly for every full-suite run, given that up to six agents share the machine. It therefore runs only when `MULTIAGENTS_SL_FULL=1` is set and is skipped otherwise, with a skip reason naming the variable. The single-module SL-R3 test (`..._the_migration_module_alone_...`, about 45 s) stays on by default as the always-on guard. Run the whole-scope test before merging any change to the nc/c23/pc fixtures. (Run ag-9e4522.)

**SL-R5.** The fix does not change the per-id outcomes of the scoped suite
(1555 passed, 1 skipped on main @ 436dfed) and does not lengthen it by more than
10 %.
Verified by: full scoped run with `-n 4`.

## Part 2: the owner dies (2026-10-07)

Observed after part 1 merged: about 91 `scheduler start --foreground` and
`scheduler.worker` processes in the agents' container, reparented to
docker-init, up to 2 days old. Part 1 teardown runs only when pytest runs its
finalizers. Agents' suites are often killed by a timeout (SIGTERM/SIGKILL),
and then the daemon and its workers survive. Both run in their own session
(`scheduler/__init__.py:84-92`, `engine.py:1110-1114`), and the daemon has no
exit condition of its own. Separately, `tests/support/nc_harness.py`
`Sched.close` never reaps workers (researcher ag-08622e; advisor ag-aed397).

Scope widens to every test module that starts a real scheduler daemon:
nc_fixture `World`/`ClockWorld`, `tests/support/nc_harness.py`,
`tests/support/nt_part2_harness.py`, and their callers. Production code IS in
scope for SL-R6..R8, opt-in only.

**SL-R6.** A test harness that launches a real scheduler daemon marks it with
an owner. The owner is the pytest process that runs the test: the xdist
worker, not the controller or the CLI wrapper. The marker identifies the
owner by pid and process start time, so pid reuse cannot impersonate it, and
it carries the fixture's project path. The daemon passes the marker to every
worker it spawns. Without the marker, production behaviour is unchanged
(workers still outlive a daemon stop and are adopted on restart).
Verified by: a test that launches a marked daemon and a worker and inspects
what they carry; a test that an unmarked daemon behaves as before.

**SL-R7.** A marked daemon or worker whose owner has died, or become a
zombie, exits within 10 s of real (monotonic) time. ClockWorld's simulated
clock has no effect on this. The check also runs while startup or admission
is blocked, not only between ticks. Before exiting:
- the daemon launches nothing more;
- each worker ends its own run and that run's wrapper descendants, without
  relying on the daemon (which may already have been SIGKILLed);
- shutdown is bounded: TERM, then KILL after a grace period; each target's
  identity is checked again just before it is signalled; directly owned
  children are waited for.
While the owner lives, stopping or killing the daemon still leaves workers
running and adoptable, exactly as today.
Verified by: an inner owner process that starts a marked daemon with a running
worker and is then SIGKILLed; after 10 s no process carrying that owner's
marker remains. Plus the same check for a SIGTERMed owner.

**SL-R8.** Ordinary fixture teardown kills its own daemon and workers,
including in `nc_harness.Sched.close`. All harnesses share one helper that
selects processes by marker and project path. A teardown never touches
processes of another fixture owned by the same pytest process, nor anything
unmarked.
Verified by: two fixtures under one owner; closing one leaves the other's
daemon and worker alive. A `Sched` teardown after a worker launched leaves no
worker.

**SL-R9.** At session start (once per xdist worker is fine; concurrent sweeps
must be harmless and idempotent), the suite kills marked scheduler and worker
processes whose owner is dead, along with their verified descendants.
Nothing is killed on any other basis:
- a malformed marker, an unreadable `/proc` entry or a different uid gives no
  authority to kill;
- an unmarked process is never killed, so orphans from before this change
  stay manual;
- a process whose owner is alive, such as another agent's running suite, is
  never killed.
Verified by: planted marked processes with a dead owner, a live owner, a
malformed marker and no marker; only the first kind dies.

**SL-R10.** SL-R5 holds for the widened scope: no per-id outcome changes, and
the runtime does not grow by more than 10 %.
Verified by: full suite with `-n 4`, compared with main.

Decisions (orchestrator, 2026-10-07, on tester ag-850b7f's questions):
- SL-R7 "launches nothing more": the daemon checks that the owner is alive
  immediately before every launch. Any launch that starts after the owner
  has died is a violation; the tester's default stands.
- SL-R9: a zombie owner counts as dead, as in SL-R7. The sweep must kill
  processes whose owner is a zombie, and that case is tested.
- SL-R9 selection: the sweep picks processes by the marker in their
  environment, never by argv. A marked process with a dead owner is killed
  whatever its command line, so planted processes shaped like a worker or a
  daemon are valid fixtures. The sweep must not require a process to be a
  real child of a daemon. Its verified descendants are killed with it.

## Out of scope
- Processes leaked by tests outside the scope above (report them if seen).
- Killing processes by name pattern: never `pkill` by pattern, other agents run
  suites on this machine.
