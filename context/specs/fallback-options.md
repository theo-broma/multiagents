# Per-provider options in an agent's `models:` map — the contract

**Status:** contract, orchestrator, 2026-10-02.
- **Ids:** `FO-R*`, never renumbered.
- **Found by:** the advisor (ag-d20e1e) while checking the deepinfra roster.

## The defect

```yaml
models:
  opencode-zai: {model: zai-coding-plan/glm-5.3-flash, variant: max}
```

- `AgentSpec.fallback_for()` (`config.py` ~603) keeps only keys that are `AgentSpec` dataclass fields. `variant`, and any other provider placeholder option such as one consumed by a provider's `spawn.optional` block, lives in `AgentSpec.extra`, so it is silently dropped.
- The run then launches without `--variant`, and nothing says so.

## Behaviours

**FO-R1: placeholder options in a `models:` entry are applied.**
- When an agent runs on provider P, the options in `models.P`, including keys that are not dataclass fields (`variant`, and any key a provider's `spawn.optional` consumes), override the agent's top-level values for that run.
- This applies both when P is reached as a fallback and when P is the agent's own primary provider and `models.P` exists.
- An explicit empty string (`variant: ""`) clears the top-level value for that provider. The optional flag is then omitted.
- Verified by: the spawned command line contains `--variant max` for the entry above, and omits `--variant` for `variant: ""`.

**FO-R2: a bare-string entry keeps today's behaviour.**
- `models: {P: some-model}` changes only the model. The top-level options travel unchanged, as the `fallback_for` docstring promises.
- Verified by: a top-level `variant: high` and `models: {P: m}` give `--variant high` on P.

**FO-R3: unknown keys are not silently swallowed.**
- A key in a `models:` entry that is neither a dataclass field nor an option any configured provider consumes is reported once as a config warning, naming the agent, the provider and the key. It does not fail the load.
- Verified by: loading such a config yields the warning, and the agent is still usable.

**FO-R4: no regression.**
- Configs without `models:` dict entries behave exactly as today, and so do agents with dataclass-only overrides (`effort`, `permission`, …).
- Verified by: the existing config and fallback tests stay green.

## Revision after the advisor's check (2026-10-02, before tests)

**FO-R1 (precise).**
- **Primary provider.** When the agent runs on its primary provider P and `models.P` is a dict, the options in that dict are merged over the top-level values. This includes `model`, unless the run is pinned to an explicit model.
  - Today the primary returns before applying its entry (`runner.py` ~7127).
- **Precedence**, highest first:
  1. an explicit per-run pin (model);
  2. the `models.P` entry;
  3. the agent's top-level values.
- **Scope.** This applies to start, steer, consult and fallback alike.
- **No shared mutation.** The merge produces a per-run spec. It never mutates the shared `AgentSpec`.

**FO-R3 (precise).**
- **Which keys are valid.** A key in `models.P` is valid if it is:
  - a dataclass field;
  - structural (`model`, `id`);
  - an option the **destination provider P** consumes via its resolved `spawn.optional`. Shipped providers consume `variant`, `effort`, `max_budget_usd` and `autocompact`.
- **Anything else** is reported as a config warning naming the agent, the provider and the key.
- **"Once".** The warning is emitted once per distinct (agent, provider, key) per process, through the same channel as other config warnings.

## Decision during review (2026-10-02)

These override any earlier wording they contradict.

**FO-R3a: validity is decided by the destination provider alone.**
- The sentence "Shipped providers consume `variant`, `effort`, `max_budget_usd` and `autocompact`" describes the union of all shipped providers. It does not extend what any single provider accepts.
- A key in `models.P` is valid only when it is a dataclass field, is structural (`model`, `id`), or is consumed by **P's own** resolved `spawn.optional`.
- Examples:
  - `models: {opencode-zai: {max_budget_usd: 5}}` is reported, because opencode only consumes `variant`.
  - `models: {codex: {variant: max}}` is reported, because codex only consumes `effort`.
  - `models: {claude: {max_budget_usd: 5}}` is not reported.
- **Why.** FO-R3 exists so that an option dropped at launch is never silent. The union reading lets exactly those options through.
- **`provider` is not an override.** A `provider` key in an entry is ignored and reported. It never moves the run.
- **Surfacing.** The warnings are returned by `validate_agent_models()`, which is what `multiagents doctor` prints.
- **Dataclass fields are always valid.** A field such as `effort` is valid under any provider: FO-R3 reports unknown keys, not fields a provider ignores. Whether a provider honours `effort` is decided by the routing rules (RM-R5b), not by FO-R3. Noted by the tester on 2026-10-02.

## Decisions after the final review, round 1 (2026-10-02)

These override any earlier wording they contradict.

**FO-R3b: deduplicate at display, not at load.**
- Every `Config` carries all of its FO-R3 warnings. Loading never consumes a dedup token. A reloaded config, as the MCP server reloads on every tool call, therefore still carries its warnings.
- `validate_agent_models()` returns all of them, and so does `multiagents doctor`.
- "Once per (agent, provider, key) per process" applies only to emission to a log or stderr channel that would otherwise repeat on every reload. It never applies to what a validation call returns.

**FO-R1b: an `effort` in the primary entry is explicit.**
- `models.<own provider>: {effort: X}` is an explicitly configured effort, exactly like the same entry under a fallback provider.
- When it conflicts with the model's implied effort, it is refused (RM-R5a/RM-R5b). It is never silently normalised.
- A top-level `effort` keeps its current treatment.

**FO-R1c: the entry applies to whatever destination is chosen.**
- Whichever path selects the destination provider D (primary, fallback, or a family sibling while family routing still exists), `models.D` is applied when present. This includes an options-only entry with no `model`.
- `_spec_of()` rebuilds with the same rule.

**FO-R4a: a bare-string entry for the primary provider sets the model.**
- `models: {P: m}`, where P is the agent's own provider, runs on model m. This follows FO-R1 (precise) and FO-R2.
- FO-R4's "behave exactly as today" is narrowed to configs with no `models:` entry for the destination.
- No shipped or project agent has a bare entry for its own provider, so nothing changes in practice.
