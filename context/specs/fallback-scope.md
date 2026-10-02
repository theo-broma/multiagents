# Fallback scope: an agent falls back only where it was told it may — the contract

**Status:** contract, orchestrator, 2026-10-02.
- **Ticket:** bug-ac396a. It is fixed in-house and never submitted.
- **Ids:** `FS-R*`, never renumbered.

## The defect

Routing builds the candidate set from four sources:
- the agent's provider;
- its `models:` keys;
- its provider family;
- the **project-wide** `budget.fallback_chain` (default `opencode, agy, defer`).

An agent can therefore be routed to a provider it never named. In bug-ac396a, an agent whose own fallbacks named only claude was sent to an exhausted agy. With the 2026-10-02 roster this matters more. The implementer tiers have `models: null`, the user's "no fallback" choice. The project chain would still route them to opencode or agy, which is exactly the silent fallback the user forbade.

## Behaviours

**FS-R1: candidates come from the agent's own configuration.**
- A provider is a routing candidate for an agent only if it is the agent's configured `provider`, or a key of its `models:` map.
- The project `fallback_chain` no longer adds providers. It only:
  - orders the agent's own candidates, those it lists first, in its order;
  - contributes its terminal `defer` behaviour.
- Verified by:
  - an agent with `provider: A` and `models: null` is never routed to B or C, even when the chain lists them and A is exhausted;
  - it is deferred instead.

**FS-R2: a provider's family does not widen the set.**
- An agent on `opencode-deepinfra` is not routed to `opencode` or `opencode-zai` because they share the opencode binary. A family member is a candidate only if it is listed in `models:`.
- Verified by: the same test as FS-R1, across family members.

**FS-R3: a pinned model or provider is unchanged.**
- An explicit pin keeps today's semantics: no fallback, with a refusal or deferral as now.

**FS-R4: the routing message names only real candidates.**
- `routing` in the start result never names a provider outside the agent's candidates.
- When everything is exhausted, the deferral reason lists the candidates that were tried.
- Verified by: assertions on `routing` and on the deferral reason.

**FS-R5: migration note.**
- Some agents in shipped defaults or this project may have relied on the chain for fallback. Their `models:` maps must list them explicitly.
- The implementer checks the shipped `defaults/agents*.yaml`. Where an agent lost all fallbacks because of this change, it lists the agent in its result rather than silently adding entries; the orchestrator decides.

**FS-R6: no regression.**
- Agents whose `models:` already list their fallbacks route exactly as today, in the same order.
