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
