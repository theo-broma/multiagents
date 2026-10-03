# C22 — configurable choice among a family's accounts: the contract

**Status:** contract, orchestrator, 2026-10-04.
- **Requested by:** the user.
- **Ids:** `IS-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer.

## Today

When an agent's preferred provider has same-family siblings (agy/agy-b, codex/codex-b, claude/claude-b), `budget.pick_instance()` (`budget.py` ~1750) chooses among the instances that still have room. The order is:
1. instances not held for the orchestrator;
2. known readings before unknown ones;
3. the fewest running agents;
4. the longest since last use;
5. the name.

Headroom is a filter there, never a ranking, on purpose: an advisor objected that ranking by a stale shared percentage pins every concurrent spawn to one instance. `choose_provider` (~1786-1860) explains the choice in its reason string, fixed in 6428dbd.

## What the user wants

Several selection rules, configurable globally and per provider. The default is "the one whose nearest window ends soonest".

## Behaviours

**IS-R1: named strategies.** A strategy decides which eligible instance wins:

| Strategy | The winner is the instance… |
|---|---|
| `soonest_reset` (**default**) | whose earliest upcoming window reset comes first, across its counted windows that have a known reset time. Its quota is about to be refilled anyway, so it is spent first. |
| `shortest_window_least_remaining` | with the least remaining quota in its shortest window, e.g. 5h or session |
| `shortest_window_most_remaining` | with the most remaining quota in its shortest window |
| `longest_window_least_remaining` | with the least remaining quota in its longest window, e.g. weekly or monthly |
| `longest_window_most_remaining` | with the most remaining quota in its longest window |
| `least_loaded` | chosen as today: load, then last use. Kept for anyone who wants the current behaviour. |

- **Window length.** "Shortest" and "longest" are judged by each window's span. When a reading does not carry the span, it is inferred from the window name: 5h/session < daily < weekly < monthly. "Remaining" is `100 − percent used`.
- **Missing data.** An instance whose reading lacks the data a strategy needs (no reset time, no matching window) ranks after the instances that have it.
- **Tie-breaks.** Ties, and instances that all lack the data, fall back to today's order: load, last use, name.
- **Unchanged:**
  - eligibility: room left, the reserve, the orchestrator-held instances last, known readings before unknown;
  - who is in the pool, i.e. the family and the `models:` keys (FS-R1).

  A strategy only ranks the instances that are already eligible.
- **Verified by:** a fixture pool of two or three instances whose readings differ per window gives the expected winner under each strategy, and the tie and missing-data cases fall back as described.

**IS-R2: configuration.**
- **Global:** a key in the project or global config, `budget.instance_strategy`, which follows the usual project → global → shipped layering.
- **Per provider:** `instance_strategy` on a provider in `providers.yaml`, inherited through `extends` like other keys.
- **Which applies:** the preferred provider's own `instance_strategy` if set, else `budget.instance_strategy`, else `soonest_reset`.
- **Bad values.** An unknown strategy name is a config error, with the file and line, and `doctor` reports it. A bad value never silently falls back.
- **Documentation:** in `defaults/project.yaml` and `defaults/providers.yaml`.
- **Verified by:** precedence tests (provider over global over default), `extends` inheritance, and a doctor problem line for an unknown name.

**IS-R3: the reason names the strategy.**
- When a sibling wins over the preferred provider, `choose_provider`'s reason says which strategy decided and the deciding fact. Examples:
  - `"soonest_reset: agy-b's gemini-5h resets at 21:50, before agy's"`;
  - `"longest_window_most_remaining: agy-b has 84% of gemini-weekly left vs 9%"`.
- "constrained" and "held for the orchestrator" keep their meaning from 6428dbd.
- **Verified by:** the reason for each strategy names the strategy and the deciding window or fact.

**IS-R4: no regression.**
- Routing outside families, the reserve, the defer and wait-for-reset logic, and the existing routing tests behave as before.
- Tests that pin today's load ordering for family pools are updated deliberately by the tester to `least_loaded`, or to the new default, as fits their intent.

## Out of scope

- Strategies across different families or vendors (the fallback chain).
- Mixing several strategies in one rule.
