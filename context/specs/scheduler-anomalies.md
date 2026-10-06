# The scheduler reports what is stuck instead of waiting silently (AN)

Source: scheduler first-real-use trial, 2026-10-06. The user proposed a 2-minute
check. User decision 2026-10-06: "Déterministe d'abord": a deterministic
check now, and a cheap LLM agent later only for ambiguous cases, as advice.
Advisor ag-aed397.

Existing: the per-run Supervisor already detects silence, wall clock, doom loop
and runaway steps for node runs too (`supervisor.py:285-369`,
`runner.py:5472-5481`, `6815-6823`, `scheduler/engine.py:423-426`). AN reuses
those detections and does not redefine "stuck".

Trial defects this closes:
- a refused admission produced no transition, and `starving` stayed empty;
- a held node gave no further signal;
- a supervisor trip on a node run was invisible in `wait_for_nodes`.

## Behaviours

**AN-R1.** The host scheduler runs a consistency check every
`scheduler.anomaly_interval_seconds`, which defaults to 120. It does no model
call and adds no measurable cost to an idle tick.
Verified by: a test with a short interval and a fake clock.

**AN-R2.** Each check emits an `anomaly` transition, with `detail.kind` and
evidence, for each of these:
- `run_stuck`: a node's current run has been marked stuck by the Supervisor
  (it carries the Supervisor's reason);
- `verdict_unrecorded`: a loop round is `unresolved_round` while its verdict
  child's text holds a parser-accepted verdict line. This is defence in depth
  for VR, and should not fire once VR holds;
- `admission_blocked`: a node has been ready but refused admission for longer
  than `scheduler.anomaly_admission_seconds` (default 600), with the refusal
  reason;
- `held_idle`: a node has been held for longer than
  `scheduler.anomaly_held_seconds` (default 600) with no transition since.

Verified by: one test per kind.

**AN-R3.** Anomalies are deduplicated. The same (node, kind) is not emitted
again until the node's state changes or the anomaly clears and comes back.
Verified by: a test across several checks.

**AN-R4.** `anomaly` transitions are durable and delivered by `wait_for_nodes`
like any other transition. A `wait_for_nodes` in flight returns on one.
Verified by: a test.

**AN-R5.** The check only reports. It never changes a node, a run or a verdict.
Verified by: a test that node and run state are byte-identical before and after
a check that emits anomalies.

## Out of scope (later, separate decision)
- Waking an orchestrator that is not connected. This needs an authorised
  launch/resume policy, singleton protection and a cooldown.
- The cheap LLM "sentinel" agent for ambiguous anomalies. It would be advisory
  only and would never record a verdict.
