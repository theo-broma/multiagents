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

## Revision after the advisor's check (2026-10-04, ag-aed397, before tests)

These override any earlier wording they contradict. The decisions are the orchestrator's.

**IS-R1a: a tolerance around the best score, against herding.**
- **The problem.** A strict ranking on a reading shared by every spawn sends each successive launch to the same winner (the `pick_instance` docstring, ~1755). `soonest_reset` concentrates work by design, and the user wants that.
- **The rule.** Instances whose score is within a tolerance of the **best** score form the winning group. Compare each one against the best, never pairwise. Inside that group, the order is today's: load, which counts recent claims plus starting and running agents (`runner.py` ~1190-1207), then last use, then name.
- **Default tolerances:**

  | Strategies | Tolerance |
  |---|---|
  | `soonest_reset` | 15 minutes |
  | remaining-quota strategies | 5 percentage points |

- **Configuration:** `budget.instance_tolerance_minutes` and `budget.instance_tolerance_points`, layered like `instance_strategy`.
- **Out of scope:** a cap that spreads load across larger gaps.

**IS-R1b: window semantics.**
- **Which windows count.** A strategy uses only counted windows (`counted: false` is excluded) that have a finite percent between 0 and 100 inclusive.
- **Reset times.** For `soonest_reset`, a reset counts only if it is timezone-aware and in the future.
- **Stale readings.** A stale reading contributes no score, even when every candidate is stale. Those instances rank as "missing data".
- **No windows.** When `windows` is empty, the remaining-quota strategies use the top-level `used_percent` as a single window of unknown span. That window serves only as both the "shortest" and the "longest" when no instance has spans.
- **Equal spans.** When several windows share the shortest (or longest) span, the instance is represented by its most-used window at that span.
- **"Remaining"** compares percentages, not absolute capacity.

**IS-R1c: window spans.**
- **Codex.** It already receives `window_minutes` but drops it when it builds its output windows (`codex.py` ~545-554, ~712). It must keep that value, e.g. as `span_minutes`, on each window.
- **Name inference.** Where a reading carries no span, the span is inferred from the window name through an explicit table:

  | Window names | Span |
  |---|---|
  | `5h`, `five_hour`, `gemini-5h`, `3p-5h`, `codex-5h`, `session`, `*/session` | 5 h |
  | `daily`, `day` | 1 day |
  | `weekly`, `seven_day`, `weekly_all`, `*/weekly_all`, `gemini-weekly`, `3p-weekly`, `codex-weekly` | 7 days |
  | `monthly` | about 30 days |

- **The prefix.** A vault account prefix such as `default/` or `b/` is stripped before matching.
- **Unknown names.** `rolling` and any unknown name have an unknown span: they are never guessed, and never derived from the time left until the reset.
- **Excluded.** Windows of unknown span are left out of the shortest and longest choice.

**IS-R2a: precedence.**
- One strategy is resolved for the whole pool: the preferred provider's effective setting. An inherited setting counts.
- Explicit `null` on a provider clears an inherited value, so the global setting applies.
- A sibling's own setting applies only when that sibling is itself the preferred provider.

**IS-R3a: the reason is honest.** It names what actually decided:
- the strategy metric, but only when it separated the winner from the preferred provider beyond the tolerance;
- otherwise whichever of these decided instead: reservation, an unknown reading, missing data, the tolerance group followed by load or last use, or the name.

It never states a reset or quota comparison that did not decide.

**IS-R5: scope note.** C22 chooses among provider **instances**, such as claude versus claude-b. It does not choose the account inside the claude sidecar vault: that vault picks its own budget representative (`budget.py` ~1047-1075), and nothing there changes.

## Decisions after the tester's read (2026-10-04, ag-830f8c)

- **Interface.** `pick_instance` and `choose_provider` take the keyword arguments `strategy=`, `tolerance_minutes=` and `tolerance_points=`. An unknown strategy passed directly raises `ValueError`.
- **Config errors.** An unknown strategy in config raises a `ValueError` naming the value, the file and its line. `doctor` reports it as one problem, with exit code 1.
- **Tolerance.** The boundary is inclusive: a score within 15 minutes, or within 5 points, of the best is tied.
- **Window names** are matched case-insensitively.
- **Windowless readings under `soonest_reset`.** A reading with no windows contributes no reset, so it counts as missing data. A top-level `resets_at` is not used.
