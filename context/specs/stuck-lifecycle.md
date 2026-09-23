# A watchdog trip is a label, not a state that outlives the run (ticket bug-2cebea)

**Status:** contract, 2026-09-23.

**Ids.** Every id carries the prefix `SL-`. Never renumber; retire with
`SL-Rn — withdrawn: <why>`.

**Invariant carried over from phase 0:** providers are plugins. No provider
name and no provider tool name may appear in `src/multiagents/*.py` as a
result of this work. Provider-specific facts live in `providers.yaml`.

## The defect

When the supervisor trips (`doom_loop`, `silence`, `runaway_steps`, …) the
node's status becomes `stuck`. `Runner._finalize()` then skips the terminal
status for any node that is `stuck` (`runner.py` ~1481, "keep the trip reason
visible"). So a run that trips and then finishes cleanly stays `stuck` for
ever. `stuck` is in `tree.ACTIVE`, so the node:

- counts against `max_concurrent` in `_preflight` and in `capacity()`, and
  `start_agent` refuses new work;
- is reported by `wait_for_agents` under `already_finished`, while the same
  call's `capacity` counts it as running;
- has no `ended_at`.

The operator's only way out is `discard_agent(force=true)`. On 2026-09-23 this
blocked `start_agent` several times (ag-f2cb6d, ag-d088d0, ag-2bedc4).

Second cause of the same symptom: agy reports `view_file` with only
`AbsolutePath`. Across every run log on disk (1,122 calls), no `view_file`
event carries a line range. Reading one file in several ranges therefore
produces identical loop signatures and trips `doom_loop` on normal work.

## Terms

- **live**: the node's process (or, for a conversation, its current turn) is
  still running.
- **trip**: a supervisor alert that sets a node to `stuck`.

## Behaviours

**SL-R1 — a run that ends gets its terminal status, tripped or not.** When a
run's process exits, the node gets the same terminal status it would have got
had it never tripped (`done`, `failed`, `truncated`, `limited`, idle for a
conversation, …), with `ended_at` set. A prior trip does not prevent or change
that classification.
*Verified by:* a test where a run trips (any trip kind), then exits 0 with a
normal result: the node is `done`, `ended_at` is set, and it is not in
`tree.active()`. The same with an exit that classifies as `failed`.

**SL-R2 — the trip is not lost.** After SL-R1, the trip is still visible to
the operator: the node's `reason` names the trip kind and its message (for
example `done (was stuck: doom_loop: view_file called 3x …)`), and the trip's
existing event in the event stream is unchanged. The exact wording is the
developer's; the test asserts that the reason contains the trip kind.
When the terminal classification itself has a reason (a failure message), both
are present.
*Verified by:* asserting on `reason` in the SL-R1 tests.

**SL-R3 — `stuck` clears when the agent visibly moves on.** A live node in
`stuck` returns to `running` on the first of:
- a tool call whose loop signature differs from the call that tripped;
- a change in the worktree recorded by progress tracking;
- for a `silence` trip, any stream event.
The trip stays recorded as an event. A later trip sets `stuck` again as it
does today. Clearing does not steer, restart or otherwise touch the agent.
*Verified by:* tests driving the supervisor/consume path with a trip followed
by each of the three kinds of progress, asserting the status goes back to
`running`; and one where the same repeated call continues, asserting it stays
`stuck`.

**SL-R4 — one definition of "occupies a slot".** `_preflight`,
`capacity()` and `wait_for_agents` agree. A node occupies a concurrency slot
if and only if it is `pending`, `running`, or `stuck` **and live**. A
finished node never occupies a slot, whatever its label.
*Verified by:* a test where one node is `stuck` and live and another has
finished after a trip: `capacity()["running"]` counts only the first, and
`start_agent` is allowed when that leaves a free slot.

**SL-R5 — `wait_for_agents` waits on a live `stuck` agent.** An agent that is
already `stuck` and live when `wait_for_agents` is called is waited on like a
running one. It is not reported under `already_finished`. The wait still
returns when an agent **becomes** `stuck` during the call (the documented
"finishes or gets stuck"), so a new trip is never missed. The result reports
the stuck agents it is still waiting on, with their reasons, so the caller
can see them.
*Verified by:* (a) a node stuck before the call, which then finishes: the
wait returns on the finish, not immediately; (b) a running node that trips
during the wait: the wait returns with it as changed.

**SL-R6 — reads the stream cannot tell apart are not a doom loop.** A
provider may declare, in `providers.yaml`, tools whose reported arguments do
not identify the call (the stream omits part of what the agent passed).
Repeated calls to a declared tool never produce a `doom_loop` trip on their
own. `silence`, `runaway_steps` and the wall clock still bound them. The agy
block declares `view_file`. Tools not declared behave exactly as today.
**Try the parser first.** If agy's raw stream carries the range anywhere
(another field of `step_update`, `tool_info`, …), mapping it into the event's
arguments in `providers.yaml` is preferred to the declaration, and `view_file`
is then not declared. The developer checks a raw agy stream and says in the
commit which case held and how it was checked. The declaration mechanism is
built either way, since other tools may need it.
*Verified by:* a supervisor test with a provider declaring a tool: N repeats
of it (N ≥ the loop threshold) with nothing changed on disk do not trip; the
same repeats of an undeclared tool do trip. And a check that no provider tool
name was added to `src/multiagents/*.py`.

**SL-R7 — nodes already stuck on disk are healed.** A `tree.json` written
before this change may hold nodes that are `stuck`, not live, and without
`ended_at`. They stop occupying slots without any operator action (SL-R4
makes this so for capacity). When the server's existing reconciliation of
dead processes runs, such a node gets a terminal status as a node whose
process vanished does today, with the trip kept in its reason.
*Verified by:* a test that writes such a node into a tree, then checks
capacity and the reconciled status.

## Decisions from the advisor's read (ag-25c350, turn 9)

- **Liveness is `node.pid` + `node.pid_start` via `procs.alive`.** They
  are persisted in `tree.json`, so SL-R4 holds across a server restart.
- **Explicit operator actions win over SL-R2.** `steer_agent` and
  `stop_agent` set their own status and reason, as they do today; the trip
  remains in the event stream. The "free retry for a cheap death"
  (`runner.py` ~1521) resets a node to `running` as today, and SL-R2 applies
  to the status the retried run finally ends with, not to the dead attempt.

## Decided, from the tester's questions (ag-9cdb71)

- **SL-R6 key:** a provider-level `opaque_tools: [<tool name>, …]` in
  `providers.yaml`.
- **SL-R4 and `pending`:** a `pending` node has no pid yet and occupies a slot
  as today. "Live" is checked only for `running` and `stuck` nodes that have
  a pid. A `running` node whose process is dead does not occupy a slot.
- **The free retry:** only the final status is asserted. The dead attempt's
  trip need not appear in the final reason.
- **SL-R5's report:** the field name is the developer's. The result must name
  each stuck agent still being waited on, with its trip kind, outside
  `changed` and `already_finished`.
- **SL-R7's reconciliation** is `cli.cmd_resume` (the `multiagents run`
  pass). It ends such a node as `orphaned`, and its reason keeps the trip.

## Out of scope, recorded

- `manage_task` polling by agy agents (ag-2bedc4) is a real wasteful loop,
  not a false positive. Unchanged here; BRIEF item 6 covers why it happens.
- Whether the threshold `doom_loop_repeats` is right.
