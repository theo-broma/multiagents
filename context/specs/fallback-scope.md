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

## Revision after the advisor's check (2026-10-02, before tests)

The advisor (ag-d20e1e) traced the code.
- **Already true today.** `_routed_spec` (`runner.py` ~7127–7147) already rejects providers an agent does not name, except **family siblings**. Every current provider family is a singleton, so no shipped or project agent gains an unnamed fallback today, and the `models: null` implementers are already safe.
- **The narrative was overstated.** The defect section above overstates what the chain does. The root cause of bug-ac396a stays unconfirmed: most likely the agent's `models:` did name agy, and its reading was unknown. The parts of FS that remain are hardening, plus removing the family widening. They supersede FS-R1, FS-R2 and FS-R6 as follows.

**FS-R1 (revised): candidate set.**
- Candidates are exactly the agent's `provider` plus the keys of its `models:` map.
- The project `fallback_chain` adds no provider.
- **Ordering is unchanged from today:** the preferred provider first, then the agent's `models:` order. The chain's position only matters for where `defer` sits.
- **After every eligible candidate fails, the start is deferred,** even when the chain omits `defer`.

**FS-R2 (revised): no family widening anywhere.**
- Implicit family-sibling resolution is removed from every routing path: start, `_routed_spec`, consult, steer, and the chooser's family pool (`budget.py` ~1638).
- A family sibling is a candidate only if it is listed in `models:`.
- **Legacy sessions:** a session recorded on an unlisted sibling resumes on its recorded provider when steered. Its session is bound there and we never move it. It is never chosen for new work.
- Verified with a test config in which two providers genuinely share a family.

**FS-R6 (revised): no regression.** Agents route exactly as today, in the same order. The only difference is that unlisted family siblings are no longer reachable.

FS-R3, FS-R4 and FS-R5 stand.
