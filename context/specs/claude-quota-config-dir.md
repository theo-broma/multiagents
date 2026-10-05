# The built-in claude quota reader honours the caller's config_dir (BQ)

Finding: **F120** (context/review/C2-budget.md), critical. Re-verified still
present on 2026-10-05 by researcher ag-8dc228: the direct builtin-owner path
calls the reader without `config_dir` (`src/multiagents/budget.py:1077-1079`);
inherited named accounts have a separate path (`budget.py:1103-1105`) that is
not the defect. Plan: `context/plans/2026-10-05-scheduler-first-real-use.md` §2,
group *budget* (F122, F150, F154 were found already fixed).

## Behaviours

**BQ-R1.** When a provider's budget script declines (exit 64) and the built-in
claude reader is used, the reader reads the Claude profile directory that
*provider* is configured to use — resolved by the same rule the inherited-account
path already uses (`resolved_profile(provider)`, `budget.py:1103`) — not the
default `~/.claude`. Note: the `config_dir` argument of `read_provider` /
`read_all` is the multiagents config directory used to locate scripts; it is
NOT a Claude profile and must not be passed to the reader as one. Script lookup
is unaffected.
Verified by: `test_claude_builtin_fallback_ignores_the_callers_config_dir`
inverted (renamed to the new behaviour); a test with two providers configured
on two synthetic profile directories holding different quota data, asserting
each read returns its own.

**BQ-R2.** A provider with no configured profile gets exactly today's behaviour
(the default profile). `read_provider` / `read_all` signatures stay compatible
with their existing callers (`runner.py:1138`, `cli.py:1756`).
Verified by: a test, and the budget suites green.

**BQ-R3.** The inherited-named-account path (`extends`) keeps its current
behaviour.
Verified by: the existing `test_c2_budget_characterization.py` tests on
`config_dir` and `extends` stay green.

## Constraints
- Tests use synthetic profile directories under tmp_path; never read the real
  `~/.claude*` files.
