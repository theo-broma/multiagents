---
status: ready
---
# Test tiers: stop agent full suites now, test queues after part 2

Agreed with the user on 2026-10-17, in an init-agent session. The
requirements are in `context/specs/test-tiers-and-queues.md`: decisions
TQ-D1 to TQ-D5 and requirements TQ-R1 to TQ-R15. The measurement that
motivates it is in the spec's "Problem" section.

## Apply now

**1. Immediate rule: agents do not run the full suite.** This applies now,
before any tooling, and is independent of phase 7 part 2.
- **Implementers and reviewers.**
  - They run the tests their change touches: the changed and added test
    files, and the test files that import the changed modules (found by
    `rg`).
  - They also run the test families of the subsystem they changed, for
    example every `test_ap_*` for an auth-proxy change.
  - They never run the full suite, and never every chunk of it.
  - They report exactly what they ran.
- **The orchestrator's task wording** stops asking agents for "full suite
  = baseline". For tester-provided files, the existing "restore these
  files, do not commit" instruction stays.
- **One full suite per final candidate tree, run by the orchestrator
  only,** from a `/var/tmp` worktree through `multiagents bg run`, as
  today.
  - The tree includes the tester's files.
  - Any later change to the code or the tests invalidates that result.
  - Combine approved candidates into one suite when they are ready
    together.
  - The `MULTIAGENTS_SL_FULL=1` check (nc/c23/pc fixture changes) stays
    the orchestrator's.
- **New failures from that suite** go back to the loop as a new round with
  the failing ids.
- **Watching the risk.** The low yield of the orchestrator's suite was
  measured *after* the implementers' own suites had already filtered
  defects. Without that filter, more rounds are likely.
  - Count in BRIEF the rounds caused by the orchestrator's suite, and the
    suite hours.
  - If more than one candidate in three needs such a round, tell the user;
    the rule may need a tier-2 approximation (a few families) before
    hand-back.
- **Files to change:**
  - `AGENTS.md`, "Test suite" section. Today it says "agents should prefer
    targeted files unless the task asks for a full run". It becomes: agents
    never run the full suite; the orchestrator does, once per merge
    candidate. The orchestrator commits this change.
  - The role briefs: see **Config changes**.

**2. Test tiers and queues (TQ-R1..R15), after phase 7 part 2.**
- **Amendments to apply first.** The initializer does not edit existing
  specs, so the orchestrator applies both:
  - IC-R5 in `phase7-part2-containers.md`;
  - IC-C14 in `phase7-part2-contract.md`.

  Each gains the exception of TQ-D5: CPU, memory and pids caps for
  test-lane jobs only. They depend
on IC-R1 (bundles), IC-R5 (resources), IC-R8 (cleanup) and IC-R11 (the RPC
transport).
- **The contract** is the orchestrator's: it writes it from the spec,
  citing the TQ ids, and puts it to the advisor.
- **Milestone 0 (TQ-R14) comes first.**
  - It is a measurement on the host with nothing else running, so the
    orchestrator picks a quiet moment.
  - Its numbers set the lane defaults. They are shown to the user before
    the defaults are recorded.
- **Suggested slices:**
  1. the durations store and the estimate (TQ-R1, R2);
  2. the lanes, their containers and caps, and submission and results
     (TQ-R3..R6, R11);
  3. tiers 1 and 2 (TQ-R7, R8);
  4. the verification node and priority (TQ-R9, R10);
  5. observability and instructions (TQ-R12, R13, R15).
- **The adversary is mandatory** on slice 2 (isolation between jobs,
  escaping the cap, false greens) and slice 4 (restart and double merge).
  The spec lists the scenarios.
- **Once the verification node is merged,** the immediate rule's manual
  orchestrator suites end. Tier 3 replaces them.

## Next phase

- **The small lane's slot count.** The user chose one slot for each lane.
  The initializer raised a head-of-line risk: eight runs would share one
  `-n 1` slot. Milestone 0 measures the wait, and the user decides then
  (TQ-R14).

## Config changes

- **None applied.**
- **Accepted by the user on 2026-10-17: the role briefs.** These are the live
  prompts in `.multiagents/config/agents/`, which are untracked.
  - `implementer.md` and `reviewer.md` get a short paragraph with the
    immediate rule of item 1.
  - The tracked defaults in `src/multiagents/defaults/agents/team/` get the
    same paragraph through the normal pipeline.
  - The orchestrator applies the live ones now: the user said yes on
    2026-10-17.
- **Proposed by the spec (TQ-R3),** for the contract to name: a `tests:`
  block in `project.yaml`.
  - The lanes, each with `name`, `workers`, `slots`, `cpus`, `memory` and
    `max_estimate_seconds`.
  - The total test core budget.
  - The defaults, until milestone 0: `large` at 12 workers with 1 slot, and
    `small` at 1 worker with 1 slot, for 13 cores.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
