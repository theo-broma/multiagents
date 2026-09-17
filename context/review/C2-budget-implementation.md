# C2 — defects found while repairing the budget accounting

Two same-class defects reported by the implementer that fixed `bug-565863`
(phase 1 item 2), deliberately left alone because they are outside that
ticket's named sites and unexercised by its requirements. Recorded here so the
next review does not rediscover them, and so the fix's own scope stays honest.

Both are the same idiom the ticket describes, at per-node read sites rather than
on the budget-tag path. The ticket named four readers and the fix made all four
honest by normalising at the point of summation instead; these two read per-node
usage directly and were never reached by that change.

**F160** — `budget_status`'s per-provider spend table cannot see claude spend
*Class:* correctness
*Severity:* medium
*Where:* `src/multiagents/server.py:964`
*Evidence:* inspection
*What happens:* The per-provider spend table reads `usage.get("total") or usage.get("total_tokens") or 0` for each node. A claude run sends neither key — only `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `cost_usd` — so every claude node contributes zero. This is the same defect as `bug-565863` at a site that ticket did not name, and it survives the fix for it.
*Disposition:* fix
*Reasoning:* `budget_status` is the tool an orchestrator routes on. A spend table that reports the most expensive provider in the roster as having spent nothing is wrong in the direction that encourages more spending. The fix is the one already applied elsewhere: call `token_count()` rather than reaching for key names.

**F161** — `render()`'s per-node token label is blank for a claude run
*Class:* maintainability
*Severity:* low
*Where:* `src/multiagents/tree.py:991`
*Evidence:* inspection
*What happens:* The per-node `{tokens:,}tok` label in the rendered tree uses the same key-name idiom, so a claude node renders with no token count at all rather than with its real one.
*Disposition:* fix
*Reasoning:* Cosmetic next to F160, and worth fixing in the same change because it is the same line of reasoning and because the rendered tree is what a human actually reads when asking where the tokens went. During this review the rendered tree was consulted repeatedly and silently under-reported every claude agent in it.
