# Test tiers and test queues (TQ)

Requested by the user on 2026-10-17, in an init-agent session.

## Problem

The cost is in how long an agent waits to learn whether its change works,
more than in the suite itself. The suite was measured on 2026-10-14
(`context/specs/ts-test-speed.md`): about 50 min wall at `-n 4` and about
12,650 worker-seconds. The time is real process integration, spread across
thousands of tests. Run D found no cheap large win.

**Measured on 2026-10-13 and 2026-10-14** (initializer, from the run streams
and the bg registry):
- **44 full suites for about 13 merged items,** about 3.4 per merge. Only
  one is needed: the orchestrator's, before merge.
- **The agents ran 33 of them, 25.4 h in all:**
  - implementer: 17 suites, 13.2 h, **73 % of its run time**;
  - implementer-deep: 10 suites, 7.9 h, 32 %;
  - reviewer: 5 suites, 4.1 h, 21 %;
  - the orchestrator: 15 suites, 12.7 h.
- **Up to 3 full suites ran at once,** with two or more for about 9 h. The
  longest single wait was 90 min.
- **What the agents' suites found:** almost never anything that the
  orchestrator's suite did not also find. The orchestrator's suite was the
  baseline, or the baseline plus one flake, every time but one: RP14, with
  two regressions.

## Decisions (user, 2026-10-17)

- **TQ-D1. Three tiers.**
  - **Tier 1, immediate:** the tests the change affects, in seconds to a few
    minutes.
  - **Tier 2, wide:** a defined broader set, before an agent hands back.
  - **Tier 3, complete:** the full suite, once per merge candidate.
- **TQ-D2. The full suite never runs inside an agent run.** Tier 3 is a
  scheduler verification node with no LLM. The agent hands back after tier
  2 and does not wait.
- **TQ-D3. Test queues.**
  - Every test run on the host goes through a lane. A lane has a number of
    pytest workers and a number of concurrent jobs, its slots.
  - The number of lanes and their sizes are configurable.
  - **The default is the user's:** a `large` lane at `-n 12` with one slot,
    and a `small` lane at `-n 1` with one slot, so 13 cores for tests.
  - Milestone 0 measures the defaults before they are kept (TQ-R14).
- **TQ-D4. The launcher estimates the job's size from recorded test
  durations** and picks the lane.
- **TQ-D5. A lane's CPU, memory and process count are capped by the
  system,** not only by its worker count. Our tests spawn child processes
  (git, the fake CLIs), so 12 workers can use more than 12 cores and many
  processes.
  - This is an explicit exception to IC-R5's "a weight, not a cap"
    (`phase7-part2-containers.md`, around line 163) and to IC-C14
    (`phase7-part2-contract.md`, around line 868).
  - The exception covers test-lane jobs only, never agent runs. Both are
    amended through the plan.

## Requirements

**TQ-R1. Durations store.**
- **The record.** Per test id: the last measured call+setup+teardown
  seconds, the date, and the worker count it was measured at.
- **Where it lives.** In host state, not tracked by git.
- **Bootstrap** from the `--durations=0` log of 2026-10-14, which is
  gitignored on the host at
  `.multiagents/handoff/suite-durations-full-2026-10-14.log`, or else from
  milestone 0's first full run.
- **Updates.** Every tier-3 run updates it. A test with no record is
  estimated at its file's median, else the suite's median.

**TQ-R2. Estimate and lane choice.**
- **The estimate** is the sum of the selected tests' durations, divided by
  the lane's workers, plus a fixed per-job start cost that milestone 0
  measures.
- **The lane.** A job goes to the smallest lane whose `max_estimate_seconds`
  admits it. `--full` always goes to the largest lane.
- **The result** states which estimate and which lane were used.

**TQ-R3. Lane configuration.** In `project.yaml`, a list of lanes with:
- `name`, `workers` (the `-n` value) and `slots`;
- `cpus` (the cgroup cap, default `workers` plus a configurable margin);
- `memory` and `pids` (cgroup limits);
- `max_estimate_seconds`;
- a total test core budget. The sum of the lanes' `cpus` × `slots` must not
  exceed it, and the config is refused with a message otherwise.

**TQ-R4. Submission is non-blocking.**
- **Submitting.** `multiagents test submit [targets...] [--full]
  [--overlay <commit> <paths...>] [--wait]` comes from an agent's container
  through the per-container RPC transport (IC-R11), or from the host. It
  returns at once a job id, the lane, the queue position and an ETA.
- **Waiting.** `--wait` is allowed only when the ETA is under a
  configurable bound, by default 10 min. The CLIs' own timeouts are
  shorter than a queued full suite: agy cuts at 1 h, and codex has its own
  command timeouts.
- **Follow-up.** `multiagents test status|result|cancel <id>`.

**TQ-R5. What is tested.**
- **Bound to an exact input.** A job is identified by:
  - the submitted commit;
  - the overlay (source commit and paths);
  - the test selection;
  - the environment switches (`MULTIAGENTS_SL_FULL`, Docker-gated tests).

  A result is valid only for that exact input. Any later change to the code
  or the tests invalidates it.
- **Always a commit,** never a live worktree. The launcher makes a bundle
  of the submitted commit (IC-R1's mechanism) and tests it in a fresh
  clone, inside a lane container.
- **The overlay.** `--overlay` copies named paths from another commit over
  the clone without committing them. This replaces the manual "restore the
  tester's test files uncommitted" dance in BRIEF. The overlay is recorded
  in the result.
- **The basetemp** is private to the job, on real disk, outside any git
  repository, and without the `ag-` prefix (see `AGENTS.md`). It is
  removed when the job ends.
- **No egress** by default.

**TQ-R6. The result.**
- **Every job:** pass, fail and skip counts; the ids that failed; wall
  time; lane; estimate against actual; the overlay.
- **The comparison with the baseline.** The failures are compared with
  `context/ts/manifests/baseline-reds.txt` and sorted into:
  - new failures;
  - baseline reds still red;
  - baseline reds now green.
- **Flakes.** Each new failure is rerun once alone, in the small lane.
  - **A pass on that rerun is evidence of sensitivity to parallelism or
    load, not proof the failure is harmless.** The result keeps the first
    failure, with its output, under "failed in parallel, passed alone". It
    never reports a pass.
  - **Recognised flakes.** Such a failure is waived automatically only when
    its test id and failure signature match an entry in a tracked list of
    known flakes. Anything else is shown to the orchestrator as a
    decision.
- **Delivery.** The result reaches the submitting run through the RPC, as
  data the agent can read. Agents cannot see host `/var/tmp`. The full log
  stays on the host, with its path in the result.

**TQ-R7. Tier 1, impacted tests.** `multiagents test impacted [--base
<rev>]` lists the tests related to the files changed since the base.
- **Sources:** the changed test files themselves; tests that import the
  changed modules, through the import graph; and the naming conventions
  (`test_<id>_*` for a spec id the change cites).
- **It is advisory.** It is never a merge gate, because many tests run the
  code in child processes and no import or coverage analysis sees them.

**TQ-R8. Tier 2, wide.** `multiagents test submit --tier wide` is the
impacted tests plus every test whose recorded duration is under a
configurable bound, by default 1 s.
- **The arithmetic.** About 9,700 tests together carry about 2,650
  worker-seconds of setup, call and teardown. That is about 221 s at
  `-n 12`.
  - This is an ideal figure: it assumes no idle workers and no start cost.
  - Milestone 0 measures the real set, from the summed per-test durations,
    and then its wall time.
- **Hand-back.** An implementer hands back after a green tier 2.

**TQ-R9. Tier 3, the verification node.**
- **What it is.** A scheduler node kind with no LLM. It runs the full
  suite in the largest lane on the candidate commit, after the reviewer's
  approval.
- **A clean result** (no new failure; flakes reported) makes the candidate
  ready to merge.
- **New failures** open a new round of the loop. The implementer gets the
  list of failing ids and the log excerpts, as data.
- **Combined candidates.** The orchestrator may verify several approved
  candidates together as one combined tree, as it did by hand for PS and
  BT. The result names every candidate.
- **The `MULTIAGENTS_SL_FULL=1` check** for nc/c23/pc fixture changes
  (`AGENTS.md`) is a tier-3 option, chosen by the paths the change
  touches.
- **What tier 3 does not replace.** The live checks under real Docker,
  IC-R13 (`phase7-part2-contract.md`, around line 205), stay separate. A
  lane has no Docker.

**TQ-R10. Priority and fairness.**
- **The order.** In each lane, tier-3 jobs go before tier-2, and tier-2
  before tier-1. Within one class the order is FIFO.
- **No starvation.** A waiting job gains priority with age. A job is never
  pre-empted once started.
- **Shown.** The queue order is visible (TQ-R13).

**TQ-R11. Lifecycle.**
- **Durable queue.** It survives a host restart. A job that was running
  when the host stopped is reported `interrupted` and requeued once.
- **Timeout.** Each job has a timeout derived from its estimate, with a
  floor.
- **Impossible jobs are refused at submission,** with a reason. Examples:
  the estimate exceeds every lane, the commit is unknown, or the overlay
  touches `src/`. They are never left queued.
- **Submission ids are durable.** Resubmitting with the same request id
  returns the existing job.
- **Cancellation.** A job is cancelled when the run that submitted it ends,
  unless it is a tier-3 job owned by the scheduler.
- **Cleanup.** Containers and basetemps are removed. Orphans are reported,
  as IC-R8 requires for run containers.

**TQ-R12. Agents never run pytest outside the queue on the host.** Inside
their own container, agents may run a single test file directly. It is
cheap, and tier 1 must stay instant. Anything bigger goes through
`submit`.
- **The bound.** A configurable maximum number of test ids per direct run.
- **Enforcement.** By the instructions, and reported when the RPC sees a
  direct full run, the way the git guard reports.

**TQ-R13. Observability.** The monitor shows:
- the lanes, each with its running job and its queue (position, owner,
  tier, ETA);
- recent results, with estimate against actual.

`/api/tests` exposes the same data to the future GUI.

**TQ-R14. Milestone 0, measure before choosing defaults.** On the host,
with nothing else running:
- **The full suite alone at `-n 8` and at `-n 12`,** in a lane container
  with its CPU cap:
  - wall time;
  - worker-seconds;
  - the per-id outcomes compared with the serial manifests (TS-R1 parity);
  - any new flake.
- **The tier-2 set** at `-n 12`: is it under 5 min?
- **The per-job start cost** (bundle, clone, container).
- **Throughput and tail wait with both lanes busy.** Replay one busy hour
  of 2026-10-13 or 2026-10-14 from the run streams: the tier-1 jobs in the
  small lane while a full suite runs in the large one.
  - Measure the median and the 90th-percentile wait in the small lane.
  - If the 90th percentile exceeds 2 min, raise the slot count with the
    user.
  - Measuring idle-machine speed and parity alone does not validate the
    split of capacity.

The defaults are set from these numbers and recorded in this spec's
amendment.

**TQ-R15. Instructions.** `AGENTS.md`, the implementer, reviewer and tester
briefs, and the orchestrator's task wording describe the tiers:
- an implementer hands back after a green tier 2;
- a reviewer runs tier 1 on what it checks and reads the tier-2 result,
  and never the full suite;
- the orchestrator relies on tier 3, and does not rerun it by hand.

## Out of scope

- Making individual tests faster. That is closed (TS, 2026-10-14).
- A remote CI.
- Selecting tests by coverage as a merge gate (TQ-R7).

## Adversarial scenarios to close

- **Wrong code:** a submitted commit that is not the one tested, or an
  overlay that changes `src/`.
- **Leaks between jobs:** a job's basetemp or clone visible to another job,
  or to another run's container.
- **Escaping the cap:** a job that exceeds its lane's CPU cap through child
  processes, or one that keeps running after cancellation.
- **A false green:** a result reported green while pytest crashed or
  collected nothing, or a flake reported as a pass.
- **A host restart** while a tier-3 job is running: no double merge, and
  no lost result.
- **The queue:** one agent flooding it, against TQ-R10's fairness.
