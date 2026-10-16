# RB — publication and relaunch respect the node's real baseline

Approved by the user on 2026-10-16.

## Why

On 2026-10-16 the K10 fix (node nd-e00b53a9) hit two scheduler defects, which
researcher run ag-df5ffe confirmed from the code:

- **Stale read-only reference.** The agent had merged main, so its tree held
  main's newer versions of three read-only test files. At publication, the
  read-only restoration compared those paths with the run's old input commit
  and "restored" the old content (`readonly_reverted`). The squash merge then
  carried that revert onto main, and a human had to restore the tests by hand
  (20d4b869).
- **Relaunch off the branch.** A relaunch with a pending recovery prepared its
  input by merging the recovery onto the declared input generations, not onto
  the node's branch tip. The input did not descend from the branch tip, and
  publication then failed with `integration_conflict`.

## Behaviours

**RB-R1 — content already on the base branch is never reverted.**
- At publication, a read-only path whose content in the result equals its
  content in a base-branch commit that is an ancestor of the result is
  `kept`, not restored. This holds even when that content differs from the
  run's input commit.
- An edit to a read-only path that matches no such commit is still restored,
  and still recorded as `readonly_reverted`, exactly as today.
- **Clarification (2026-10-16, approved by the user).** This applies only
  when the run's input carried no version of its own for that path, that is,
  when the input's content for the path is itself the content of a
  base-branch commit (the input was just an older base). When the input
  carries its own version, for example a test a tester wrote, that version
  still wins over the base branch: Decision 11 of
  context/specs/readonly-restore-provenance.md (RP-R11) is unchanged.
- Verified by: an agent merges a newer base-branch commit that changed a
  read-only test, and publication keeps it with no `readonly_reverted` entry;
  an agent's own edit to the same path is still reverted.

**RB-R2 — a relaunch input descends from the branch tip.**
- When a node with a recorded branch tip is relaunched, its prepared input
  descends from that branch tip, whether or not a recovery is pending and
  whether or not the node declares inputs.
- A pending recovery is merged onto that baseline. If the merge conflicts, the
  node is held with a reason naming the conflict. It never launches on an
  input that does not descend from the branch tip.
- Verified by: a relaunch with a pending recovery and declared inputs gets an
  input whose ancestry contains the branch tip, and its publication
  integrates without `integration_conflict`; a conflicting recovery holds the
  node.

**RB-R3 — nothing else changes.**
- A first launch, a node without a branch tip, and a publication with no
  read-only paths touched behave as today.
- Verified by: the existing recovery, readonly-restore and integration test
  files stay green.

## Plan

tester, then implementer (after QS merges: both touch scheduler/results.py), then reviewer.
