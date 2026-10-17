---
status: ready
---
# Definition of v1

Agreed with the user on 2026-10-18, in an init-agent session. Until now the
brief ("multiagents repairs itself") had no finish line. This plan sets one.
It adds no work of its own; it says when the orchestrator may announce
"v1 done" and what remains outside v1.

## Apply now

**1. Record the definition in BRIEF.** Add a short "v1" section near the
top that points to this plan. v1 is done when **every** criterion below
holds. Each one is checked against evidence, never asserted.

- **V1. Phase 7 part 2 has switched over** (`phase7-part2-containers.md`,
  IC-D7).
  - Slices 1 to 7 are merged.
  - The live checks under real Docker are green on main: the IC-R13
    isolation suite, the latency benchmark and a host restart with a live
    run adopted.
  - The shared mode is removed.
  - The ledger shows F31, F32, F34, F35, F64, F65, F69, F70 and F205
    `fixed`, or each one carries a recorded decision.
- **V2. Every role in the live roster runs in isolated mode,** or the user
  has decided what happens to it. Contract 4C currently marks agy
  unsupported in isolated mode, and codex as well until IC-C34 has its
  verdict (around 2026-10-27). Yet reviewers run on agy and implementers on
  codex.
  - Before slice 7's switch-over, the orchestrator puts the situation to
    the user through the decisions queue. The options are a roster change,
    a supported path, or keeping a documented exception.
  - It never removes the shared mode while a role in the roster would stop
    working.
- **V3. Test tiers and queues are in place** (`test-tiers-and-queues.md`).
  - TQ-R1 to TQ-R15 are merged, with milestone 0's numbers recorded and
    the lane defaults set from them.
  - The full suite no longer runs inside agent runs or by hand: the tier-3
    verification node runs it.
- **V4. The user decisions queue is complete** (`user-decisions-queue.md`):
  UD-R1 to UD-R12 are merged, and the orchestrator's waits go through it.
- **V5. C3 to C7 have been reviewed, and the findings acted on.**
  - A review phase covers the contexts `REPORT.md` lists as never reviewed.
    The user chooses its scope at its start.
  - "Acted on" has the review's own meaning: `list_findings` reports
    `done`, with no finding open, scheduled or regressed.
- **V6. The 70 known `test_phase2_*` reds are resolved.** The deferred
  phase 2 review is done: each red is either fixed or deleted with a
  recorded reason. `context/ts/manifests/baseline-reds.txt` is then empty,
  or holds only ids the user has explicitly accepted.
- **V7. Nothing known is left untriaged.** Every item below either belongs
  to a v1 criterion or has been set aside by the user, with the reason
  recorded:
  - DJ-b;
  - specs 2b, 4b and 4c;
  - the kimi-k2.6 startup errors;
  - the tooling queue in BRIEF;
  - the open tickets.
- **V8. Main is clean.**
  - A full suite on main has no red outside the accepted list of V6.
  - No ticket is open.
  - No node is held or failed without a decision.
  - The unmerged branches and worktrees are triaged, as in the 2026-10-15
    cleanup.

**2. Announcing it.** When the orchestrator believes every criterion holds,
it does not announce v1 itself.
- It opens a decision for the user through the queue (UD), with one line
  of evidence per criterion: commits, suite job id, ledger counts.
- v1 is done when the user answers yes. BRIEF then records the date and the
  commit.

**3. Tracking.** At each handoff, BRIEF's v1 section shows each criterion's
state in one line: done, in progress or not started.

## Next phase

These are **outside v1**. They are kept as ideas and are not scheduled:
- the microVM backend (IC-D2);
- the graphical interface the user imagines, which builds on UD-R9's API;
- the parked ideas in the initializer's memory: Handwired (state
  invariants, scheduler replay, reference-model testing) and the
  ma-commands mod;
- a channel that proves a human answered (UD-R4's residual).

After v1, work continues as continuous improvement, one plan at a time.

## Config changes

None.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
