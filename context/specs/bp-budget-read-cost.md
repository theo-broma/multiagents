# BP — the cost of a budget read

Status: contract, orchestrator, 2026-10-01. Source: TS Run B (ag-6e59b2)
measured `budget.read_all` at ≈ 0.44 s per call: a full `config.load` plus
~17 YAML re-parses (`_fetching_allowed`, `_reset_margin`,
`_reading_age_bound_from_layers`), on every spawn and every poll (e.g.
`test_qf_r5_wait_keeps_waiting` 20 s for ~90 polls). Ids `BP-R*`.

**BP-R1 — one parse per read.** One `budget.read_all` call loads the
configuration at most once and parses each config file at most once; the
per-provider helpers reuse what that call loaded instead of re-reading the
layers.
Verified by: a test counting YAML parses / config loads during one
`read_all` over a project with ≥ 3 providers (bound: one load, each file once).

**BP-R2 — same answers.** For the same files on disk, every value
`read_all` returns (and every helper's result) is identical to today's,
including when a layer is missing, malformed, or changes between two calls
(a change on disk is seen by the next call — no cache across calls unless it
is invalidated by the file's mtime/size).
Verified by: the existing budget/quota/PS suites stay green; a test editing a
layer between two calls sees the new value on the second.

**BP-R3 — measurably cheaper.** `test_qf_r5_wait_keeps_waiting` and one
`read_all` over the shipped config each drop by at least half (report
before/after timings; not asserted in the suite).
