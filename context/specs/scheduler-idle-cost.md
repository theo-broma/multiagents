# The idle scheduler costs almost nothing (SC)

Observed 2026-10-07: the host scheduler daemon used about 44% of a core with
nothing running (0 open nodes; ~270 done/cancelled nodes and attempts).
Researcher ag-97734f:
- Between ticks, the daemon checks for finished attempts every 0.1 s
  (`engine.completion_changed`, engine.py:384-409). Each check runs
  `SELECT id, record FROM attempts` and decodes every attempt record, finished
  ones included. That is about 2,700 JSON decodes per second while idle.
- Each 5 s tick then loads and decodes the whole `nodes` table about ten
  times (windows, rate limits, composites, attention mirror, notifier), and
  reads `tree.json` two or three times.

Cost must scale with live work, not with history. Data structures, indexes,
caching and query shape are the developer's choice.

## Behaviours

**SC-R1.** The between-tick completion check does no work per terminal
attempt or terminal node. With no claimed or launched attempt, it decodes no
attempt record.
It still wakes the loop early, exactly as today, when a claimed or launched
attempt reaches a terminal state.
Verified by: a store holding 2,000 terminal attempts and nodes. The number of
attempt records decoded per completion check is 0 with nothing active, and
stays bounded by the number of active attempts once one is active. The
existing early-wake tests stay green.

**SC-R2.** An idle tick's cost does not grow with history. On a store with
5,000 terminal nodes and attempts and nothing open, an idle tick (windows,
rate limits, composites, adopt, mirrors, notifier scan) is at most 2× the cost
of the same tick on a store with 50 such nodes. It produces the same
transitions, holds, attention file and notifications as before.
Verified by: a test that measures this with a deterministic proxy (records
decoded, or rows read), not wall clock. Plus the existing scheduler suite,
with unchanged per-id outcomes.

**SC-R3.** Nothing in the observable contract changes: tick cadence,
wake-on-completion latency, window and rate-limit evaluation, attention.json
content, notifications and anomaly scans.
Verified by: full suite with `-n 4`, per-id outcomes unchanged against main.

Decisions (orchestrator, 2026-10-07, on tester ag-1a3151's questions):
- "Idle tick" means a steady-state tick. The first tick after daemon start
  may read all history once, for example to build a cache. Later ticks may
  not.
- SC-R1's bound is at most 3 attempt-record decodes per active attempt, plus
  a constant 5 for the `tree.json` read.
- "Same transitions, holds, attention.json and notifications": this suite
  compares ticks within the suite and across history sizes. The per-id
  comparison against main is SC-R3's full-suite check.
- Counting every `json.loads` in the process is an acceptable proxy, since
  the fixture keeps `tree.json` small.

## Out of scope
- `runner._worktree_state` walking the whole worktree, including `.venv`, on
  each progress sample of an active run. It costs only while runs are
  active. Report it if you measure it as significant; do not change it here.
- Pruning or archiving old nodes or attempts (a data-retention decision).
