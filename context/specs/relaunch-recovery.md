# Relaunch and recovery of nodes (RR)

Most of the orchestrator's manual scheduler workarounds in October 2026 come
from six defects. Their diagnoses are in BRIEF.md (RD1-RD5, RL, LR2, CC):

- **RD5:** `relaunch_node` silently drops `task`. The MCP wrapper
  (server.py `relaunch_node`) has no `task` parameter, although
  `control.decide` would apply it.
- **RD2:** a relaunch restarts from the node's pinned `input_commit`. Work the
  dead run had committed is lost, even though its tip was captured and
  validated as `refs/heads/node-results/<attempt>` (results.py). Only `done`
  results are integrated.
- **RL / RD3:** changing a child's task needs a separate `update_node` after
  the relaunch. The next admission tick can claim the child first, so the run
  starts with the old task.
- **RD1:** when a nested (non-root) MCP server exits, `_runner.shutdown`
  cancels the scheduler-owned runs it can see, which sends SIGTERM to their
  providers.
- **LR2:** a loop held `run_failed` whose named child has since been reopened
  stays held forever. Composites self-release only `child_held`.
- **CC:** `close_node` settles only its target. Descendants that are held or
  open with no active attempt are left behind, and keep showing up as
  blocked.

This spec builds on LR (loop-run-failed-resume.md) and keeps LR-R1 to LR-R4.
Data structures and storage are the developer's choice.

## Behaviours

Throughout: root-only authorisation, `revision` preconditions and request-id
replay semantics of `relaunch_node` and `close_node` are preserved.
`control.decide` already runs inside one RPC write transaction
(scheduler/rpc.py); "atomic" below means no partial change is ever
committed, not that no intermediate write happens.

**RR-R1 (RD5).** The `relaunch_node` MCP tool accepts `task: str`. For a
simple target, the task is applied in the same transaction as the reopen, so
the first run launched afterwards receives it. For a loop target, `task` is
refused as `invalid` with a message pointing at `children` (RR-R2). A
relaunch without `task` leaves the task unchanged.
Verified by: an MCP-level test calling the tool with a new task, then
checking the task that the next launched attempt received. A test that a loop
target with `task` is refused and changes nothing.

**RR-R2 (RL, RD3).** `relaunch_node` on a **loop** target accepts
`children: {child_id: {revision: int, task?: str, pins?: {...},
new_session?: bool}}`. On a simple target `children` is refused as `invalid`;
use `task`, `pins` and `new_session` there.
- Each entry names a simple descendant of the loop with no active attempt,
  and that child's current `revision` as a precondition.
- Every entry is validated before the transaction commits. A stale revision
  refuses the whole call with `conflict`; an unknown child, an active child
  or an invalid field with `invalid`. Nothing is committed: not the target,
  not any child, not any alias binding.
- If all are valid, the patches and the reopen commit together. No admission
  tick can observe the reopened loop with any patch missing.
- `new_session: true` has the same alias-wide meaning as today's top-level
  `new_session` list: it renews the binding shared by every child on that
  alias.
- The existing top-level `pins` and `new_session` keep working. A child named
  both there and in `children` is refused as `invalid`.
- Dependents: unchanged from today. `dependency_reopened` is emitted only for
  dependents of the relaunched target that have prior runs; no dependent is
  reset or stopped.
Verified by:
- a test where an admission tick runs immediately after the relaunch, and the
  first claimed attempt carries the patched task and pins;
- a test that one stale revision leaves the whole store unchanged;
- the existing relaunch tests stay green.
Reviewer inspection: all validation happens before the first write that could
commit.

**RR-R3 (RD2).** A relaunch resumes from the dead run's work by default.
- **Eligibility.** A relaunched simple node, or the loop child reset by a
  loop relaunch, is eligible when its newest attempt ended other than `done`
  and the store records a successful result capture for it. A node with no
  recorded capture relaunches exactly as today.
- **What it launches from.** The next launch starts from the merge of the
  captured commit with the node's currently resolved inputs, so later work
  from dependencies or siblings is kept. When the inputs have not moved, this
  is the captured commit itself.
- **Holds.** The node is held `recovery_conflict`, with the attempt and the
  cause in the detail, when the captured ref is missing or no longer at the
  recorded commit, when it does not descend from the attempt's input, or when
  the merge with current inputs conflicts. The hold keeps all patches applied
  by the relaunch and the evidence. It never silently falls back.
- **Durable record.** The source attempt, the captured commit and the launch
  that consumed it are recorded on the node. Recovery is consumed only when a
  run actually launches; a launch failure before that keeps it pending for
  the next try.
- **Opting out.** `relaunch_node(recover_wip=false)` applies to the whole
  call and starts from the currently resolved inputs, as today. Relaunching a
  `recovery_conflict` hold with `recover_wip=false` clears it without
  repeating or reverting earlier patches.
- **No authority.** Recovered work is only a starting point: it creates no
  approved generation and no publication right.
- The relaunch result reports `recovered_from: {attempt, commit}` when
  recovery applies.
Verified by:
- a test where a run commits, dies (`failed`, signal or crash), and the
  relaunched run's worktree contains that commit;
- a test where the inputs advanced meanwhile and both the WIP and the new
  input are present;
- one test per hold cause, and a test that `recover_wip=false` clears the
  hold;
- a test that a launch failure keeps recovery pending;
- a test that a node with no recorded capture relaunches as today.

**RR-R4 (RD1).** No MCP server other than the scheduler stops a
scheduler-owned run when it exits, whether by transport close, SIGTERM or
SIGHUP, at any step of its shutdown, quota-handover cleanup included.
Ownership is decided from the host-recorded attempt authority, not from what
the server can see. It covers launched, suspended and recovering attempts.
The server detaches from them and leaves the scheduler's status, claims and
completion evidence intact, so no duplicate admission can follow. Runs the
server started itself outside the scheduler keep today's behaviour.
Verified by: a test that starts a scheduler-owned run, shuts down a nested
server with the same visibility, and checks the run is still alive, is
claimed exactly once, and is later completed or adopted by the scheduler. A
test that a non-scheduler run is still stopped as today.

**RR-R5 (LR2).** The engine repairs a stale `run_failed` hold. Today's direct
relaunch of the named child already clears the loop at once (LR-R2), so this
covers states left by older code or by a crash between steps.
- Every relaunch that reopens a loop child records the loop round in which it
  happened, durably on the child.
- At each tick, a loop held `run_failed` whose named child exists, is in the
  loop's subtree, is open or running, and carries that record for the
  current round, returns to running with one `hold_repaired` transition
  naming the child.
- In every other case the hold is untouched: the child is missing, the round
  has changed, or the child is still `done/failed` or already completed
  otherwise.
Verified by: a test that seeds the inconsistent state directly in the store,
restarts the engine, ticks once, and checks the repair and its single
transition. One test per untouched case.

**RR-R6 (CC).** `close_node` on a held or open composite settles, in the same
transaction, every descendant that is `held` or `open` with the same outcome.
- **Refusals.** It refuses with `active_runs` and changes nothing if any
  descendant has a claimed, launched, captured or suspended attempt, or a
  pending resume. Close never touches resumable or running work (NC-R37). The
  existing completion, disposal and git-operation guards are kept.
- **What is recorded.** Each settled descendant records
  `closure: {by: "root", via: <closed id>, outcome}`, gets its revision
  incremented, and emits one `closed_by_ancestor` transition.
- **What approved means.** It only settles scheduling: no descendant
  generation becomes approved. Dependencies on the settled descendants are
  released as they are today for a closed node.
- Descendants already `done` keep their outcome. Bindings and dirty archives
  are kept.
Verified by: a test closing a loop whose current child is held and whose next
sibling is open; both end `done` with the outcome, with the closure record
and one transition each, and nothing reports them as blocked afterwards. A
refusal test for each active-attempt kind that changes nothing.

## Out of scope

- Automatic retry of transient failures (spec 2, RF). RR-R3 only decides
  where a relaunch starts.
- Making `cancelled` nodes relaunchable.

## Decisions

1. Recovery is on by default (RR-R3). Losing committed work is the costlier
   mistake, and the orchestrator has had to merge the dead tip by hand each
   time.
2. A recovery conflict holds the node instead of guessing. The orchestrator
   decides with `recover_wip=false` or a fresh node.
3. Recovery merges with the current inputs rather than replacing them
   (advisor, 2026-10-10): replacing could silently drop later sibling or
   dependency work.
4. `children` is loop-only (RR-R2). A simple target already has `task`,
   `pins` and `new_session`.
