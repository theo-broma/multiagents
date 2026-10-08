# A loop held by a failed run can be resumed (LR)

Observed 2026-10-08: two review loops (nd-941c69b2 and nd-fbdd42ac) were held
with reason `run_failed` after a child run hit a provider quota. They could not
be resumed:
- `relaunch_node` on the loop is refused: "requires loop_max, unresolved_round
  or a done loop".
- Relaunching the failed child does not clear the loop's hold.
- The only way out is `close_node`, which loses the loop.

Researcher ag-de186a:
- A failed child's settlement holds its nearest loop
  (scheduler/engine.py:1637).
- The refusal is in scheduler/control.py:40-42.
- `reset_round` (engine.py:1767-1782) resets a whole round, or only the
  verdict child. It clears the hold and keeps alias and session bindings.
- A direct child relaunch resets only that child (control.py:96-102).
- Parents are released only for `child_held` (engine.py:1712-1718).
- A sequence whose child fails becomes done/failed and is not relaunchable
  (engine.py:1720-1722). That is out of scope here.

## Behaviours

**LR-R1.** `relaunch_node` on a loop held with `run_failed` is accepted. It
resets only the failed child named in the hold, and only in the current
round, without counting a new round.
- The sibling's completed state and its session binding are kept.
- The failed child's session binding is kept too, so the child resumes.
- The loop's hold is cleared.
- `max_rounds` may be passed but is not required.
- Pins passed to the relaunch apply to the failed child.

Verified by: a loop whose worker run fails. A relaunch is accepted, the
worker reruns in the same round with its session, and the reviewer then runs
and its verdict is honoured. Round count unchanged.

**LR-R2.** Relaunching the failed child directly has the same effect as
LR-R1: the parent loop's `run_failed` hold that names that child is cleared,
and the round continues.
Verified by: same fixture, relaunching the child instead of the loop.

**LR-R3.** Holds for other reasons keep their current preconditions and
behaviour, including the existing refusal for a loop that is not held and not
done.
Verified by: tests/test_nc_m4_loops.py and tests/test_tb_a_relaunch_simple.py
stay green, plus a refusal test for a running loop.

**LR-R4.** `wait_for_agents` reports a hold in `changed` only once. Today, a
call with explicit ids returns at once, listing standing holds of unrelated
nodes as `changed` every time, so it can never block. A standing hold may
still appear under `holds`. `changed` lists only transitions that are new
since the caller's last wait or acknowledgement, and a wait with nothing new
on the requested ids blocks until its timeout.
Verified by: two held unrelated nodes. A wait on a running node's id blocks
until that node finishes, or until the timeout. It does not return at once
with the old holds in `changed`.

## Out of scope
- Resuming a sequence that became done/failed.
- Automatic relaunch of a loop after a quota reset (it stays an orchestrator
  decision).

Decisions (orchestrator, 2026-10-08, on tester ag-d00ca8's questions):
- LR-R4, strict reading: a wait on explicit ids never lists in `changed` a
  hold or transition of a node outside those ids (and their descendants for a
  composite id), not even on the first call. Such holds may still appear
  under `holds`.
- A hold of a requested node appears in `changed` once, then not again.
- "Resumes its session" means the rerun resumes a session of an earlier run
  of that child. Which one, after a retry, is not specified.
- LR-R1/R2 pins (on implementer ag-379938's question): NC-R32 still holds.
  A pin that changes the model of an aliased session needs `new_session` or a
  provider with `session_model_change: true`. The two pin tests
  (test_lr_r1_pins_apply_to_the_failed_child and
  test_lr_r2_pins_on_the_direct_child_relaunch_apply) are wrong as written.
  The tester fixes them after merge by enabling that capability in their
  fixture providers. Until then they are known reds, and the implementation
  must not bypass NC-R32.
- Repeated-failure relaunch test (on implementer ag-a9dd0b's question): the
  existing provider failure breaker (three failures, 1,800 s) still holds;
  LR does not bypass it. The test's fixture is wrong to expect a 20 s
  relaunch after three failures. The tester fixes it after merge by raising
  `provider_failure_threshold` to 4 in that fixture. Until then it is a known
  red.
- LR-R4 review (reviewer ag-5e7de9), decided by the orchestrator:
  - The strict reading above stands. test_lr_r4_a_hold_that_appears_during_the_wait_is_reported_once expects a hold on a node outside the requested ids (H3) in `changed`, and that test is wrong. The tester rewrites it so the new hold is on a requested node or one of its descendants. It also checks that an outside hold is NOT in `changed`.
  - The delivery marks for wait_for_agents must not grow without bound. A mark is dropped once its hold clears or its node is gone.
  - A new hold episode on the same node with an identical payload is a new hold and is reported once again.
  - Relaunch emits the existing `reopened` transition for every node whose state it changes: the reset failed child (LR-R1) and the loop whose hold a direct child relaunch clears (LR-R2).
