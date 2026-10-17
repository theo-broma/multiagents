# SF — a session id is only resumed on the provider family that issued it

Approved by the user on 2026-10-18. Source: ticket bug-fe6ab3. On 2026-10-18 a
consult to the advisor failed: a codex-b relaunch was handed an opencode
session id, and the codex wrapper exited 20 (`session id is not
UUID-shaped`). The failure is classed "other, never retried", so the
standing advisor stays unusable.

## Behaviours

**SF-R1 — one guard, at launch.**
- Every launch checks that a requested session belongs to the target
  provider's family. If it does not, the session is dropped. The check runs
  in `_launch` itself, so it covers every caller: the free retry, the
  commit-fix turn, the queued retry, consult and quota handover.
- A dropped session emits a `session_dropped` event. It records the reason
  `provider_changed`, both families and the run id, never the session id
  itself.
- Same-family siblings, such as codex and codex-b, still resume.
- Verified by:
  - each of the three unguarded paths, given an opencode-family id and a
    codex-family target, launches with no `--session`;
  - a codex to codex-b relaunch keeps `--session`;
  - the event is recorded.

**SF-R2 — the conversation is carried over.**
- When the session is dropped, the new provider's prompt is the cold-start
  continuation prompt that quota handover uses: the instructions, the task
  and the run's context, never a bare retry message.
- Verified by: a dropped-session launch's prompt contains the agent's
  instructions and the original task.

**SF-R3 — the stamp names the issuer.**
- The family stored beside a session id is the family of the provider that
  issued it, recorded when the id is first observed, not at finalize time.
- Verified by: a run that falls back mid-run stores the issuing provider's
  family.

**SF-R4 — a rejected session is recoverable.**
- A launch that a provider refuses because of its session id is classed as
  `session_lost`, which clears the id and relaunches fresh as today. It is
  never "other, never retried".
- The codex wrapper reports that refusal with a dedicated exit code.
- Verified by: a fake codex wrapper that rejects the session leads to a
  fresh relaunch, and the consult succeeds.

**SF-R5 — recovery of the current advisor.** The fix needs no manual
reset. The advisor's next consult, after the merge, goes through SF-R1 or
SF-R4 and recovers.

## Plan

1. tester (`tests/test_sf_*.py`);
2. implementer-deep, because the change cuts across runner retry paths
   (`runner.py` is also touched by 4C, though in other regions);
3. reviewer;
4. full suite;
5. merge, then resolve bug-fe6ab3 as fixed.
