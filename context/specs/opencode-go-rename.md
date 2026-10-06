# The opencode Go provider is named `opencode-go` (OG)

Source: user, 2026-10-06: "il faudrait renommer opencode en opencode-go dans les
provider sinon on a l'impression qu'il s'agit de opencode en général".

Today the shipped `opencode` block does two jobs:
- it defines how to drive the opencode CLI (binary, spawn arguments, stream,
  MCP, home links), which `opencode-zen`, `opencode-zai` and
  `opencode-deepinfra` all inherit through `extends: opencode`;
- it is also the route for the Go subscription (`models_include:
  ["opencode-go/*"]`, the go usage endpoint).

The name reads as "opencode in general".

## Behaviours

**OG-R1.** The shipped providers have the following shape:
- `opencode` is the CLI base only. It is not a route: no agent may name it as
  `provider:` or in a `models:` chain, routing never selects it, and it has no
  `models_include`, budget or family of its own.
- `opencode-go` is the Go subscription. It has `extends: opencode`, `family:
  opencode-go` and `models_include: ["opencode-go/*"]`, and it ships enabled.
- `opencode-zen`, `opencode-zai` and `opencode-deepinfra` keep extending
  `opencode`, and nothing else changes for them.

Verified by:
- a config test of the resolved shipped set and families;
- a test that naming `opencode` as a route is refused (see OG-R2 for the
  alias).

**OG-R2. Old configs keep working, loudly.** A project or global config that
names `provider: opencode`, or `opencode:` in a `models:` chain, is read as
`opencode-go`. Each load prints one deprecation warning that names the file and
line and says to write `opencode-go`. The same holds for:
- a project `providers.yaml` override block named `opencode` that sets
  route-level keys (`enabled`, `models_include`, `env`, budget keys). Those
  keys apply to `opencode-go`, with the same warning. A key that is CLI-level
  (`bin`, `spawn`, …) still applies to the base;
- `multiagents auth login|status opencode`, which acts on `opencode-go`;
- MCP tool arguments (`pins.provider`, `refresh-quota`, `list_models`).

Verified by: one test per surface, asserting the warning text and the
effective `opencode-go`.

**OG-R3. State migrates once.** Durable state keyed by provider `opencode` is
moved to `opencode-go` the first time the new code loads it. That covers:
- breaker counts, cooldowns, quota and headroom records, and spend history in
  tree.json;
- scheduler pins;
- deferred tasks;
- any provider-keyed usage file.

The move is atomic and idempotent, and nothing is lost or double-counted.
Records that already exist under `opencode-go` are merged, not overwritten.
Verified by: a test with a pre-rename tree.json and scheduler DB fixture, loaded
twice.

**OG-R4. Names shown to the user say `opencode-go`.** This covers `doctor`,
`budget_status`, the monitor, `list_agents`, routing reasons and errors.
`opencode.sh` keeps its file name, since it serves the whole CLI.
Verified by: a test on `doctor` and `budget_status` output.

**OG-R5. Docs.** The README and provider docs describe `opencode` as the CLI base
and `opencode-go` as the Go subscription.
Verified by: review.

## Out of scope
- Renaming the opencode CLI's own auth-store entries (`opencode-go`,
  `opencode`), which belong to opencode.
- Removing the OG-R2 alias. That is a later, separate decision.
