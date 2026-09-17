# Phase 1 contract — the quota window nobody can see

The interface contract for `BRIEF.md` phase 1 item 7, ticket `bug-e1cb10`.
Written by the orchestrator.

**Read the ticket with `list_tickets`.** It carries the root cause with line
numbers and names the missing test.

This is the last item of phase 1, and it is the one that cost this session most.
Every routing decision made today was made against a number that does not
describe the bucket that actually stops work.

---

## R11 — `budget_status` shows every window a provider reports

**Observable behaviour required.** When a provider reports several quota windows,
all of them appear under `windows`, and `headroom` and `severity` derive from the
worst.

That is not an aspiration. It is what `budget_status`'s own documentation already
promises:

> *"Where a provider reports several windows they are listed under `windows`, and
> `headroom` is the worst of them, because the fullest bucket is what will
> actually stop a run — but which one it is changes what to do: a rolling or
> 5-hour window clears in hours, a weekly or monthly one does not."*

`opencode` and `agy` keep that promise — their scripts return a `windows` dict
which `_from_script()` passes through. **`claude` does not.** `read_claude()`
computes `worst_percent` across the `limits` array (or the `five_hour` /
`seven_day` fallbacks), sets `headroom` and `resets_at` from the worst, and never
populates `budget.windows`. `Budget.to_dict()` only emits the key when it is
truthy, so it is silently absent.

**What must be true afterwards.**

- A claude profile reporting more than one window produces a `windows` dict
  containing all of them, each with its percent and reset time.
- `headroom` and `severity` derive from the worst bucket, as today. That part of
  `read_claude()` is correct and must not change behaviour.
- A provider reporting a single window is unaffected.

**The test the ticket names, and it does not exist:** a claude profile whose
short window is at 95% and long window at 30% returns `severity: "critical"` and
includes the short window in `budget.windows`. Existing coverage
(`test_read_claude_picks_the_worst_of_multiple_limit_buckets`) asserts `headroom`
and `resets_at` from the worst bucket and never looks at `windows` — which is
exactly why two tests that appear to cover this do not.

---

## Why this one is not cosmetic

`budget_status` exists to route. Its own docstring says so: *"Use this to route:
when your own five-hour bucket is tight, delegating to an unrationed provider is
the highest-value thing you can do."* So routing on it is the correct behaviour,
and routing on it is what fails.

Observed twice in one session, hours apart. `budget_status` reported
`severity: normal`, `headroom: 0.4`, reset in four days, and
`advice: ["all providers have headroom"]`. At that same moment the quota guard
was interrupting agents with *"claude is about to run out of quota — roughly 1
minute(s) of it left"*. Two components reading the same provider, reporting
thirty percent headroom and one minute.

The failure is in the permissive direction and it is confidently worded. An
orchestrator that believes it commits an expensive long-running agent and is cut
off mid-run — which happened, repeatedly, and cost several runs their work.

---

## What is NOT in scope

- **Do not change how `headroom`, `severity` or `resets_at` are computed.** That
  logic is correct; only the intermediate data is missing.
- **Do not change the `opencode` or `agy` scripts**, which already do this right
  and are the shape to match.
- **Do not reconcile `budget_status` with the quota guard's own sampling.** The
  ticket raises it as a possibility and it is a larger question: the guard
  extrapolates from a headroom time-series via `tree.burn`, which is a different
  mechanism, not a different reading of the same number. Once `windows` is
  populated the two should agree about the facts; whether they should share a
  code path is a separate decision.

---

## What "done" looks like

The suite is now **green**: 966 passed, 0 failed, 3 skipped in a container, and
968 passed with 1 failed on a host — the one host failure being
`test_the_claude_script_uses_the_container_profile_only_where_it_should`, which
is finding F130, scheduled in phase 3, and manifests only where `docker` is
present.

That green is new, as of this phase. Do not be the change that breaks it.

Run the suite as `uv run --frozen python -m pytest`.

R11 is done when its test is green and that baseline is otherwise unchanged.

---

# Amendment — after the R11 test landed

`tests/test_c2_budget_characterization.py::test_r11_read_claude_reports_every_window_not_only_the_worst`
is written, merged and verified red on `assert len(b.windows) == 2` → `0 == 2`.
The four assertions above it — `known`, `severity == "critical"`,
`headroom == approx(0.05)`, and `resets_at` taken from the session bucket — pass
today, which is what proves the test is wired to real behaviour rather than
failing for an incidental reason. **An implementer can pick this up cold.**

## Answer to `NEED_INFO(ticket)`

The test engineer asked whether the ticket constrains the naming of the keys in
`windows`. **It does not, and neither do I.** The key name is the implementer's
choice. What carries the meaning is the reset time attached to each entry —
which is exactly what the test asserts on, and it was right not to pin the names.

## A gap this contract has, named by the test engineer rather than by me

R11's third bullet says a provider reporting a single window is unaffected.
**That is not covered by the test**, and it is not the test engineer's fault: the
task was one requirement and one test, correctly scoped.

So nothing currently stops an implementer from populating `windows` for a
single-bucket profile as well, which this contract forbids in prose and does not
forbid in code. Whoever implements R11 should either add that case or say why it
does not matter — and whoever reviews the fix should check for it, because the
suite will not.

## A mistake in how these tasks were written, worth not repeating

Every task in this phase told its agent to *"read the ticket with
`list_tickets`"*. **Agents cannot do that.** `list_tickets` is an MCP tool of the
running server, available to the orchestrator and not to a subagent, and the
tickets are not on disk in a worktree either.

Nothing was lost, because each task restated the ticket's content — which is why
it took until the last run of the phase for anyone to notice. But every agent
that tried it wasted steps discovering the instruction was false, and the one
that said so was the first to be explicit rather than to quietly work around it.

For any later phase: put the ticket's substance in the task, or point at a file
in the tree. Do not send an agent at a tool it does not have.
