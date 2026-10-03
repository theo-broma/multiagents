---
status: ready
---
# Phase 7, part 1: plan nodes, composite nodes and the scheduler

This plan was agreed with the user on 2026-10-04, in an init-agent session,
and reviewed by the advisor (ag-aed397, turns 5 and 6). The requirements
are in `context/specs/phase7-part1-nodes.md`: decisions NS-D1 to NS-D7 and
requirements NS-R1 to NS-R19.

## Apply now

- **Open phase 7, part 1** as the current work:
  - **The contract.** The orchestrator writes the interface contract from
    `phase7-part1-nodes.md` in its phase 2, citing the NS ids, and puts it
    to the advisor.
  - **The work.** It runs tester → implementer → reviewer for each
    milestone of NS-R18, merging them progressively behind the feature
    gate.
  - **When it is done.** Part 1 counts as done only when the whole contract
    passes (NS-D6: the user wants everything delivered together, not in
    stages).
- **The adversary is mandatory** on three milestones, because each one
  touches host authority:
  - milestone 1: the scoped RPC and the caller's identity;
  - milestone 3: the host advancing `nodes/<id>`, under H1 and H3;
  - milestone 5: stop and resume for windows, including locks and slots.
- **The user's standing rules still apply:**
  - a reviewer after every implementer;
  - one implementer per item;
  - escalation to opus at round 3;
  - tickets are fixed in-house.
- **The user's corrections to `phase7-part1-nodes.md` (2026-10-04).** They
  override the spec wherever it says otherwise. Apply them to the spec
  before writing the contract.
  - **NS-R7, starvation.** A node waiting too long is reported to the
    **orchestrator**, as a `wait_for_nodes` transition. It is not reported
    to the user, and it is never aged automatically. The spec's wording
    already says this; the change confirms it.
  - **NS-R12, a loop at its maximum always calls on the orchestrator.**
    - There is **no `on_max` setting**: the loop stops, notifies the
      orchestrator, and waits.
    - The orchestrator then decides: relaunch, with a higher maximum or a
      different model or both; close the loop as exhausted; or anything
      else the node tools allow.
    - Withdraw `on_max: close` and `on_max: escalate`. The rule "round 3 →
      opus" becomes one of the orchestrator's decisions, which its
      instructions can describe; the engine does not automate it.
    - What stays: exhausted never means approved, the counter rules, and
      NS-R9 for a child that shares a session when its model changes.
    - In NS-R17, the user's example reaches its maximum, the orchestrator
      is notified, and it relaunches with another model. This replaces
      "reaching `escalate`".
  - **NS-R14, time zone.** The default is Europe/Paris. It is a setting
    in the general multiagents config: the global config, overridable per
    project. The contract names the key. A window may still name its own
    zone.
- **Amendments to `phase7-nodes-and-containers.md`, for the orchestrator to
  apply.** The initializer does not edit existing specs.
  - **PAC-R9** (the scoped host RPC) moves into part 1 as NS-R5. Part 2
    reuses it and adds the per-container transport.
  - **PAC-R4** is satisfied by NS-R5: a run that delegates creates nodes
    through the RPC. Part 2 keeps only the docker side: the scheduler
    creates the container.
  - **PAC-R12** (a durable identity for each run) is largely covered by
    NS-R6's attempt ids. Part 2 adds the container id and the repository.
  - **Part 1's status** in that seed becomes "specified in
    `phase7-part1-nodes.md`".
- **BRIEF.** Add a "Phase 7 part 1" current-work section that points to this
  plan and the spec. Mark the C13 live exercise (PN-R4a) as done: this plan
  is it, written by the initializer through `multiagents plan commit`.

## Next phase

- **Phase 7, part 2: one container per run.** It is specified with the user
  once part 1 is done, from `phase7-nodes-and-containers.md` part 2 (PAC-R*)
  as amended above.
- **tmux step 2** goes with part 2's run lifecycle.
- **Still deferred:** the review's phases 2 and 3.

## Config changes

- **None applied by this plan.** No change to `agents.yaml` is proposed: the
  current roster already covers the work. That means codex implementers,
  a reviewer on agy with a codex fallback, the advisor, and the adversary.
- **The feature gate** (NS-R18) will be a `project.yaml` key, which the
  contract names. It stays **off** until part 1 is complete.

## Notes considered

No user notes existed at the start of this session: `context/notes/` held
only README.md.
