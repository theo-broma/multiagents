# Tooling batch 2, 2026-10: defects still open after the scheduler trial

Source: §4 trial report in BRIEF.md (2026-10-07), inventory by researcher
ag-f02c6b. Rules are prospective, as in tooling-batch-2026-10.md.

Not in scope: a child depending on an already-finished node launches at once.
That is the dependency semantics working as written; the orchestrator deposits
a dependent only once its task is final.

**TB2-R1. collect_agent reports a node run's commits.**
- For a run launched by the scheduler, `commits`, `diff_stat` and
  `no_commits` describe the commits that run added to its node branch
  (`nodes/<node>`), from the run's input commit to the generation commit.
- A node run that added no commit still reports `no_commits: true`.
- Runs started outside the scheduler keep today's behaviour.
- Verified by: a node run that commits (no `no_commits`, correct count and
  stat) and one that does not.

**TB2-R2. The doom-loop watchdog does not flag commands whose output changes.**
- A repeated identical command counts toward `limits.doom_loop_repeats` only
  when its output (or result) is also identical to the previous repetition,
  for every provider's tool-event shape, codex `command_execution` included.
- When a provider's event carries no output, a repeated command counts only
  if nothing changed on disk AND no file the agent's worktree holds grew since
  the previous repetition. (Tailing a growing log is not a loop.)
- Verified by: codex-shaped events tailing a growing log (no stuck); the same
  command with identical output and no disk change (stuck, as today).

**TB2-R3. wait_for_nodes reports capacity.**
- Its reply carries the same `capacity` object as `wait_for_agents`
  (`running`, `max_concurrent`, `free_slots`, and the idle note).
- Verified by: a wait with one node running.

**TB2-R4. Waiting on a composite wakes on its children.**
- `wait_for_nodes` (and `wait_for_agents` given a composite node id) filtered
  on a composite returns transitions of any descendant, not only of the
  composite itself.
- Verified by: a sequence whose first child finishes while the caller waits on
  the sequence id.

**TB2-R5. Status output does not grow with history.**
- `scheduler_status` and wait replies list window entries only for nodes that
  are not final (open, running, held, pending resume).
- Verified by: 50 done nodes and 1 open node with a window: one entry.

## Decisions after tester ag-c964ae (2026-10-07)
- `commits` is the integer count, as for runs outside the scheduler. A relaunched node counts only the latest run's commits (input commit → generation commit).
- "Grew" means a file in the worktree is larger than at the previous repetition (tracked or untracked).
- Evidence for the real R2 trip: ag-89e3a7 ran `tail -n 2 nt-baseline.log` three times; the codex `item.completed` events carry three different `result` digests (output.ndjson items 30/31/33, copy in .multiagents/tb2-evidence/codex-tail-ag-89e3a7.ndjson), yet the run was flagged `doom_loop`. In stream.jsonl the matching `step` events have no result. TB2-R2 is therefore not met for the real codex event sequence; a test must replay that sequence through the real adapter and supervisor.
