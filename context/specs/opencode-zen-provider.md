# opencode-zen is a provider of its own, distinct from opencode-go (OZ)

Source: user, 2026-10-06: "il faut faire la distinction entre opencode-go et
opencode-zen".

What happened:
- `opencode-go/space-bunny-free` returned "Unexpected server error" on every
  call.
- `opencode/space-bunny-free`, the same model served by zen, answered.
- The go failures tripped the single `opencode` breaker, and that breaker then
  refused zen as well.
- The orchestrator added a project-level `opencode-zen` provider as a stopgap
  (`.multiagents/config/providers.yaml`), modelled on `opencode-deepinfra`:
  `extends: opencode`, its own `family`, `models_include: ["opencode/*"]` and
  `MULTIAGENTS_OPENCODE_PLAN: zen`.
- Gap in the stopgap: `opencode.sh` has no zen plan, so the budget, check and
  usage actions fall through to opencode-go's
  (`https://opencode.ai/zen/go/v1/usage`).

## Behaviours

**OZ-R1.** The shipped `providers.yaml` defines `opencode-zen`:
- `extends: opencode`, `family: opencode-zen`;
- `models_include: ["opencode/*"]`;
- `MULTIAGENTS_OPENCODE_PLAN: zen`;
- disabled by default, like the other opencode instances.

The project override then reduces to `enabled: true`.
Verified by: a config test that the provider resolves, together with its
family and includes.

**OZ-R2.** Breaker, cooldowns, headroom and spend are tracked per family. A
failure on `opencode` (go) never makes `opencode-zen` refused or cooling down,
and the reverse holds too.
Verified by: a test that trips one breaker and admits a node pinned to the
other.

**OZ-R3.** `opencode.sh` with `MULTIAGENTS_OPENCODE_PLAN=zen`:
- `budget` never reads the opencode-go usage endpoint. If zen has no documented
  quota endpoint, it reports capacity as unknown, using the script contract's
  existing unknown/decline form.
- `check` reports whether an opencode credential usable for zen is stored,
  following AU's rule: unknown is never reported as logged in.
- `usage` does not report go's numbers as zen's.

Verified by: script tests with a fake `opencode` binary and a fake HTTP
endpoint, asserting that the go URL is never called under `zen`.

**OZ-R4.** The `opencode` (go) provider's model set is
`models_include: ["opencode-go/*"]`. A go provider therefore never launches a
zen model id, and zen never launches a go model id.
Verified by: a routing test for each direction.

## Out of scope
- Paid zen models and spend caps.
