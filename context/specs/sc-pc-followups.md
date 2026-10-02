# Spend-cap and concurrency follow-ups — the contract

**Status:** contract, orchestrator, 2026-10-02.
- **Ids:** `SF-R*`, never renumbered.
- **Origin:** the residual P2s from the final reviews of SC (ag-e9dd77) and PC (round 8).
- **Related contracts:**
  - `context/specs/spend-caps.md`, especially SC-R4c;
  - `context/specs/provider-concurrency.md`.

## Behaviours

**SF-R1: a stopped run records the crossing that stopped it.**
- `Node.spend_cap_crossings` holds the id of the crossing that actually caused the stop. This holds even when the configured cap changed between the crossing and the stop, for example: crossed at $1, lowered to $0.50 before the sibling polled.
- Verified by: in that scenario, after `record_stop` fails and the server restarts, the recovered `spend_cap` event for the $1 crossing names the stopped sibling.
- Reviewer repro: `.multiagents/runs/ag-e9dd77/result.json`, finding 1.

**SF-R2: recovery uses historical stop evidence, not current status.**
- A node that was stopped by a crossing is named in the recovered event for that crossing, whatever its status now. It may since have been cancelled, steered, resumed or finished.
- Verified by: after a cap stop with a failed `record_stop`, a restart, then `stop()` (or a resume after a cap raise) on that node, the recovered event still names it.
- Reviewer repro: same file, finding 2.

**SF-R3: shutdown during steer does not leak the startup claim.**
- If server shutdown begins while `steer()` is stopping its predecessor, `_launch` refuses to start. Any startup half-open probe claim, PC reservation or supervision lock taken for that steer is released at once, not when the server pid exits.
- Verified by: after a refused-on-shutdown steer, the provider's probe claim is free and the PC slot count is back to its prior value, within the same process.
- Origin: PC round-8 review. See `runner.py` around the `_launch` shutdown refusal, before its cleanup guard.

**SF-R4: no regression.**
- All `test_sc_*`, `test_pc_*` and `test_rc_*` tests stay green.

## Revision after the advisor's check (2026-10-02, before tests)

**SF-R1/R2: cumulative history.**
- **Accumulate, never overwrite.** A node's crossing evidence is the **union** of every crossing id that actually stopped it. It is preserved across later stops, steers and resumes. Today the field is overwritten (`runner.py` ~5583).
- **Retry.** A failed evidence write is retried with the same rules as a failed stop record. It is retried at the next poll and at finalization, and recovery also derives evidence from the ledger's stop records.

**SF-R3: precise scope.**
- **What this covers.** Every way a steer can end before spawn:
  - cancellation during the predecessor stop;
  - the shutdown refusal;
  - every other pre-spawn refusal.
- **What is released.** Only **this steer's** own resources are released at once:
  - its probe claim;
  - its PC reservation;
  - a queue entry it had claimed, restored to its place.
- **What is kept.** The predecessor's cleanup hold stays until its death is confirmed.
- **Verified by.** Once the predecessor is dead, there is **no phantom slot**: the PC count equals the live runs. A slot count restored to its pre-steer value is not required.

**SF-R3a (orchestrator decision, 2026-10-02, after the tester's question).**
- **When it applies:** a steer that holds the half-open probe claim and ends before spawn, for any reason (cap, shutdown, cancellation, other refusal).
- **What it does:** it releases the claim as **neutral**. It is not a failure and it does not re-arm the cooldown, because nothing was launched and nothing was learnt about provider health.
- **Result:** the provider goes back to half-open with the probe free (`availability` is `None`), so the next start or steer can take the probe.
