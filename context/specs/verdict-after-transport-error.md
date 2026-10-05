# A complete verdict survives a transport error at the end of the stream (RV)

Source: plan `context/plans/2026-10-05-scheduler-first-real-use.md` §3 defect 2;
researcher ag-9e858d (node nd-c1b1de2b). User decision 2026-10-05: "Verdict
complet = succès".

Observed: agy-b reviewer runs (ag-8bc769, ag-0a7301, ag-0a5e9b) exit 0 and their
only `result` event has `status: "ERROR"` (API EOF, HTTP 503, "stream was
interrupted") while its `response` holds a complete review ending in a
`VERDICT(...)` line. `runner._classify` maps any non-success final status to
`failed` (runner.py:6963, 6982); `_provider_health_after` counts it as a failure
(runner.py:6629), and three in a row trip the breaker (tree.py:1194, 1216).

## Behaviours

**RV-R1.** A run whose process exited 0, whose final provider status is not a
success status, and whose final text contains a complete verdict — one that the
existing verdict parser (the one used today to read reviewer verdicts, for runs
and for node verdict children) accepts — is classified `done`, not `failed`.
The verdict it carries is the one used downstream (by the orchestrator and by a
node loop's verdict child) exactly as for a clean run.
Verified by: tests on recorded-shape streams (a `result` event with
`status: "ERROR"` and a response ending in `VERDICT(rejected, 2): …` and in
`VERDICT(approved, 0): …`), asserting status `done` and the parsed verdict.

**RV-R2.** The transport error is not lost: the run's result records that the
provider reported an error after the verdict, with the provider's error text,
where `collect_agent` / `check_agent` show it.
Verified by: a test on the persisted result.

**RV-R3.** Such a run counts as a success for the provider's circuit breaker
(it does not increment consecutive failures and resets them as a success does).
Verified by: a test that three such runs in a row leave the breaker closed.

**RV-R4.** Everything else keeps today's classification: no verdict, an
incomplete or unparseable verdict, a non-zero exit, a limited / refused /
truncated outcome, or a run whose final text is only the error.
Verified by: one test per case, asserting today's status and breaker effect.

**RV-R5.** The rule is not specific to agy: it applies to any provider.
Verified by: a test on a second provider's stream shape (or a parametrised test).

## Out of scope
- Retrying the provider call, and any change to how agy's stream is parsed.
