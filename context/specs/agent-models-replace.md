# MR — a declared `models:` replaces the fallbacks below it

Approved by the user on 2026-10-18, with an addition: an empty list cancels
every default fallback. Source: ticket bug-d491e8.

## The defect

`config.load()` merges the layers (shipped defaults, user, project) with
`deep_merge()` (`src/multiagents/config.py:77`). Mappings recurse. So when a
higher layer declares `agents.<name>.models`, its keys are added to the
shipped ones, and a shipped route the project left out stays live.

Example: the shipped `tester` had an `opencode-go` kimi route. A project
declared `tester.models` with only `codex` and `codex-b`, yet kimi was still
launched (runs ag-44dfc5, ag-b1015f).

Side effect today:
- `models: {}` merges nothing, so it does NOT clear the shipped fallbacks.
  This project's own config uses `{}` that way.
- `models: null` replaces, so it does clear them.

## Behaviours

**MR-R1 — the highest layer that declares `models` wins whole.**
- When a layer declares `agents.<name>.models`, the effective map for that
  agent is exactly that layer's map, in that layer's order.
- Routes from lower layers are not kept.
- Every other agent field keeps today's recursive merge.
- A layer that does not mention `models` leaves the lower layer's map
  untouched.
- Verified by:
  - shipped defaults plus a project `tester.models` holding only two routes
    give an `AgentSpec.models` with exactly those two, in the project's
    order;
  - with the primary provider exhausted, no shipped-only route is launched;
  - a project override of another field (for example `model`) without
    `models` keeps the shipped map.

**MR-R2 — an empty declaration cancels every fallback.**
- `models: []`, `models: {}` and `models: null` all mean "no fallback
  routes".
- The agent then runs on its primary provider only.
- Verified by: each of the three forms, declared over shipped defaults that
  have routes, gives an empty effective map, and no fallback is launched.

**MR-R3 — any other list is a config error.**
- A non-empty list under `models` is rejected at load, with a message naming
  the agent and saying that `models` is a mapping of provider to model, or
  empty.
- Verified by: a test with `models: [codex]`.

**MR-R4 — nothing else changes.**
- An empty-string model still disables that one route (RT-R1,
  `context/specs/h4-h14-routing-limits.md`).
- Validation of provider and model names is unchanged.
- Verified by:
  - the existing routing, fallback-scope and config tests stay green;
  - `tests/test_fs_fallback_scope.py` keeps passing unchanged.

## Migration

No data migration. The implementer reports, for this repository's
`.multiagents/config/agents.yaml`, every agent whose effective `models`
changes under MR-R1 and MR-R2, as before and after. Changes are expected for
the agents declaring `models: {}`, and that change is the intended one.

## Plan

1. tester;
2. implementer (default tier), after 4C merges, since both touch
   `config.py`;
3. reviewer;
4. full suite;
5. merge, then resolve bug-d491e8 as fixed.
