# opencode-deepinfra: DeepInfra models as a provider, plus spend caps — the contract

**Status:** contract, written by the orchestrator on 2026-10-01 at the
user's request: "je voudrais integrer les modeles 'deep infra' de opencode".
- **Ids:** `DI-R*`, never renumbered. A behaviour is retired by marking it
  withdrawn.
- **Two-slash ids:** checked by the advisor on 2026-10-01: the catalog
  splits with `split("/", 1)` and usage keys keep the full id; no
  model-derived path was found. DI-R2's tests stay as the guard.
- **Precedent:** `context/specs/zai-provider.md` (ZA). Where this contract
  says "as ZA-Rn", the ZA behaviour applies with the names below swapped in.

## The user's decisions (2026-10-01)

- **The cap.** A spend cap built into multiagents, per provider (here
  DeepInfra), with per-model overrides. It is **off by default**; the user
  turns it on in config.
- **Egress.** `api.deepinfra.com`, and only that host, was added to this
  project's `egress_allowlist` by the orchestrator.
- **Roster.** "Juste disponible": the provider is integrated and tracked.
  No agent in `agents.yaml` uses it, as primary or as fallback.

## Facts established live on 2026-10-01

- `opencode auth list` shows a "Deep Infra" credential. It lives in
  opencode's `auth.json`. **The entry name is unverified**; `deepinfra` is
  the assumption. The tester must confirm it from opencode's source or docs
  (never by reading the user's store) before writing DI-R3's tests, and say
  so in its result; if it differs, the confirmed name replaces `deepinfra`
  in DI-R3.
- `opencode models` lists 50 `deepinfra/<org>/<model>` ids, for example
  `deepinfra/openai/gpt-oss-20b` and `deepinfra/Qwen/Qwen3.8-Max`. These ids
  contain **two slashes**.
- `opencode run -m deepinfra/openai/gpt-oss-20b --format json` emits the
  same NDJSON as the other opencode models. Its `step_finish` carries
  `tokens` and a **non-zero `cost`** in USD (0.00029121 for 9575 tokens).
  Unlike Go and Z.AI, that cost is money actually billed.

## Behaviours

**DI-R1: a shipped provider instance.**
- The shipped `providers.yaml` gains:

  ```yaml
  opencode-deepinfra:
    extends: opencode
    family: opencode-deepinfra
    enabled: false
    billing: metered
    models_include: ["deepinfra/*"]
    env:
      MULTIAGENTS_OPENCODE_PLAN: deepinfra
  ```

  It ships disabled, with a comment saying how to enable it, and that it
  bills real money.
- **Its own family.** Failover between it and `opencode` or `opencode-zai`
  is never implicit.
- **`opencode` and `opencode-zai` are unchanged.** `deepinfra/*` is never
  attributed to either of them.
- This project enables it in `.multiagents/config/providers.yaml`. That is
  the orchestrator's change.
- Verified by:
  - `load_providers` on the shipped file yields `opencode-deepinfra` with
    `family == "opencode-deepinfra"`, `billing == "metered"`, the opencode
    spawn and stream rules inherited, and `enabled == False`;
  - `refresh-models`, with a fake `opencode models` listing `opencode-go/*`,
    `zai-coding-plan/*` and `deepinfra/*` ids, records `deepinfra/*` only
    under `opencode-deepinfra`, including two-slash ids intact.

**DI-R2: model ids with two slashes work end to end.**
- An agent whose model is `deepinfra/Qwen/Qwen3.8-Max` is spawned with
  exactly that id as opencode's `-m` argument.
- Wherever a model id is split into provider and model, or used in a path,
  a file name, a cache key or a log label, the extra slash neither truncates
  the id nor creates a directory.
- Verified by: spawn-argument and usage-recording tests with a two-slash
  id; the recorded model in `usage_by_model` equals the full id.

**DI-R3: `check` and `login`, as ZA-R2.**
- With `MULTIAGENTS_OPENCODE_PLAN=deepinfra`, `check` exits 0 when the auth
  store holds an entry `deepinfra` with a non-empty `key`. Otherwise it
  exits 10, naming the missing entry and the fix:
  `multiagents auth login opencode-deepinfra`, then choose Deep Infra.
- A missing or unparsable store gives exit 10 with its own message. The key
  is never printed.
- `login` prints one line saying to choose Deep Infra, then runs
  `$MULTIAGENTS_BIN providers login`.
- With the variable unset or set to `zai-coding-plan`, the script behaves
  exactly as today.
- Verified by: the ZA-R2 test matrix with `deepinfra` swapped in, plus a
  store holding only `zai-coding-plan` giving exit 10 for deepinfra.

**DI-R4: `budget` for a metered provider.**
- DeepInfra has no quota windows. `budget` reports no window, and headroom
  is **unknown**, never zero and never "exhausted" because of a missing
  window. Missing windows alone never refuse admission (cooldown, auth and
  model checks still apply, as for any provider).
- `opencode.sh budget` branches on `MULTIAGENTS_OPENCODE_PLAN=deepinfra`
  **before** the Go network probe, and makes no network call at all.
- `budget_status` shows `opencode-deepinfra`'s run spend in `by_model`, as
  for every provider, with the full two-slash model id. Its cost is real
  billed USD: the row is **not** marked `"billing": "plan"`. Per-period
  spend is SC's business, not this contract's.
- Verified by: `budget` under a fake store, with a PATH `curl` that fails
  the test if invoked, gives exit 0 with no windows;
  the router admits an enabled `opencode-deepinfra` with no windows;
  a run's reported cost appears under its full model id in `by_model`,
  without a plan marker.

**DI-R5: spend caps — moved to their own contract.**
- The cap is a separate feature with its own ids: see
  `context/specs/spend-caps.md` (`SC-R*`). DI-R1 to DI-R4 and DI-R6 do not
  depend on it and are built first.

**DI-R6: no regression.**
- With no cap configured, Go, Z.AI, claude, codex and agy behave exactly
  as before.
- Verified by: the full suite; the only reds are the 72 known phase-2 ones.

## Out of scope

- DeepInfra's own billing API, or reconciling with its invoice.
- Putting a DeepInfra model in the roster: the user's call, later.
- Caps on token counts rather than USD.
