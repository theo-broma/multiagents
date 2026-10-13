---
status: ready
---
# Phase 7, part 2: one container per run

This plan was agreed with the user on 2026-10-13, in an init-agent session,
and reviewed by the advisor (ag-aa1387, turn 11). The requirements are in
`context/specs/phase7-part2-containers.md`: decisions IC-D1 to IC-D7 and
requirements IC-R1 to IC-R15.

## Apply now

- **Order.** Start part 2 **after** both findings plans have merged:
  `2026-10-13-findings-c1-c2-part1.md` and `-part2.md`. Part 2 rewrites
  `executor/docker.py` and the proxies, which those plans are still
  changing.
- **The contract.** The orchestrator writes the interface contract from
  `phase7-part2-containers.md` in its phase 2, citing the IC ids, and puts
  it to the advisor. The milestones at the end of the spec are a
  suggestion; the contract decides.
- **The adversary is mandatory** on every milestone that touches a
  boundary:
  - provisioning (IC-R1..R3);
  - the executor's signals and adoption (IC-R6..R8, the C15 cases);
  - credentials (IC-R4);
  - the network and the RPC transport (IC-R10, R11).
- **Live checks under real Docker are part of "done".** Agents have no
  Docker, so the orchestrator runs these from a `/var/tmp` worktree on the
  host, as for ST1/ST2:
  - the isolation suite (IC-R13);
  - the latency benchmark;
  - a host restart with a live run adopted.
- **The user's standing rules still apply:**
  - a reviewer after every implementer;
  - one implementer per item;
  - escalation to opus at round 3;
  - tickets are fixed in-house.
- **Amendments to `phase7-nodes-and-containers.md`, for the orchestrator to
  apply.** The initializer does not edit existing specs.
  - Part 2's status becomes "specified in `phase7-part2-containers.md`
    (IC-*), which supersedes PAC-R1..R13 and the C15 residuals".
- **BRIEF.** Add a "Phase 7 part 2" current-work section that points to this
  plan and the spec.
  - Record that **tmux step 2, as a supervisor, is withdrawn** (IC-D4,
    user, 2026-10-13). tmux stays as the viewer.
  - Record that the live node view is in scope (IC-D5).
- **The ledger.** When part 2 merges, set F31, F32, F34, F35, F64, F65, F69
  and F70 to `fixed`, or reconsider each one (IC-R15).

## Next phase

- **A microVM backend** (IC-D2), once checked on Linux without Docker
  Desktop.
- **Should C3 to C7 be reviewed?** They were never reviewed (`REPORT.md`).
  The user decides after part 2.
- **Still queued:**
  - DJ-b;
  - specs 2b, 4b and 4c;
  - the kimi-k2.6 server errors at startup;
  - the tooling queue in BRIEF;
  - the review's phases 2 and 3.

## Config changes

- **None applied by this plan.**
- **Proposed, for the contract to name.** The executor selection (IC-D7)
  and the per-run resource keys, in `project.yaml`:
  - the total memory budget;
  - the reservation and the hard limit per run;
  - the CPU weight per run, and an optional `cpus` cap.

  Proposed defaults:
  - a 20 GB budget;
  - 2.5 GB reserved and 6 GB as the hard limit per run;
  - CPU: an equal weight for every run and **no cap** (user, 2026-10-13);
    the optional cap is off;
  - `max_concurrent` stays 8.
- **No change to `agents.yaml`.** The current roster covers this work:
  codex, then opencode-zai implementers; reviewers on agy; the adversary;
  the advisor.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
