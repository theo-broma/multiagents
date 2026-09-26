# Agents survive a restart of the orchestrator's CLI (BRIEF item 8)

**Status:** contract, 2026-09-24.

**Ids.** Prefix `SV-`. Never renumber; retire with `SV-Rn — withdrawn: <why>`.

## Why

The Runner lives in the MCP server, a stdio child of the orchestrator's CLI.
When that CLI goes away, today's server cancels every running agent
(`runner.py` ~1326-1353). After a crash, the next `multiagents run` reaps them
as `orphaned` (`cli.py` ~970-1013). No money runs away, but the work in flight
is lost. That happens on:
- an R8f compaction (which is why R8f may only fire on an idle tree);
- a usage-limit restart (`driver.py` ~1010);
- a restart to load new code or a new credential;
- a stray `/exit`.

The smallest design, from the advisor's analysis (ag-25c350, turns 11-13):
1. agents write their output to files, not to a pipe the server owns;
2. a root server leaves them running when it exits;
3. the next root server adopts them.

## Facts this rests on (advisor, verified in code, turn 13)

- **A `docker exec`'d command keeps running when the exec client dies**
  (`executor/docker.py` ~65). Its output goes nowhere, so an agent may block
  or die from SIGPIPE when it writes. If the launch wrapper
  (`docker.py` ~1339) sends the output to a file, that risk goes away.
- **The run dir sits on the writable bind mount** (`paths.py` ~182,
  `docker.py` ~363), so host and container see the same files. Follow them by
  polling (read to EOF, sleep, retry). Do not rely on inotify across the
  mount.
- **`tree.json` writes are atomic and briefly locked** (`tree.py` ~332). No
  lock records who supervises a node.
- **The server cannot tell `/exit` from a crash or a SIGTERM.** In
  interactive mode the driver has exec'd into the CLI, and the server sees
  the same EOF in all three cases. So no design may rely on a marker written
  before a stop.

## Behaviours

**SV-R1 — output goes to files.** Every agent, on both the local and the
docker executor, has its stdout and stderr written by its launch wrapper to
files in `runs/<id>/`. The server reads those files and holds no pipe the
agent writes to. Whatever the server currently consumes from the pipe
(stream events, usage, session id, result) it consumes from the file, with
the same results.
*Verified by:* a stub agent that writes output lines over a few seconds.
SIGKILL the server process partway through. The agent finishes, and the file
holds every line, including those written after the kill. This is run on both
executors (docker marked as needing the container).

**SV-R2 — the exit status is recorded without a server.** When the agent
process ends, the wrapper writes its exit status to `runs/<id>/exit_status`,
whether or not any server is alive. A timeout kill (SV-R4) is recorded so it
can be told apart from the agent's own exit.
*Verified by:* a stub agent exiting 3, with no server attached, leaves `3`.
The SV-R4 case leaves a status that the server classifies as `timeout`.

**SV-R3 — a root server leaves agents running when it exits.** The root
server is the one started by the orchestrator's or initializer's CLI, with
no `MULTIAGENTS_AGENT_ID`. On stdin EOF, SIGTERM or SIGHUP it does not
cancel its running agents. It marks them `detached`, with the time, records
a `detached` event per node, and exits. A depth ≥ 1 server keeps today's
behaviour: it cancels its children, whose parent agent has finished and
would never read their result. `stop_agent` and cancellation by a parent are
unchanged.
*Verified by:* a root server with two running stub agents gets EOF on
stdin. It exits, both processes are still alive, both nodes are `detached`.
The same with a depth-1 server: its children are cancelled.

**SV-R4 — a detached agent is bounded without a server.** The node's
wall-clock timeout is enforced by the launch wrapper as well as by the
watchdog, counted from the run's start. At expiry it kills the agent's
process group (inside the container, for docker) and records the SV-R2
status. No agent can outlive its timeout because no server is watching.
Token-budget ceilings are not enforced while detached (see Out of scope).
*Verified by:* a stub agent that sleeps forever, timeout 3 s, no server. It
is dead within the timeout plus a small grace, and its status reads as a
timeout.

**SV-R5 — ownership is per node, and exclusive.** A server supervising a
node holds an exclusive advisory lock on a file in `runs/<id>/` for as long
as it supervises it. It takes the lock when it starts or adopts the node,
and gives it up when the node reaches a final state or is detached. No
server adopts a node whose lock is held. Several root servers may run in one
project at once, as they do today. A suspended old server that wakes up
still holds its locks, so nobody has adopted its nodes.
*Verified by:*
- two server processes on one project: a node locked by A is not adopted by
  B;
- after A is SIGKILLed, B adopts it;
- A's own nodes are never touched by B while A lives.

**SV-R6 — the next root server adopts what is left.** At startup a root
server looks at every node in `running` or `detached` whose lock is free,
and which belongs to the same session role (orchestrator or initializer, as
`agent_tree` attributes it).
- **Process alive:** take the lock and replay the output file from the last
  consumed offset. That rebuilds usage, session id, step and doom-loop
  counters and events. Then follow the file. The node returns to `running`
  with an `adopted` event.
- **Process dead:** replay to the end and finalise the node from the stream
  and `exit_status`, exactly as if it had ended while supervised (`done`,
  `failed`, `timeout`, including the provider's own classification of its
  result).
- **Nothing to judge it by** (no output file and no status): `orphaned`, as
  today.

`_reconcile` in `cli.py` no longer reaps a live process whose node it could
adopt.
*Verified by:*
- a detached live stub is adopted and later collected as `done`, with its
  full result;
- a stub that finished during the gap is finalised `done`, with the right
  usage;
- a stub that failed during the gap is finalised `failed`;
- a node with neither file is `orphaned`;
- a node of the other role is left alone.

**SV-R7 — nothing is counted twice or lost.** The last consumed offset of
each output file is persisted, so a replay neither drops nor double-counts
tokens, cost, steps or events. That holds for the gap, and for a server
killed at any point during replay.
*Verified by:* a stub emitting known usage. Detach it mid-stream, let it
finish, then adopt it. The totals equal those of an unbroken run. The same
with the adopting server killed mid-replay and a third server adopting.

**SV-R8 — the watchdogs are fair across the gap.**
- The wall clock counts from the run's original start.
- The silence clock counts from the later of the node's last output and its
  adoption. The server's downtime is not silence.
- Step and doom-loop counts continue from the replay (SV-R6).

*Verified by:* a stub that wrote output 1 s before a 2× `silence_timeout`
gap is not reported stuck on adoption. A stub past its wall clock at
adoption is ended as timeout.

**SV-R9 — an adopted node is an ordinary node.** `wait_for_agents` (with or
without ids), `check_agent`, `collect_agent`, `steer_agent`, `stop_agent`,
`merge_agent` and `discard_agent` behave on it exactly as on a node the
server started. Steering an adopted node whose run has ended resumes its
provider session, as it does today.
*Verified by:*
- `wait_for_agents()` with no ids, called on the adopting server, returns
  when the adopted stub finishes;
- `stop_agent` kills it;
- a steer after it finished resumes it.

**SV-R10 — the user can see and undo it.**
- `multiagents run` states on startup how many agents were left running by
  the previous session, and their ids. The orchestrator sees them in
  `agent_tree` marked adopted.
- `multiagents stop <id>` and `multiagents stop --all` end detached or
  running agents of this project, with no server needed. They use the
  recorded pid and process group, or `docker exec` kill in the container,
  and mark the nodes `cancelled`.

*Verified by:* with a detached stub, `multiagents stop --all` leaves no live
process and marks the node cancelled. The startup notice names it.

**SV-R11 — compaction may fire with agents running.** Once SV-R3 to SV-R6
hold, the R8f idle-tree condition is relaxed: compaction may fire when no
agent has produced a result the orchestrator has not yet seen, instead of
when none is running. This is kept separate so that it lands last and alone.
*Verified by:* the R8f compaction tests, with a running stub agent. The
compaction fires, and the agent is adopted and finishes.

## Decided

- **Where the offset lives (SV-R7).** Where it is stored is the developer's
  choice. The invariant is not: the consumed offset and the usage, steps and
  events it accounts for are persisted in the same atomic write. A crash
  between the two must not be possible.
- **A dead process with no `exit_status`** (killed by the kernel, or the
  wrapper itself killed) is judged from its stream. It is `done` or
  `failed` if the stream holds the provider's final result. Otherwise it is
  `failed` with the reason "process ended without an exit status". Nothing
  waits for a file that will never appear.
- **Session role** comes from matching the node's `session` to a driver
  node's `session` and that driver's `role` (`runner.py` ~1400,
  `tree.py` ~474).
- **`/exit` detaches too.** It cannot be told apart from a crash (Facts).
  A stray `/exit` was one of the cases to protect. SV-R4 bounds the cost,
  and SV-R10 gives the undo.
- **No lifetime lock on the project,** because concurrent root servers exist
  today. The lock is per node (SV-R5).
- **Depth ≥ 1 servers keep cancelling.** Only root servers detach (SV-R3).

## Decided, from the tester's questions (ag-0e7618)

- **When adoption happens (SV-R5/R6):** at a root server's startup, and
  again while it runs, at least every 10 s. Adoption covers a node whose
  owner died next to a live server, not only a node left by a previous
  session.
- **Timeout (SV-R4/R8):** either `status == "timeout"` or `failed` with a
  reason that names the timeout. The developer picks one and uses it on both
  paths.
- **SV-R11 "unseen result":** a final node's result is seen once
  `wait_for_agents`, `check_agent` or `collect_agent` has returned that
  final status to the orchestrator's server. Until then, compaction waits.
  This must be recorded durably, since compaction follows. When SV-R11
  lands, the tester (not the developer) updates
  `test_p0_r8f_2_2_a_live_agent_blocks_it[running]`, which asserts the
  opposite.
- **`multiagents stop` (SV-R10), per the user, 2026-09-24.** It already
  exists (`cli.py` `cmd_stop`): it stops the drivers, then every agent
  (committing their work in progress), then the container. It stays the
  explicit "stop everything" command. With no argument it must also stop
  DETACHED agents, with no server alive. `--all` is accepted as a synonym
  for no argument, and `stop <id>` stops just one agent. Detaching applies
  only to implicit ends of the CLI: `/exit`, a crash, compaction, a
  limit restart. It never applies to `multiagents stop`.
- **Startup notice (SV-R10):** `run --no-launch` counts as a startup. The
  wording is free, as long as it names the ids and the count.
- **Docker variants:** run on the host with `SV_TEST_DOCKER=1` before
  delivery. The orchestrator checks they actually ran.

## Out of scope, recorded

- **Token-budget ceilings while detached.** The wall clock bounds them; a
  budget_tag overshoot during a gap is accepted.
- **A daemon, or an HTTP MCP server hosted by the driver.** Both were
  rejected in BRIEF item 8.
- **Adoption across session roles,** for example the initializer adopting
  the orchestrator's agents.

## Clarification, 2026-09-26 (SV-R4, decided by the orchestrator)

The wrapper kills at the deadline **only while no server holds the node's
supervision lock**. A supervised run past its wall clock is reported `stuck:
timeout` and left to the orchestrator, as the watchdog always has been. A
watchdog never kills. Once the lock is released (the server detached or died),
the deadline applies again and the wrapper enforces it. Adoption skips a node
already past its wall clock, so the wrapper ends it and the next pass records
the timeout. This came from ag-a29f15, whose reading kept the
`test_phase0_watchdog.py` timeout tests valid.

Also accepted: `stuck` nodes are adoptable like `running` and `detached` ones.
When an agent exits, the wrapper kills whatever is left in its process group.
