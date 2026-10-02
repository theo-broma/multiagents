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
