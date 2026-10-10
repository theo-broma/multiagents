# Transient failures are classified, retried and held, never lost (TF)

In October 2026 the orchestrator stepped in by hand dozens of times on runs
that died for reasons that had nothing to do with their work. BRIEF.md has the
diagnoses under RF and RD4. The recurring cases:

- **codex at launch:** "workspace routing discovery failed", with the
  chatgpt.com MCP unreachable at startup.
- **SQLite at launch:** `start did not launch: OperationalError: database is
  locked`. The node ended `done/failed` with no hold and no notification.
- **Quota:** on 2026-10-10 a weekly usage limit was exhausted mid-run. Both
  implementers ended `done/failed` with no hold; the cause was only in
  stderr.
- **CLI crashes:** exit -6 (SIGABRT), 138 (SIGUSR1) or -15, after minutes of
  normal work.
- **consult:** a call that failed on a provider error was never retried.

Today only a structured HTTP 429 takes an automatic path: suspend, honour
`retry_after` or back off, hold after 6 tries, then resume the same session
(scheduler/rate_limits.py, engine.py). A loop also gets one crash retry per
round. Generic `failed` and `quota` results lose their cause at capture.

This spec builds on RR (relaunch-recovery.md). Every automatic retry starts
from RR-R3's validated recovery. Storage, backoff curves and the provider
pattern tables are the developer's choice, except where a behaviour below
fixes them.

## Behaviours

**TF-R1 — every failure keeps a classified cause.** When an attempt ends
other than `done`, it durably records a `cause`:
- **class**, assigned in this order of precedence, first match wins:
  1. `question`: the run emitted NEED_DECISION or NEED_INFO;
  2. `stopped`: the root or a parent stopped or cancelled it;
  3. `auth`: the provider reported missing or expired credentials;
  4. `refusal`: the provider refused the content;
  5. `spend_cap`: a configured spend cap was reached;
  6. `throttle`: a structured HTTP 429 or rate limit with a retry delay;
  7. `quota`: a usage-limit exhaustion, from a structured signal or from the
     provider's own usage-limit message;
  8. `launch`: failed before the provider process was confirmed running;
  9. `crash`: the provider process ended by a signal, or exited non-zero,
     after launch, with no provider error text;
  10. `transient`: a provider error matching that provider's configured
      list of transient errors;
  11. `other`: anything else.
- **provider, account and model** that ran it;
- **exit code and signal,** either of which may be null, for example before
  spawn;
- **an excerpt** of the provider's error text, at most 2,000 characters, with
  anything matching the configured secret patterns redacted;
- **reset time** when the provider announced one.

`get_node`, the hold detail, and `check_agent` / `collect_agent` show it.

Verified by: one test per class, driven by a fake provider, asserting the
class, nullable fields, the excerpt bound and redaction. A test where two
classes both match, asserting precedence.

**TF-R2 — one bounded automatic retry path.**
- **What is retried.** Attempts whose class is `launch`, `crash` or
  `transient` are retried automatically.
- **One retry owner.** `throttle` keeps today's 429 path and its own limit.
  The loop's per-round crash retry is replaced by this path, not stacked on
  top of it.
- **Budget.** Each activation has a durable retry budget, default 3 retries
  in total across all classes. A successful launch does **not** restore it.
  Only a root `relaunch_node` resets it, or a change of pins or input.
- **What a retry does.**
  - It goes through normal admission, so cooldowns, breakers and `models:`
    fallbacks apply.
  - It resumes the same session when the provider can. Otherwise it launches
    fresh from RR-R3's recovery.
  - It records the source attempt, the new attempt and the time.
- **Before retrying.** The predecessor process must be confirmed dead. If
  its liveness cannot be established, the node is held `liveness_unknown`
  instead.
- **What takes precedence.** A question, a cancellation, a root relaunch or
  close, and terminal holds all win over a pending retry, and cancel it.
  Loop round, sibling state, review designation, spend and caps are
  preserved.
- **When the budget is spent,** the node is held `retries_exhausted`, with
  the last cause in the detail.
- **Never retried automatically:** `question`, `stopped`, `auth`, `refusal`,
  `spend_cap` and `other`.

Verified by:
- a transient failure once, then success, where the node ends done;
- three crashes interleaved with successful launches, which hold at the
  budget;
- alternating classes, which still consume one shared budget;
- one test per never-retried class;
- a question raised during a pending retry, which cancels it;
- a loop child crash, retried once by this path and not twice;
- a retry that starts from the dead attempt's committed work.

**TF-R3 — a usage-limit exhaustion holds; it never fails.**
- **Hold.** An attempt of class `quota` holds the node `quota_exhausted`.
  The detail carries the account and the announced reset time, when known.
- **Cooldown scope.** The cooldown applies to the **account** that ran out
  (for example `codex-b`), not to its sibling accounts.
- **Rechecking.** A reset time is a time to recheck, not proof that quota is
  back. The account's usage is read at or after the reset, and when no reset
  is known, every 30 minutes by default. The node is released when routing
  finds a usable route: a recovered account, or a configured fallback.
- **On release.** The run resumes the same session where the provider
  allows it. A fallback on another provider launches fresh from RR-R3's
  recovery.
- **No fallback left.** If every route is exhausted, the node stays held,
  with the earliest known reset in the detail.
- **Throttles and spend caps are unchanged.** `throttle` keeps today's path;
  `spend_cap` keeps today's refusal.

Verified by:
- a fake provider printing a usage-limit message: the node is held, not
  failed;
- with a fallback configured: it relaunches there from the recovered work;
- without one: it stays held until the fake account's usage reads usable;
- a cooldown on one account leaving its sibling account usable.

**TF-R4 — failures around launch are caught, bounded and reconciled.**
- **Caught.** An exception while admitting, claiming or launching an
  attempt, whether in the engine or in the worker, is caught and recorded as
  class `launch`, with its type and message. Examples: database locked, a
  spawn failure, a `KeyError` in checkout reseat, a provider startup
  failure. Reseat failures that already hold today keep doing so.
- **Recording itself fails.** If the database is unavailable for the
  record, the write is retried, bounded. The next tick reconciles any
  attempt left claimed with no record.
- **A process may exist.** If a process may have been spawned, it is adopted
  or confirmed dead before any retry.
- **Not retried.** Cancellation and shutdown are not turned into retryable
  failures.
- **Never silent.** Retries follow TF-R2. A node never ends `done/failed`
  from a launch exception without a hold or a notification, and never loops
  unbounded.

Verified by:
- an injected transient launch exception that recovers;
- a persistent one, held after the budget with the exception text;
- a locked database at record time, reconciled on the next tick;
- a cancellation during launch, which is not retried.

**TF-R5 — `consult` retries transient failures within its deadline.**
- **When.** A `consult` whose agent fails with class `launch`, `crash` or
  `transient` is retried inside the same call, within the call's existing
  single deadline. Retries never extend that deadline.
- **How.** The same message is replayed on the same conversation when its
  session survives. Otherwise it goes to a fresh session, and the reply says
  so.
- **Partial output** from a failed try is kept in the run record and never
  returned as advice.
- **What is kept.** The conversation lock, cancellation, caps and
  authorisation stay in force between tries. The retries count against
  TF-R2's budget machinery.
- **When it gives up,** it returns an error naming the cause class.

Verified by:
- a fake conversational provider failing once, then answering;
- a persistent failure, where the error names the class;
- a deadline reached during the retries, which returns at the deadline;
- a lost session, where the reply notes the fresh session.

## Interface (decided 2026-10-10, after tester ag-931be6)

**Provider patterns.** In providers.yaml, each provider may declare:

```yaml
failures:
  transient: ["<regex>", ...]   # matched against the provider's error text and stderr
  quota:     ["<regex>", ...]   # usage-limit messages
  reset:     ["<regex with a named group 'reset'>", ...]  # extracts the announced reset time
```

Matching is case-insensitive, and a pattern list that is absent means no
match. Defaults ship for codex: "workspace routing discovery failed" as
transient, "You've hit your usage limit" as quota, and its "try again at
<time>" as reset.

**Retry and recheck timing.** In project.yaml:

```yaml
scheduler:
  retries:
    budget: 3                       # TF-R2, total per activation
    backoff_seconds: [30, 120, 600] # delay before retry 1, 2, 3; the last value repeats
  quota_recheck_seconds: 1800       # TF-R3, recheck interval when no reset is known
  hang_seconds: 1800                # reserved for spec 2b
```

**Where a reset time comes from.** In order:
1. a structured reset in the provider's result;
2. the provider's `reset` pattern;
3. the account's usage window `resets_at` from the budget reading;
4. otherwise unknown.

**Clock.** Every backoff, recheck and deadline in this spec uses the
scheduler's existing injectable clock, the one rate_limits uses. Tests drive
it rather than wait in real time.

## Deferred to a later spec (2b)

These belong to the same family but are each large enough to need their own
contract:
- hang detection that stops and retries a silent run;
- stop attribution: who signalled a run and why;
- AL: alias bindings versus relaunch pins, and the `KeyError` on a missing
  binding commit.

## Decisions

1. Quota holds rather than fails (TF-R3). A failed node loses its place in
   the plan; a held one resumes when capacity returns.
2. One shared retry budget per activation, not restored by a successful
   launch (TF-R2, advisor 2026-10-10). Otherwise repeated mid-run crashes
   retry forever.
3. The 429 path stays as it is; TF does not take it over.
