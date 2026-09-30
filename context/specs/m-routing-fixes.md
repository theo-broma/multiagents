# M: routing and admission fixes, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.
- **Source:**
  - the ticket triage by ag-72e868 (BRIEF.md, "Ticket triage");
  - ticket bug-ac396a;
  - the advice from the advisor (ag-322b14) on 2026-09-30.
- **Ids:** `RM-R*`. They are never renumbered.

## Behaviours

**RM-R1: a resumed `consult` respects `max_concurrent` (ticket 1g).**
- **The defect.** A consult that resumes an existing conversation skips the concurrency admission check. Today only the creation of a new conversation is checked (`runner.py` ~4524 and ~902).
- **What happens now.**
  - A resumed turn is admitted by the same occupancy rule as `start()`.
  - When the tree is full, it is refused with the same refusal text and shape as `start_agent`, and the conversation node stays idle and resumable.
- Verified by: with `max_concurrent` slots all occupied, a consult to an existing idle conversation is refused, and once a slot frees the same consult succeeds and resumes the same session.

**RM-R2: the agent's own fallbacks come before the project chain (bug-ac396a).**
- **Candidate order:**
  1. the agent's preferred provider;
  2. its same-family routes, as today;
  3. the providers named in the agent's `models:` map, in the order they are written;
  4. then the project `budget.fallback_chain`, skipping providers already listed.
- **Providers the agent never named.** A provider that appears only in the project chain is still eligible, so behaviour for agents with no `models:` map is unchanged.
- **The routing message.** It names the provider actually chosen, and says whether that provider came from the agent's own list or from the project chain.
- Verified by: an agent with `models: {claude: …}`, where the preferred provider is constrained and the project chain is `[opencode, agy, defer]` with all of them usable, routes to claude.

**RM-R3: unknown headroom ranks below known-good headroom, within a tier.**
- **Within a tier.** Inside one preference tier of RM-R2, a candidate with a known usable reading is tried before one whose reading is unknown.
- **Unknown stays eligible.** A candidate with unknown headroom remains usable.
- **Across tiers.** The order is never reshuffled across tiers.
- Verified by: two candidates in the same tier, the first unknown and the second known at 50%, route to the second; across tiers, an unknown first-tier candidate is still chosen over a known second-tier one.

**RM-R4: a stale "known" budget reading is demoted.**
- **When a reading is demoted to unknown for routing.** In either of these cases:
  - (a) the reading's reported reset time for the constraining window is in the past;
  - (b) the reading is older than `budget.max_reading_age_seconds`, a new project setting with a default of 3600. The age is taken from the reading's `stale_seconds`, or from its own timestamp.
- **Kept for display.** The reading's last values and its age stay available for display, marked stale.
- **The incident.** Codex read "weekly 100%, resets Oct 4" from rollout history 13 000 s old, although the window had already been reset.
- Verified by:
  - a reading at 100% whose `resets_at` has passed routes as unknown, so it is usable;
  - a reading older than the bound routes as unknown;
  - a fresh reading at 100% is still unusable.

**RM-R5: the model/effort pair is validated before launch (ticket 6).**
- **When.** After routing has resolved the provider and model, and before any worktree or process side effect, the pair `(model, effort)` is checked.
- **When it conflicts.** A provider may declare, in `providers.yaml`, that its model ids carry the effort, e.g. agy's `-low`, `-medium` and `-high` suffixes. For such a provider, an `effort` that contradicts the model's suffix is resolved by **dropping the effort** and letting the model name govern. An event records it.
- **No such declaration.** Behaviour is unchanged.
- **The incident.** `gemini-3.8-flash-medium` with `effort: low` was rejected by agy at launch: "--model gemini-3.8-flash-medium conflicts with --effort=low".
- Verified by: an agy spec with `model: gemini-3.8-flash-medium` and `effort: low` builds a command without the conflicting effort flag, and an event notes it.

**RM-R6: nothing else changes.**
- Agents without a `models:` map route exactly as today.
- The existing routing, budget and consult tests stay green, apart from the known reds.

## Out of scope

- agy pool splitting. It is a user decision, pending.

## Amendments of 2026-09-30, after the advisor (ag-d20e1e, gpt-6.1-sol). They supersede the text above where they differ.

**RM-R2a: the tiers, exactly.**
- **Tier A** is the preferred provider together with its same-family instances. It stays **one pool**, chosen by reservation, load and last use as today (`budget.py` ~1064, ~1139). It is not two tiers.
- **Tier B** is the agent's `models:` routes, in the order they are written, as one ordered tier. When a provider in Tier B has family siblings, the siblings join Tier B right after it.
- **Tier C** is the project `fallback_chain` entries not already listed. `defer` still ends the chain, after Tiers B and C.
- **Unchanged:** the existing short-reset wait before a cross-family fallback (`budget.py` ~1151).
- **"Eligible" means eligible under the existing `usable` / `_usable_spec` rules.** A cross-family project entry with no explicit or inheritable model still cannot run, as today.

**RM-R3a.** The known-before-unknown ranking applies inside Tier A's pool and inside Tier B. It never moves a candidate across tiers.

**RM-R4 is replaced by RM-R4b.**
- **The existing per-window reset rule stays as it is.** QF-R1 / `_apply_reset_margin` clears expired windows after the 120 s margin and recomputes the constraint. RM-R4(a) is withdrawn: demoting the whole reading could bypass another window that is still full.
- **Age.** A reading carries an age in seconds:
  - the provider's own `stale_seconds` when it gives one;
  - otherwise a new optional script field, `read_at` (epoch seconds), when it gives that;
  - on a cache hit, the age grows with the time spent in the cache. It is not frozen.
- **A reading whose age exceeds `budget.max_reading_age_seconds`** (default 3600, configurable) routes as unknown, and the raw reading stays available for display, marked stale.
- **A reading with no age information** is unchanged from today.
- Verified by:
  - a codex-shaped reading with `stale_seconds` 13000 at 100% routes as unknown;
  - the same reading with `stale_seconds` 60 is unusable;
  - a cached reading ages across cache hits;
  - an expired window is still cleared per window by the existing rule.

**RM-R5 is replaced by RM-R5a.**
- **The mapping.** A provider may declare `effort_suffixes` in `providers.yaml`, a mapping from an anchored model-id suffix to an effort, e.g. `{"-low": low, "-medium": medium, "-high": high}` for agy. A model id that matches no suffix has no implied effort.
- **Inherited effort.** When the effective effort was **inherited**, i.e. from the agent's top-level `effort` and not written on this provider's route, and it conflicts with the model's implied effort, it is normalised to the implied effort.
- **Explicit effort.** When the conflicting effort was **explicitly configured for this destination route**, the start is refused before any side effect, with a message naming the model, the effort and the route.
- **Persistence.** The normalised spec is what is persisted, and it is reused on steer and consult. An event records the old effort, the effective model and effort, and the reason.
- **Unchanged:** providers that declare no `effort_suffixes`.
- Verified by:
  - the agy incident case (inherited `low`, model `-medium`) launches with the effort `medium` and writes an event;
  - an explicit conflicting route effort is refused.

**RM-R6a.** Nothing changes except what RM-R1..R5b describe.

## Amendments of 2026-09-30, after the adversary (ag-9dffc6, `tests/test_m_adversary.py`). They supersede the text above where they differ.

**RM-R7: an effort refusal has no side effect.**
- **Before anything else.** The RM-R5a refusal happens before the startup claim or half-open probe is taken, and before any `startup.json` record, node, worktree or process exists.
- **On every exit path.** More generally, any refusal or exception in `start()` that happens after the startup claim was taken releases that claim.
- Verified by: `test_explicit_effort_refusal_does_not_hold_the_half_open_probe` and `test_explicit_effort_refusal_leaves_no_startup_run_record`.

**RM-R1a: the admission of a resumed consult is atomic.**
- **The rule.** A resumed consult that passes admission counts toward `max_concurrent` from that moment, not only once its process has started. The check and the reservation happen together, as they do for `start()`.
- **Two consults for one slot.** When two resumed consults compete for the last slot, exactly one is admitted and the other gets the RM-R1 refusal.
- **On failure.** A reservation whose launch fails is released.
- Verified by: `test_two_concurrent_resumed_consults_cannot_both_take_the_last_slot`.

**RM-R5b: explicitness belongs to the route, not to the provider's name.**
- **The rule.** When routing lands on a family sibling of the route the effort was written on, the effort is still explicit for RM-R5a. A conflict is refused there, exactly as it would be on the route's own provider.
- Verified by: `test_explicit_route_effort_is_refused_on_the_routes_sibling_too`.

**RM-R5c: what a value in `effort_suffixes` must be.**
- **Valid values.** Each value in `effort_suffixes` must be a non-empty string.
- **Invalid values.** A null or any other non-string value is a config error at load, like a non-mapping `effort_suffixes`. It is never turned into an effort string.
- **What reaches the CLI.** `--effort None`, or any effort that did not come from a valid configured value, never reaches argv.
- Verified by: `test_null_effort_in_effort_suffixes_never_reaches_the_cli`.

**RM-R4c: a stale reading feeds no prediction.**
- **What is excluded.** A reading that routes as unknown under RM-R4b is not added to the burn samples and never triggers `_wind_down` or a cooldown.
- Verified by: `test_a_stale_reading_does_not_wind_its_provider_down`.

**RM-R4d: time going backwards never makes a reading fresher.**
- **In the cache.** A negative elapsed time, from a wall-clock step backwards, expires the cache entry.
- **For a reading's age.** A reading's age never decreases and never freezes across such a step. The contract does not fix the mechanism; a monotonic clock is acceptable.
- **A future `read_at`.** A `read_at` more than 300 s in the future, which covers a value sent in milliseconds, makes the reading route as unknown. Clamping such a reading's age to 0 is not acceptable.
- Verified by: `test_cached_reading_age_is_not_frozen_by_a_backwards_clock_step`. The future `read_at` rule has no test yet; the reviewer checks it.

**RM-R3b: known before unknown inside Tier A, answering the adversary's NEED_INFO.**
- **The decision.** The current behaviour is kept. Inside Tier A's pool, a known usable reading is preferred to an unknown one before load and last use are compared.
- **Why.** An unknown sibling may itself be exhausted.
- **The docstring.** `pick_instance`'s docstring must say this. It must not say "with load and last-use equal".

**RM-R2b: disabled siblings never join Tier B.** A family sibling with `enabled: false` is never added to the agent's own routes.

**Test guards.** The guards in `tests/test_m_adversary.py` also cover:
- the persistence of the normalised effort across a consult resume in a new process, and on steer;
- a malformed `max_reading_age_seconds`, which falls back to 3600.

They must stay green.

## Amendments of 2026-09-30, after the round-3 re-review (ag-53986b). They supersede RM-R4d where they differ.

**RM-R4e: cache age, the exact rule.** This ends the cycle of patches.
- **What the age is measured from.** The cache observes the wall clock only when the entry is read, so the age is measured from those observations.
- **At every hit:**
  - the entry's age grows by `now - last_seen`, when that is ≥ 0;
  - `last_seen` becomes `max(last_seen, now)` and never moves backwards. This holds under concurrent hits too: updates to the entry are serialised, or use `max`.
- **An observed backward step.** When `now < last_seen` at a hit, the entry is **permanently expired**: the next access re-reads the provider. A later forward recovery never makes it authoritative again.
- **Accepted limit.** A backward step that happens and is recovered entirely *between* two reads cannot be observed. The age may then count less time than truly elapsed. This is accepted; a monotonic clock is not required.

**RM-R1b: cleanup after a failed launch is complete and survives a second cancellation.**
- **Where it applies.** Any exit between `executor.start()` succeeding and supervision being established.
- **What it must do, all shielded from a further cancellation (e.g. `asyncio.shield`, or cleanup in a `finally` that re-catches cancellation):**
  - stop the process;
  - release the supervision lock;
  - `occupancy.forget()` any registered container occupancy.
- **The order.** The slot or claim is released only after that cleanup has run.
