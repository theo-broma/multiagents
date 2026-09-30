# PS: providers that share tooling but keep their own models and quota, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.

**Source:** the user, on 2026-09-30 (translated):

> "Several providers, each defined by its own model list and its own quota, may share tooling: the same CLI for the communication interface (e.g. opencode) and the same authentication (e.g. a gemini subscription). Providers must be able to share tools while keeping models and quota separate."

**The first use:** split agy's Claude/GPT pool into its own provider. Today those buckets are reported `counted: false` by `agy.sh`, and they are never used for routing.

**Advice:**
- the advisor, ag-d20e1e, turn 2;
- the current-state map by the researcher ag-e9e343.

**Ids:** `PS-R*`, which are never renumbered.

## Facets, and how each is shared

| Facet | Mechanism | Status |
|---|---|---|
| CLI (bin, spawn, stream parser, action script) | `extends: <provider>` | Exists, unchanged. |
| Authentication (credential store, profile env, docker vault, login) | `auth_from: <provider>` | New. |
| Quota source (the budget reading) | `budget_from: <provider>` together with `budget_windows` | New. |
| Model list | each provider's own allowlist, the mechanism opencode-zai uses | Enforcement is new. |

A provider that declares none of these keys behaves exactly as today.

## Behaviours

**PS-R1: the `auth_from` declaration.**
- **What it names.** `auth_from: X` names the **credential owner**. X must be a declared provider.
- **Rejected at config load,** with a message naming the provider and the key:
  - X missing;
  - X being the provider itself;
  - X declaring its own `auth_from`, which rules out chains and cycles;
  - the dependent's `env` setting a key that X's `env` also sets.
- **A disabled owner** (`enabled: false`) still provides credentials to an enabled dependent. Disabling means "do not route runs to X", not "X's login is gone".
- Verified by: each rejection case, and a disabled owner with an enabled dependent that loads and authenticates.

**PS-R2: a dependent uses the owner's credentials, everywhere.**
- **Environment.** For launches and for script actions alike, the dependent's environment is:
  - the owner's `env`,
  - overlaid with the dependent's own non-conflicting `env` (see PS-R1).
- **Under the docker executor,** mounts, vault, seed/reset and credential refresh are allocated **once per credential owner**, not once per provider:
  - there is one backing and one refresh lock, both keyed by the resolved owner;
  - two providers sharing an owner never renew the same credentials concurrently.
- **Distinct logins stay distinct.** Distinct host and container logins, and `--account` selections, remain separate.
- Verified by:
  - a dependent run's command/env carries the owner's profile path;
  - under docker, two dependents of one owner produce one backing and one refresh lock;
  - two concurrent refreshes through two dependents perform one renewal.

**PS-R3: login and auth status follow the owner.**
- **Login.** `multiagents auth login <dependent>` runs the owner's login action and says so ("agy-partner uses agy's login").
- **Status.**
  - `auth_status` checks each credential owner once per execution context.
  - It reports each dependent with the owner's state and a field naming the owner (`auth_from`).
- **No logout.** This contract adds no logout action.
- Verified by:
  - login on a dependent invokes the owner's action once;
  - `auth_status` runs one check for owner plus dependent, and reports both.

**PS-R4: an authentication failure blocks the whole credential group.**
- **The group.** A credential group is an owner together with all its dependents.
- **Failure.** When the existing auth-failure rule trips for any member (same threshold as today, `runner.py` ~3155–3190), every member of the group is marked unauthenticated and paused together.
- **Recovery.** A successful re-login or check recovers every member together.
- **What stays per provider:** quota exhaustion, startup failures and provider_down.
- Verified by:
  - an auth failure on the dependent blocks the owner too;
  - recovery clears both;
  - a quota failure on the dependent leaves the owner usable.

**PS-R5: shared quota source, separate quota.**
- **The source.** `budget_from: X` means this provider obtains its raw budget payload from X's budget source. X must be a declared provider, and the same load rules as PS-R1 apply (no self, no chain).
- **One reading.** The raw payload is fetched once per cache period, per server process, and shared by X and its budget dependents. `refresh-quota` on any of them refreshes the payload once.
- **`budget_windows`.** A list of globs over window keys. It says which windows of the payload **count** for this provider.
  - It is allowed on any provider. On the owner, it restricts the owner too.
  - When it is absent, the counted flags in the payload are used as today.
- **Per-provider projection.**
  - Each provider projects the shared payload through its own selector.
  - It then recomputes its own headroom, severity, constraining window, reset and staleness from **its counted windows only**.
  - A window that is not counted for a provider never becomes its constraint, including in the reset-margin recomputation (`_effective_windows` / `_apply_reset_margin`).
  - Non-counted windows remain in the reading for display, marked `counted: false`.
- **No match.** A selector that matches no window in a payload makes that provider's reading **unknown**, never the owner's aggregate.
- **Overlap.** Overlapping selectors are allowed: both providers then count that window.
- **Staying per provider:** cooldowns, spend, wind-down, burn samples, headroom and routing decisions remain keyed by provider name.
- **Freshness.** The age rules (RM-R4b) apply to the shared payload's age.
- Verified by:
  - an agy-shaped payload with gemini and claude/gpt buckets, where agy (`budget_windows: ["gemini*"]`) is constrained only by gemini, and agy-partner (`budget_windows: ["claude*", "gpt*"]`, or whatever the real keys are) only by the others;
  - one budget-script invocation serving both;
  - a full partner bucket leaves agy usable;
  - a no-match selector gives unknown.

**PS-R6: a model runs only on a provider that allows it.**
- **At config load.**
  - A route in `agents.yaml` (primary or `models:` entry) whose model is outside the destination provider's allowlist is a config error.
  - The error names the agent, the route, the model and, when one exists, the provider whose allowlist does accept it.
- **At start admission.**
  - The same check applies to runtime model pins (the `model=` argument), resumes, consult resumes and deferred restarts.
  - A refusal there happens before any side effect (compare RM-R7), with the same message.
- **The catalog.** Entries retained from an older catalog that the allowlist now excludes are dropped from `models.yaml` on refresh.
- **No allowlist.** A provider without an allowlist allows everything, as today.
- Verified by:
  - a roster route to `claude-opus-4-6-thinking` on a gemini-only agy fails at load and names agy-partner;
  - a pinned start is refused with no node created;
  - a stale catalog entry is dropped.

**PS-R7: existing work is never silently re-routed.**
- **Deferred entries.** A deferred entry pinned to a model that its destination no longer allows is **refused** under DQ-R3. Its reason names the provider that now allows the model, if any.
- **Conversations.** An existing conversation whose node records a provider that no longer allows its model refuses to resume, with the same message. The orchestrator then starts a new conversation.
- **No substitution.** No model or provider is ever substituted automatically.
- **Historical accounting.** Spend, `by_model` and runs already recorded under `agy` stay as recorded, with no rewrite of history.
- Verified by:
  - a deferred entry pinned to an agy claude model, after the split, is listed as refused with the repair hint;
  - an idle conversation in the same position refuses to resume.

**PS-R8: the shipped configuration.**
- **In packaged `defaults/providers.yaml`, the new provider `agy-partner`:**
  - `extends: agy`, `auth_from: agy`, `budget_from: agy`;
  - its own `family: agy-partner`, so it never substitutes for agy, nor agy for it;
  - an allowlist of agy's non-gemini models;
  - `budget_windows` selecting the non-gemini buckets.
- **agy itself** gets a gemini allowlist and `budget_windows: ["gemini*"]`, or the real key pattern.
- **Keeping the current display.** `agy.sh`'s own `counted` flags are kept for backward compatibility.
- **Stated in the docs:** providers.yaml's comment block explains the three keys.
- Verified by: loading the shipped defaults yields both providers, with the separations of PS-R5/R6.

**PS-R9: nothing else changes.**
- Providers without the new keys route, authenticate and budget exactly as today.
- opencode / opencode-zai are unchanged. opencode-zai is a separate account: it uses `extends`, and neither `auth_from` nor `budget_from`.
- The existing suite stays green, apart from the known reds.

## Out of scope

- A logout action.
- Shared startup health. A missing binary still trips each provider separately.
- Restructuring providers.yaml into `tools:`/`accounts:` sections.
- Reading a quota across machines. "Once" means per server process.

## Roster changes (the orchestrator's, after merge)

Move every agy route with a claude/gpt model in `.multiagents/config/agents.yaml` to `agy-partner`.
