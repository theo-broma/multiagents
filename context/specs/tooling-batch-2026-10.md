# Tooling batch, 2026-10: defects in multiagents itself

Source: triage by researcher ag-f46c94 (16 items; 7 still present), checked
against the code by the advisor (ag-aed397). Items reported fixed (steer
finalization, dev-advisor delegate, no_commits, agy ERROR after verdict,
half-open claim, wall-clock watchdog, mid-line marker) are out of scope.
Dropped: agy backgrounding long commands (provider-side, no control in our
engine); repeated doctor warning (not locatable).

All rules are **prospective**: historical completed, failed or held records
are left as written. New rules apply at the next completion, relaunch or
resume boundary.

Packages and order:
- **A, scheduler semantics** (TB-R1..R3): `scheduler/`, `server.py` wait and
  relaunch wrappers.
- **C, routing and commits** (TB-R5..R7): `config.py`, `runner.py` (auto-commit
  path, conversation resolution).
- **B, rate-limit resume** (TB-R4): `runner.py` result classification and
  `scheduler/` resume. Starts after A merges, because both touch the
  scheduler's completion handling.

A and C run in parallel.

## Package A

**TB-R1. A node run that ends asking for information holds its node.**
- Trigger: the run's final result text contains a logical line that begins
  with `NEED_INFO(` (anchored at line start, after optional whitespace; not
  inside a fenced code block or backticks; same anchoring rules as the
  existing NEED_DECISION parser), and the run did not emit a verdict.
- A verdict, when present, takes precedence: the node is settled by the
  verdict as today.
- Effect: the node is `held` with `hold.reason = "needs_info"` and the
  marker text (all markers, in order) recorded durably on the node; a
  `needs_info` transition carrying that text is delivered through
  `wait_for_nodes`. `outcome` stays null while held.
- The hold is written before any dependency is evaluated: dependents with
  `require: success` never launch on such a run. Enclosing sequences and
  loops see the child as held, not completed.
- `merge_node` refuses a held node as it does today (`not_done`, force or
  not).
- Recovery: `relaunch_node` (TB-R2) or `close_node`.
- `NEED_INFO` mid-run keeps its current non-blocking meaning; only the
  final result is inspected.
- Agent-facing docs that describe NEED_INFO say that a NEED_INFO as the run's
  last word holds a scheduled node.
- Verified by: tests on a scheduled node whose fake run ends with NEED_INFO,
  with and without a verdict, with a dependent node, and inside a sequence.

**TB-R2. `relaunch_node` works on a simple node.**
- Called on a simple node with no round controls, it launches a fresh run
  (fresh session) as a new generation, on the same branch, with the same
  task. The previous run directory is kept.
- The MCP wrapper no longer sends a loop-only `retry` default; passing
  `max_rounds` or `retry` for a simple node is still refused, with an error
  naming the parameter.
- Applies to simple nodes that are held (including TB-R1's `needs_info`) or
  done with a non-approved outcome; relaunching a running node is refused.
- Verified by: tests relaunching a held simple node and a failed simple node,
  and the refusal with round controls.

**TB-R3. `wait_for_agents()` without ids sees nodes not yet launched.**
- With no ids, the wait covers the caller's runs (as today) plus a snapshot,
  taken at call time, of the caller-created nodes that are open and have no
  run yet. Nodes created later do not join.
- It returns when any covered item finishes, parks (`awaiting_user`, held),
  or is cancelled. A launch alone does not return it.
- On timeout it returns as today and also lists the snapshot nodes still
  pending, by node id, with their `blocked` reason.
- Verified by: tests with an open node that launches and finishes during the
  wait, one cancelled during the wait, and a timeout.

## Package C

**TB-R5. No automatic commit for agents that do not write.**
- When a run's launch spec has `writes=False`, the end-of-run automatic WIP
  commit and the commit-fix turn are both skipped. Commits the agent made
  itself are kept.
- Writing agents keep today's behaviour.
- A non-writing run's worktree with no commits is dropped as today; its
  scratch files go with it.
- Applies to every `writes=False` run, conversational agents (advisor,
  dev-advisor) and steered runs included.
- Verified by: tests that a non-writing run leaving an untracked file
  produces no commit on its branch, and that a writing run still does.

**TB-R6 — withdrawn: already holds.** Tester ag-f2aace found every reachable
fallback path (automatic chain, explicit fallback model, steer) already
applies the entry's options, through FO-R1's `routed()`; there is no run-level
option pin to give precedence to. Its tests in
tests/test_tb_c_fallback_options.py stay as regression guards. Original text:

**TB-R6 (withdrawn). Provider options in a fallback entry apply on every fallback path.**
- Options declared for a provider in a fallback map (such as `variant`) are
  applied whenever that fallback is used: automatic fallback selection and an
  explicitly chosen fallback model alike.
- An explicit pin on the run takes precedence over the fallback entry's
  option. An empty value in the entry clears the option.
- Verified by: tests that render the launch command for each path and check
  the option.

**TB-R7. A standing conversation follows a roster model change.**
- A conversation (advisor, dev-advisor and the like) records the effective
  launch fingerprint: provider, model, effort and provider options such as
  variant.
- On the next consult, if the current roster resolves to a different
  fingerprint, a fresh session is started instead of resuming, and a
  `conversation_replaced` event records the old and new values. Same
  fingerprint: resumed as today.
- The fingerprint compared is that of the route the consult would launch on
  now (the agent's own provider or a `models:` fallback route), against the
  one recorded for the standing session.
- The event carries `old_fingerprint` and `new_fingerprint`, each an object
  with `provider`, `model`, `effort` and `options`, plus the replaced run id.
- The comparison happens under the existing conversation lock, before any
  launch side effect.
- An explicit `steer_agent` on a run keeps today's semantics: it resumes
  with the recorded model.
- A conversation recorded before this change (no fingerprint) is compared on
  provider and model only.
- Verified by: tests with the same provider and a changed model, a changed
  variant, an unchanged roster, and a legacy record.

## Package B (after A)

**TB-R4. A provider rate limit defers a node run; it does not fail it.**
- A run that ends on a provider rate-limit signal (an HTTP 429 from the
  provider, or the CLI's own "rate limited … will be retried" message) is
  recorded with cause `rate_limited`, not as a failure.
- It is not counted against the provider's failure breaker, does not settle a
  reviewer verdict, and does not advance a loop round.
- For a scheduled node, the scheduler resumes the same session (as the window
  suspension's resume does) once admission allows: after the provider's
  Retry-After when given, otherwise after a default cooldown, with bounded
  backoff on repeats. Locks and the provider slot are released meanwhile.
- If the session cannot be resumed, the node is relaunched fresh and told to
  read the old run directory.
- Survives a host restart: a pending resume is journaled.
- Cancelling the node cancels the pending resume.
- Usage from the interrupted run is still counted.
- Verified by: tests with a fake provider returning a 429 mid-run, with and
  without Retry-After, a repeated 429, a cancel while pending, and a restart
  while pending.
