# Spend caps for metered providers — the contract

**Status:** contract, written by the orchestrator on 2026-10-01. It was
split out of `deepinfra-provider.md` (DI-R5) after the advisor's code check.
- **Ids:** `SC-R*`, never renumbered. A behaviour is retired by marking it
  withdrawn.
- **The user's words:** "plafond multiagents integre par provider (ici
  deepinfra) avec possibilite de override par modele. Le plafond est
  desactive par defaut l'utilisateur peut l'activer s'il le souhaite dans la
  config."

## Facts the contract rests on (advisor ag-d20e1e, 2026-10-01)

- `runner._consume` sees each step's cost as it streams (`runner.py` ~3425),
  and can already stop a run mid-stream (the parked-decision path, ~3483).
- Cost today lives only as per-node usage totals. `usage_by_model()`
  aggregates those totals (`tree.py` ~771–798). Timestamped stream records
  omit cost, and each turn's accumulator starts at zero. **Per-period spend
  cannot be derived from what is stored today.** That is SC-R2's job.
- Provider maps merge recursively across config layers; lists replace
  wholesale (`config.py` ~67).
- The opencode `step_finish` part carries a unique part `id` (`prt_…`) and a
  USD `cost`.

## Behaviours

**SC-R1: configuration.**
- **Where it lives.** Under a provider, in `.multiagents/config/providers.yaml`:

  ```yaml
  opencode-deepinfra:
    spend_cap:
      usd: 5.00          # absent or null: no provider cap
      period: day        # day | week | month; default day
      models:            # optional per-model caps
        "deepinfra/Qwen/Qwen3.8-Max": {usd: 2.00}
        "deepinfra/openai/gpt-oss-20b": {usd: null, period: month}
  ```

- **Off by default.** No shipped provider has a cap. With no `spend_cap`
  anywhere, nothing in SC-R3 to SC-R5 ever triggers.
- **Model caps are additional.** A model cap is checked **in addition** to
  the provider cap, never instead of it. `usd: null` on a model means that
  model has no cap of its own; the provider cap still applies to it.
- **A model cap alone.** A model cap with no provider cap is valid.
- **Periods.** They are UTC calendar periods:
  - `day` starts at 00:00 UTC;
  - `week` starts on Monday at 00:00 UTC;
  - `month` starts on the 1st at 00:00 UTC.
  A model without its own `period` inherits the provider's, and `day` if
  the provider gives none.
- **Values.** `usd: 0` is valid and means nothing is admitted. Every other
  value is a config error at load time, with a message naming the key:
  - a negative, non-numeric, boolean, NaN or infinite `usd`;
  - an unknown `period`;
  - a `models` key matching none of the provider's `models_include`
    patterns.
  An invalid cap is never silently ignored.
- **Not inherited through `extends`.** A cap belongs to the provider
  instance that declares it. A provider that `extends` a capped one starts
  uncapped.
- **Layering.** The usual recursive map merge applies. A higher layer
  removes a lower layer's cap with `usd: null`.
- **Re-read without restart.** A changed cap takes effect at the next
  admission decision.
- **Verified by:**
  - the validation matrix above;
  - a model cap alone;
  - the `extends` rule;
  - the null override.

**SC-R2: a durable spend ledger.**
- **What it records.** Every cost event observed on a **metered** provider's
  stream is appended durably to a per-project ledger. Each entry holds:
  - the UTC observation time (local observation time, not provider time);
  - the provider, the full model id and the agent id;
  - a dedup key, which is the stream's step id where the provider gives one;
  - the USD amount.
- **Deduplication.** A replayed or re-adopted stream (driver restart,
  adoption, a re-read transcript) never records an event twice.
- **What survives.** The ledger survives steer, retries, fallbacks, node
  cleanup and `discard_agent`. Money spent is spent, so a discarded run's
  spend still counts.
- **What never counts.** A `plan` provider's would-be cost (CX-C5) is never
  recorded, and never counts against a cap.
- **History.** There is no backfill. Spend from before the ledger existed
  has no timestamp and does not count. The first period after upgrade
  starts from zero, and `budget_status` says so while that period lasts.
- **Scope.** Spend is counted per project. Spend from other projects on the
  same account is not seen, and `budget_status` labels the figures "this
  project's accounting, not the provider's invoice".
- **Writers.** Concurrent writers (several runs, driver and server) never
  lose or corrupt an entry.
- **Verified by:**
  - a fake stream's costs land in the ledger with timestamps;
  - replaying the same stream adds nothing;
  - entries survive discard;
  - a plan provider records nothing;
  - concurrent appends are all kept;
  - the period sums are correct across day, week and month boundaries.

**SC-R3: admission.**
- **When it applies.** On every start, steer, resume, deferred restart and
  fallback selection.
- **Provider cap.** A provider whose period spend has reached its cap is
  unavailable.
- **Model cap.** A model cap is applied to each resolved candidate model. It
  makes only that model unavailable: it never cools or refuses the whole
  provider, and another model of the same provider stays admitted.
- **What happens on refusal.** Routing behaves as for an exhausted provider:
  - the next fallback is tried;
  - otherwise the task is deferred with the cause `spend_cap` and a
    restart time equal to the end of the period.
- **Resuming a capped session.** A pinned or resumed session needs its own
  model. If that model or its provider is capped, the session is deferred,
  never moved to a fallback.
- **Raising or removing a cap.** A deferred task is reconsidered at the next
  `wait_for_agents` or admission, without waiting for the period to end.
- **Verified by:**
  - refusal at the cap leads to the fallback, or to deferral with
    `spend_cap` and the period-end time;
  - a capped model leaves a sibling model admitted;
  - a resumed capped session is deferred;
  - raising the cap releases the deferral.

**SC-R4: a run that crosses the cap.**
- **The stop.** On the first observed cost event that brings a provider's,
  or a model's, period spend to its cap or past it, the run is stopped. So
  is **every other active run** drawing on the same capped provider, or on
  the same capped model.
- **The resulting state.** A stopped run ends in the resumable state
  `limited`, with the cause `spend_cap`. It is not failed and not
  cancelled.
  - Its branch, worktree and session are kept.
  - `steer_agent` resumes it once spend is under the cap again, because a
    new period started or the cap was raised.
- **Best effort, not a bound.** Charges already in flight when the event is
  observed, including concurrent runs' requests, may take spend past the
  cap. The contract does not promise a maximum overshoot. A strict bound
  would need provider-side control, which is out of scope.
- **The event.** A `spend_cap` event is recorded once per crossing. It names
  the provider, the model when the cap is a model cap, the cap, the spend
  and the agents stopped.
- **Verified by:**
  - a fake stream whose steps cross the cap stops the run as `limited` with
    `spend_cap`;
  - a second active run on the same provider is stopped too;
  - an active run on an uncapped sibling model is not stopped by a model
    cap;
  - after the cap is raised, `steer_agent` resumes on the same session;
  - exactly one event is recorded per crossing.

**SC-R5: visibility.**
- For each capped provider and model, `budget_status` shows:
  - the cap and the period;
  - the spend in the period and what remains;
  - the reset time;
  - whether admission is currently refused.
- Uncapped metered providers show their period spend too (day, week and
  month), without a cap.
- **Verified by:** `budget_status` output under a seeded ledger, both
  capped and uncapped.

**SC-R6: no regression.**
- With no cap configured, routing, stopping and `budget_status`'s existing
  fields behave exactly as before. The only additions are the ledger and
  the new period-spend fields.
- Configured caps deliberately change admission and enforcement for
  metered providers only.
- **Verified by:** the full suite. The only reds are the 72 known phase-2
  ones.

## Out of scope

- Reconciling with a provider's billing API or invoice.
- Caps across projects or accounts.
- Caps on tokens rather than USD.
- A strict upper bound on overshoot.

## Amendments after the advisor's second check (2026-10-01, before tests)

These override any earlier wording they contradict.

**SC-R3a: no automatic resume of a capped session.**
- Deferral with `spend_cap` applies to **fresh starts only**, using the
  existing deferred-start path.
- A session stopped by a cap (SC-R4) is **never** re-launched
  automatically. A `steer_agent`, consult or resume on a session whose
  model or provider is still capped is refused immediately. The error
  names:
  - the cap that binds;
  - the spend;
  - the reset time. When several caps bind, it is the latest of their
    resets.
- Once every applicable cap permits it, the same `steer_agent` resumes the
  same node and session, also after a server restart. A node with no
  captured session id is reported "not resumable", as today.
- This replaces the "resumed capped session is deferred" bullet in SC-R3.

**SC-R3b: every launch is guarded, not only public admission.**
- The cap check runs immediately before **every** spawn of a metered
  provider's CLI. That covers automatic retries, wrap-up turns, fallbacks
  and deferred restarts. A launch admitted just before a crossing is
  therefore refused at spawn.
- Cap edits are seen at the next check. A deferred start whose cap was
  raised or removed is restarted at the next `wait_for_agents`, before its
  stored due time.

**SC-R4a: the stop, precisely.**
- **The verdict.** A cap stop finalises the run as `limited` with
  `reason: spend_cap` and `until` set to the latest applicable reset. That
  verdict:
  - never counts toward failure breakers;
  - never sets a provider quota cooldown;
  - never triggers an automatic retry or fallback.
  The branch and worktree are kept.
- **Across processes.** The crossing writes a durable stop request scoped to
  the capped provider or model. Every active run in this project draws on
  that scope, and every one of them is stopped within 15 s, whichever
  process (root or nested MCP server) owns it. Each runner acknowledges the
  stop durably.
- **A lowered cap.** Lowering a cap below the period's spend stops active
  runs at their next observed cost event, and refuses new launches.
- **One event per crossing.** One `spend_cap` event is recorded per
  (scope, period start, cap value). Simultaneous provider and model
  crossings give one event each.

**SC-R2a: the ledger's integrity.**
- **One locked transaction.** Deduplicating, appending, updating the period
  aggregate and claiming the crossing all happen in one transaction, under
  an interprocess lock that is stable across processes. The ledger is a
  dedicated store, never the tree's event log.
- **Dedup key.** `(provider, session id, step id)`. When a step id is
  absent, the key is `(agent id, turn, stream position)`. A replay keeps
  the first observation time.
- **Ordering.** A charge is committed **before** any replay or adoption
  checkpoint moves past it.
- **Torn writes.** A torn last entry is recovered from: it is ignored or
  completed, and never corrupts earlier entries.
- **Fail closed, only when a cap is configured.** If a cap applies and the
  ledger cannot be read or written, launches under that cap are refused
  with the cause `spend_cap_unreadable`. With no cap configured, ledger
  errors are reported once and never block anything.
- **The upgrade.** The ledger's creation time is persisted. Every period
  that began before it is reported as partial in `budget_status`.

## Decisions during review (orchestrator, 2026-10-02)

**SC-R3c: admission reads the current cap, not a cached one.**
- **Which caps apply.** Every admission point uses the caps as currently written in the config files, read at the moment of the check: public admission, the spawn guard right before `executor.start()`, queued and deferred resumes, steer, consult and commit-fix launches. A cached copy is allowed only if its file mtime and size are re-validated at each check.
- **Effect.** A lowered cap that another process has already crossed binds in this process at its next launch. A raised cap admits again.
- **Rejected alternative.** Refusing on any crossing record regardless of the current cap would block after a genuine cap raise.

**SC-R4b: a crossing binds only within its period.**
- A crossing recorded in a period that has since ended does not stop runs in the new period. The spend that crossed it no longer counts against the new period's cap.
- A run that has not observed the crossing before the period rolls over keeps running. This is correct, not a missed stop: the run would be re-admitted at that instant anyway.
- The 15 s stop window of SC-R4a applies only while the crossing's period is current.

**SC-R4c: the crossing event's agent list.**
- **Normal case.** The `spend_cap` event names the agents stopped because of that crossing.
- **Recovered event.** When the event is recovered by a watcher after the claimer died, it carries `recovered: true`. Its agent list is the agents recorded as stopped for that crossing id: each stopping run records its own stop against the crossing id in the ledger. It is not the agents active at recovery time.
- **Write failures.** An announcement is recorded as done only after the event write succeeded. A failed event write leaves the crossing unannounced, so a later watcher retries.
