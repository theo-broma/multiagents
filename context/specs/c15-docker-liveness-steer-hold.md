# C15 — docker liveness under load, and a steer hold that stop_agent clears: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Ticket:** bug-d1731b (blocking). It was written by bug-reporter ag-d9513b, will be fixed in-house and is never submitted. The evidence, the code references and the proposed fix are in the ticket.
- **Ids:** `LV-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer-deep, then the reviewer.

## Behaviours

**LV-R1: a recorded pid that is positively gone is confirmed dead under load.**
- **The verdict.** The docker liveness verdict for a run's recorded wrapper and agent pids is based on a direct identity check of those pids: `/proc/<pid>` absent, or a different start time from the one recorded.
- **Fallback.** A bounded session-wide scan is kept only where it is needed to guarantee that a wrapper's descendants are dead too (SR-R2). The implementer says which case needs it.
- **Unknown stays unknown.** A timeout, an unreadable `/proc` or a transport failure is still unknown. Unknown still means refusal (SR-R2).
- **Verified by:**
  - recorded pids that are absent give a dead verdict even when a scan-style probe would be slow;
  - a live recorded pid gives alive;
  - a timed-out probe gives unknown.

**LV-R2: probes do not leak.**
- Each docker liveness probe runs in its own process group, or session, inside the container.
- On timeout, the probe and all its descendants are terminated and reaped.
- **Verified by:** a probe forced to time out leaves no probe process behind, observed through the executing fake-docker harness or an equivalent seam.

**LV-R3: `stop_agent` settles any steer hold.**
- After `stop_agent` has acted on a run, no persisted `steer_cleanup` for that run keeps rejecting `steer_agent` with "previous steer or previous steer cleanup is still pending". This holds whether or not the hold has a `blocked_reason`.
- **If death is confirmed,** the next steer proceeds.
- **If death is still unconfirmed,** the stop records that fact, and the next steer is refused with SR-R2's explicit "not confirmed dead" reason, never the generic "pending" one. The run is never launched beside a possibly live predecessor.
- `stop_agent`'s result says which of the two cases applied.
- **Verified by:**
  - an unknown-liveness cleanup with no `blocked_reason`, then `stop_agent`: steer is not refused as "pending";
  - with the predecessor dead, steer proceeds;
  - with liveness still unknown, steer is refused with the SR-R2 reason, and the stop result says death is unconfirmed.

**LV-R4: a retry after a transient unknown works.**
- A steer refused for unknown liveness leaves no hold that blocks the next steer once liveness becomes known-dead.
- **Verified by:** the first probe gives unknown and the second gives dead, so the first steer is refused and the second proceeds.

**LV-R5: no regression.**
- A live predecessor still refuses relaunch (SR-R2).
- The existing SR, SF, SC and docker executor suites stay green.
