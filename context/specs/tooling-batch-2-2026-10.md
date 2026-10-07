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

**TB2-R6. A dirty session-alias worktree is cleaned, not left to hold.**
(User request 2026-10-07: "when a block happens, the scheduler calls the agent back with the block's message and asks it to clean the worktree".)
- Scope: the worktree of a session alias (template `session: X`), at both points where its cleanliness is checked today (admission and the worker's preflight before launch).
- Git-ignored files there are removed before the check and are never a reason to hold. The alias worktree is a scheduler-owned checkout, so any ignored content in it was produced by earlier runs (`.venv/`, `__pycache__/`, `.pytest_cache/`) and is disposable.
- If tracked changes, staged changes or untracked non-ignored files remain, the scheduler first archives them on the host, recoverably (status, diff, and the untracked files' contents), next to today's `dirty-<alias>.diff`. Then it resumes the alias's previous conversation ONCE (same agent, provider, model, account and session as the alias's last run) in that worktree, at the alias's previous commit, before the worktree is moved to the waiting node's input.
- The cleanup message carries the refusal detail inline, bounded: the dirty file list and the tracked diff, never untracked file contents. It asks the agent to leave the worktree clean: commit nothing, remove or revert each listed item, and say in its reply anything it believes should have been kept.
- The cleanup run belongs to the waiting node and is visible: a transition `cleanup_requested` naming the waiting node, the alias and the cleanup run id. It holds the alias like any run on it. It produces no generation and no verdict, counts no loop round, and makes no WIP commit.
- After it, the check runs again. If the worktree is clean, the waiting node launches normally. If it is still dirty, or the cleanup cannot run or finish (session gone, quota, timeout, cancel), the node is held `dirty_worktree` as today, with the archive path in the detail. A fresh conversation is never started in place of the resumed one.
- At most one cleanup per activation of the waiting node, persisted before launch so a restart does not start a second one. relaunch_node starts a new activation.
- Verified by:
  - an alias worktree holding only `.venv/` and `__pycache__/`: the next run launches with no hold and no cleanup;
  - an alias worktree with an untracked `diff.patch`: one cleanup run on the previous session, its message names `diff.patch`, `cleanup_requested` is emitted, no generation is recorded, the archive holds the file, and the waiting node then launches;
  - a cleanup that leaves the file in place: held `dirty_worktree`, with no second cleanup.

## Decisions after tester ag-c964ae (2026-10-07)
- `commits` is the integer count, as for runs outside the scheduler. A relaunched node counts only the latest run's commits (input commit → generation commit).
- "Grew" means a file in the worktree is larger than at the previous repetition (tracked or untracked).
- Evidence for the real R2 trip: ag-89e3a7 ran `tail -n 2 nt-baseline.log` three times; the codex `item.completed` events carry three different `result` digests (output.ndjson items 30/31/33, copy in .multiagents/tb2-evidence/codex-tail-ag-89e3a7.ndjson), yet the run was flagged `doom_loop`. In stream.jsonl the matching `step` events have no result. TB2-R2 is therefore not met for the real codex event sequence; a test must replay that sequence through the real adapter and supervisor.
