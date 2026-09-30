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
