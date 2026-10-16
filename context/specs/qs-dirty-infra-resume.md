# QS — an infra-cut alias run with a dirty worktree resumes without a human

Approved by the user on 2026-10-16.

## Why

On 2026-10-15, the slice 3 implementer (run ag-6dab78, node nd-61aa817a) was cut by the codex quota. It left uncommitted edits in its alias worktree.

- `sessions.check_clean()` archived the dirty tree and refused with `dirty_worktree`. The node was then held for about 4 hours, emitting `held_idle` anomalies.
- A human had to:
  1. commit the work to a side ref;
  2. reset the alias to the binding commit;
  3. relaunch, with a task telling the agent to `git merge --ff-only` that commit.
- `relaunch_node(recover_wip)` cannot help here: it only recovers captured result refs, never uncommitted alias state.
- This analysis is from research run ag-23e00e.

## Behaviours

**QS-R1 — snapshot and reset.**
- Trigger: an attempt ends with TF class `quota`, `crash` or `signal` (an infra cut), and its alias worktree is dirty, meaning it has tracked, staged or untracked changes.
- The scheduler then does these steps, in this order:
  1. it snapshots the dirty state as a commit whose parent is the binding commit;
  2. it stores that commit on a scheduler-owned ref;
  3. it persists the ref as the node's recovery source, before touching the worktree;
  4. it resets the alias worktree hard to the binding commit, so that HEAD equals the binding commit and the tree is clean.
- The tar archive is still written, as forensic evidence.
- Verified by: tests/test_qs_dirty_infra_resume.py.

**QS-R2 — resume through the recovery path.**
- The node is not held `dirty_worktree` in that case.
- Its next attempt, after the quota is released or a normal retry, launches. That attempt's input contains the work in progress, through the existing recovery merge path.
- The recovery is consumed only after a confirmed launch.
- Verified by: tests/test_qs_dirty_infra_resume.py.

**QS-R3 — read-only paths are not carried.**
- In the snapshot commit, the run's protected read-only paths (for example `tests/`) are restored to the binding commit's content.
- Raw edits to those paths survive only in the tar archive. A forbidden test edit therefore never becomes the next attempt's baseline.
- Verified by: tests/test_qs_dirty_infra_resume.py.

**QS-R4 — restart idempotence.**
- If the scheduler restarts between the snapshot and the reset:
  - no second snapshot is made;
  - no work in progress is lost;
  - the worktree ends clean at the binding commit, with the recovery recorded.
- Verified by: tests/test_qs_dirty_infra_resume.py.

**QS-R5 — a missing or corrupt snapshot holds.**
- If the snapshot ref is missing or corrupt at resume, the node is held with a reason that names the problem.
- It never launches on a partial tree.
- Verified by: tests/test_qs_dirty_infra_resume.py.

**QS-R6 — no publication, and other ends unchanged.**
- Nothing in the snapshot is published or integrated, except through normal capture and integration. No generation or verdict is implied by the snapshot.
- A dirty worktree after a non-infra end (for example `done` or `question`) keeps today's behaviour: a cleanup run, or a `dirty_worktree` hold.
- Verified by: tests/test_qs_dirty_infra_resume.py.

## Plan

1. tester nd-9f6a9f82, which starts after slice 3 and its K10 fix are on main;
2. then an implementer;
3. then a reviewer.
