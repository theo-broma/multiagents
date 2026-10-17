# SI — a scheduler stop signals only the scheduler it means

Approved by the user on 2026-10-17. Source: researcher nd-89835a37.

Under parallel test load, test schedulers are seen killed from outside
("scheduler did not come up (exit -15)", "scheduler socket is dead").

What the code does today:
- Production `scheduler.stop()` checks the PID's start time before SIGTERM.
  It records no project identity, and leaves a small window between the
  check and the kill in which the PID can be reused.
- The test fixtures' teardown (`tests/nc_fixture/world.py` `World.close()`
  and its loops, `tests/support/nc_harness.py` `Sched.close()`) SIGKILLs a
  PID that it learnt from whichever scheduler answered its socket. It
  checks only that some process with that PID is alive.
- A test that points at another scheduler's state root and project slug,
  by mistake or through an env override, can therefore kill that
  scheduler, including the real host daemon.

## Behaviours

**SI-R1 — `process.json` names its scheduler.**
- The scheduler writes into `process.json`:
  - the resolved project root;
  - a random instance nonce;
  - its PID and PID start time.
- `stop()` signals the scheduler only when all of these hold:
  - the record's project equals the caller's resolved project;
  - the PID's start time matches;
  - the nonce matches the one the running scheduler reports over its
    socket.
- It signals through a pidfd where the platform offers one, so the PID
  cannot be reused between the check and the signal.
- A record that does not match is refused with a message, and nothing is
  signalled.
- Verified by:
  - a forged or stale `process.json` naming a live, unrelated PID is
    refused;
  - a record with a mismatched start time is refused;
  - a record of another project is refused;
  - a socket that answers with another scheduler's nonce is refused;
  - the normal stop still works.

**SI-R2 — test teardown never kills by a PID it was told.**
- The fixtures stop their own schedulers only through:
  - the marker-based `owner.reap()`, which selects processes by the
    owner marker that holds the owner PID, its start time and the project;
  - or the `Popen` handles of processes they launched themselves.
- They never SIGKILL a PID obtained from a status reply or a file.
- Verified by:
  - two Worlds that share a state root and a slug: closing A leaves B's
    scheduler running;
  - a decoy process with another owner or project marker survives
    teardown.

**SI-R3 — nothing else changes.**
- The owner reaper's behaviour is unchanged.
- `multiagents scheduler stop` keeps its output, apart from the new
  refusal message.
- Verified by: the existing scheduler, owner and NC fixture tests stay
  green.

## Plan

1. tester;
2. implementer (default tier), for the production change; the tester also
   rewrites the fixture teardown, which is test code;
3. reviewer;
4. full suite;
5. merge.

## Clarifications (2026-10-18, answering tester nd-40908215)

- **`process.json` keys:** `pid`, `pid_start`, `project` (the resolved
  absolute project root) and `nonce` (32 random hex characters).
- **Socket status reply:** it gains a `nonce` field, with the live
  scheduler's nonce.
- **Old records:** a `process.json` without `project` or `nonce` (an older
  scheduler, or a test fake) is refused by `stop()` with a message that
  names the missing keys. Nothing is signalled, so the check fails closed.
- **Test fakes:** the ST6 fake daemons are migrated to write and report the
  nonce. That is test code, so the tester does it, after this
  clarification.

## Clarification (2026-10-18, after the combined suite on SI + main)

SI-R1 must keep ST6-R2.
- **A stale record means already stopped.** The record names a PID that is
  dead, or one now owned by another process (its start time differs). In
  both cases the recorded scheduler no longer exists. `stop()` returns
  `stopped: true` and signals NOTHING, as ST6-R2 requires.
- **A refusal (`stop_refused`) is only for a LIVE process** that matches
  the recorded PID and start time, but whose project or nonce does not
  match. That is the case where signalling could hit the wrong scheduler.
- **The nonce is read from the RPC result envelope**:
  `{"ok": true, "result": {..., "nonce": ...}}`.
- SI-R1's "a record with a mismatched start time is refused" is amended
  to "is reported as already stopped, and nothing is signalled".
