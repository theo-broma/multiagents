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
