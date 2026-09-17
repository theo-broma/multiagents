# Phase 1 contract — budget accounting

The interface contract for `BRIEF.md` phase 1 item 2, ticket `bug-565863`.
Written by the orchestrator.

This is the item the brief singles out: **"This is why the review covered 2 of 7
contexts."** Every budget ceiling set during the review was either never reached
or blown open on the first run, and neither outcome had anything to do with how
much work the slice contained.

Read the ticket with `list_tickets`. It carries the root cause with line numbers
and names the missing test.

---

## The defect, stated as behaviour

A `budget_tag` is a termination mechanism. `start_agent`'s own documentation says
the ceiling is fixed on first spawn "because a ceiling the spender can raise is a
suggestion and the agent asking to raise it is the one that has just run out",
and that `start_agent` refuses once a tag is spent.

Providers do not agree on where they put a run's token total. `opencode` sends
`total`, `agy` sends `total_tokens`, and `claude` sends neither — only
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens` and `cost_usd`. The codebase already knows this:
`token_count()` exists precisely to normalise it, and its own comment records the
measurement that prompted it — *"nine million tokens dropped, and every claude row
in `usage` showing 0"*.

`token_count()` was wired into the per-model rollup and the monitor snapshot. It
was **not** wired into the budget-tag path, which still sums raw keys and then
reads `.get("total", 0)`.

---

## R4 — a tag counts spend from a provider that reports no `total`

**Observable behaviour required.** A run whose usage dict carries no `total` and
no `total_tokens` key still moves its tag's spend.

Today it does not. Observed during the review: a `claude/sonnet` run reporting
5,145,978 cache-read tokens, 55,203 output tokens and $2.80 left
`budget_tag_status` reading `tokens_spent: 0` — a figure indistinguishable from a
tag nothing has ever run against.

**What must be true afterwards.** Recording a claude-shaped usage dict against a
tag increases that tag's reported spend by a figure derived from the tokens the
run actually reported.

---

## R5 — a tag sums across providers that disagree about key names

**Observable behaviour required.** A tag carrying runs from two providers that
name their totals differently reports the sum of both, not one of them.

Today it does not, and the failure is worse than undercounting. Observed during
the review on the `ctx-provider` tag: the figure read 1,670,589 while agy runs
dominated, then **4,740,503** after an opencode run finished — and 4,740,503 was
exactly that one run's total, not the sum. The later number replaced the earlier
one rather than adding to it, because the raw per-key sum produces a dict whose
`total` key reflects only the providers that happen to use that name.

**What must be true afterwards.** Spend is monotonic in the runs recorded against
a tag: adding a run never decreases it, and a tag carrying runs from mixed
providers reports a figure that accounts for all of them.

---

## R6 — enforcement and reporting agree

**Observable behaviour required.** The figure `start_agent` refuses on is the
same figure `budget_tag_status` reports.

The ticket names four call sites reading `.get("total", 0)`: two in the
enforcement path and two in the reporting tool. They must not be able to
disagree. A caller that sees a tag with headroom and is then refused, or sees a
tag exhausted and is not, has no way to plan.

**What must be true afterwards.** With a ceiling set and claude-shaped runs
recorded past it, `start_agent` refuses, and `budget_tag_status` reports the tag
as exhausted. Both before and after the ceiling is crossed, the two agree.

---

## What is NOT in scope

**Do not change what a tag is denominated in.** The ticket suggests, reasonably,
that `budget_tag_status` might also surface cost, since cost is what claude
actually reports and what the user pays. That is a design change with its own
contract and it is not this. Tokens stay the denomination here.

**Do not change any ceiling.** The ceilings set during the review were chosen
against a broken counter and several are now meaningless. Re-tuning them is a
judgement call for whoever sets the next ones, not part of making the counter
honest.

**Do not touch `token_count()` itself.** It is correct, it is unit-tested at
`tests/test_core.py:9313-9321`, and it is the thing the four sites should be
using. If you believe it is wrong, stop and say so rather than changing it.

---

## The test the ticket names, and why the existing ones missed this

`tests/test_core.py:10493` (`test_a_budget_tag_sums_spend_across_every_run_that_carries_it`)
and the ceiling-refusal test at `:10508` both record usage as `{"total": used}`.
That is a synthetic shape no provider actually sends, and it is why two tests
that look like they cover exactly this defect do not.

**The test that should exist records a claude-shaped usage dict** — input,
output, and cache keys, with no `total` — and asserts the tag's spend moves and
the ceiling is enforced. A test built on `{"total": ...}` will pass against the
broken code and prove nothing, so it is worth checking that a new test fails
before the fix for the right reason.

---

## What "done" looks like

The suite's baseline on this branch is **1 failure**, and it is `F100`
(`tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated`),
scheduled as phase 1 item 8. On a host where `docker` is present, a second test
fails for environmental reasons that are recorded in `docs/open-questions.md`;
inside an agent, 17 runner tests fail with `PermissionError: can_spawn is false`
for reasons also recorded there. None of the three is a regression.

Run the suite as `uv run --frozen python -m pytest`.

R4, R5 and R6 are done when their tests are green and that baseline is otherwise
unchanged.
