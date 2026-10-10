# Small tooling items (PS2)

The remaining small items of the October 2026 tooling batch (BRIEF.md: small
items). Each is independent. Publication with shared history (PB) and short
tool outputs were moved to later specs (see the end of this file).

- **PB.** `merge_node` always squash-publishes, with one parent, through
  merge-tree and commit-tree (scheduler/results.py). When two node branches
  share commits that are not on main, the second publication duplicates them.
  There is no way to tell the scheduler that a node's work reached main by
  another route, so the node keeps showing as unpublished.
- **Outputs.** `wait_for_agents`, `wait_for_nodes` and `get_node` return the
  full task text and full histories on every call. One `wait_for_nodes` reply
  reached 106,000 characters, which the orchestrator cannot afford.
- **OC exit.** After three failed settlements in in_session mode, the OC
  driver raises `RuntimeError`, so the user sees a traceback.
- **dev-advisor.** `consult("dev-advisor")` from implementer-deep returns
  `forbidden`. scheduler/rpc.py (around 154-170) admits a parent's run only
  if the run is in the plan subtree, or if a host-authority record names the
  parent. A consult child is neither (researcher ag-22e402).
- **Test cleanup.** AGENTS.md tells agents to `rm -rf` their pytest basetemp,
  and the providers' own shell-safety layers refuse it. `scripts/test-chunk.sh`
  `exec`s pytest, so it cannot clean up afterwards (researcher ag-ca5253).
- **Test isolation.** Some tests (tests/test_t1_*) fall back to the current
  directory's multiagents config. Run from the project root, whose config
  enables the scheduler, 24 of them fail with `unauthenticated`; from a
  worktree they pass.

## Behaviours

**PS2-R1, PS2-R2, PS2-R3 — withdrawn:** moved to the later specs 4b
(publication) and 4c (short outputs with pagination). The advisor judged them
substantial API changes that need their own contracts (2026-10-10).

**PS2-R4 — OC exits cleanly.** Both places where the OC driver raises after
exhausting settlement retries, in_session mode and recovery, instead:
- print one line naming the request and the cause;
- leave the request in its recorded terminal state;
- let the surviving CLI's lifecycle end normally;
- exit with a non-zero status and no traceback.

Servers still reopen admission through the requester liveness check.
Verified by: a test for each of the two failure paths, asserting the clean
exit, the one-line message, the terminal state and no traceback in stderr.

**PS2-R5 — an implementer can consult dev-advisor.** When a run calls
`consult`:
- **What is recorded.** The host durably records the consult relationship,
  binding the caller's run identity to the conversation child, before the
  child is admitted.
- **What it allows.** Admission and resumption of that child are allowed for
  that caller only, on the strength of this record. A mutable runner or tree
  parent field is never enough on its own.
- **What it does not allow.** It does not extend `steer_run`, `stop_run` or
  any other operation that shares today's guard. It gives the child no
  spawn capability.
- **Another caller** reusing the same conversation is refused `forbidden`,
  unless it holds its own record.
- **Unchanged.** The existing plan-subtree and host-authority checks are
  kept.
Verified by:
- implementer-deep consulting dev-advisor with the scheduler enabled, which
  succeeds;
- a second caller on the same conversation, refused;
- a run admitting a child that is not its own, refused;
- the consulting run trying to steer or stop the child through the new
  record, refused.

Reviewer inspection: the record cannot be written or altered by the caller.

**PS2-R6 — test runs clean up after themselves.**
- **What is removed.** `scripts/test-chunk.sh` removes the **effective**
  pytest basetemp when pytest exits, whether it passed, failed or was
  interrupted. That is the last `--basetemp` given, resolved through
  symlinks.
- **Which paths qualify.** Only directories directly under /var/tmp whose
  name starts with `pt-` are removed. Anything inside a git repository, or of
  any other shape, is kept, with a one-line notice.
- **Exit status.** pytest's exit status is preserved, interruption
  included. SIGKILL cannot be covered, and does not need to be.
- **Documentation.** AGENTS.md no longer tells agents to `rm -rf`, and
  documents the accepted path shape.
Verified by:
- script tests where pytest passes, fails and is interrupted, and the
  directory is gone each time with the status preserved;
- a repeated `--basetemp`, where only the last is removed;
- a symlink resolving into a repository, kept;
- a non-matching path, kept.

**PS2-R7 — the suite ignores ambient config.** No test picks up the
multiagents config of the directory pytest was started from, unless it
deliberately sets up config loading as its subject. Running the suite from
the project root, whose config enables the scheduler, and from a clean
worktree gives the same per-id outcomes.
Verified by: the t1 files run from a directory whose config enables the
scheduler, with the same outcomes as from a clean one.

Reviewer inspection: no other test depends on ambient config.

## Out of scope, deferred to later specs

- **4b, publication (PB).**
  - Publishing node branches that share history, with a defined observable
    for "once".
  - Recording an external publication, with an exact delta check.
  - Atomic journal and ref recovery.
- **4c, short tool outputs.** Short default forms for `wait_for_*`,
  `get_node` and `list_nodes`, with pagination rather than lossy summaries.
  Actionable holds, failures and questions are never suppressed.

## Decisions

1. Consult admission rests on a host-written record, never on a mutable
   parent field (PS2-R5, advisor 2026-10-10).
2. Publication and short outputs are split out of this batch (4b, 4c).
