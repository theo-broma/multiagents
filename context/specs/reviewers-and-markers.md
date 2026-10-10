# Reviewers and control markers (RM)

Defects seen in October 2026 while running reviewers outside a loop, and
while parsing agents' control lines (BRIEF.md: MK, reviewer tooling gaps):

- **MK:** the `NEED_INFO` marker is already Markdown-aware: prose, inline
  code, `>` quotes and fences do not trigger it. `NEED_DECISION` is scanned
  live on text events with no masking (runner.py, around 532, 5828, 6057), so
  a fenced example stops the agent and opens a question. `VERDICT` lines have
  no masking either (runner.py around 545, 6545).
- A standalone reviewer, one started with `start_agent` rather than inside a
  loop, gets a worktree based on main, not on the branch it verifies. Every
  review task has to tell it to `git checkout --detach <tip>`.
- `give_verdict` exists only for a designated node run inside a loop. A
  standalone reviewer's brief asks for it, cannot find it, and falls back to
  a text `VERDICT` line.
- agy reviewers end their turn while a test command is still running in the
  background, then report without counts. Reviewers have approved with no
  observed test counts.
- An implementer committed about 20,000 lines of validation logs
  (`.oc-validation/`) to its branch.

## Behaviours

**RM-R1 (MK).** One shared masking rule decides which text can carry a
control marker, and the `NEED_INFO`, `NEED_DECISION` and `VERDICT` parsers all
use it, including any associated `DEFAULT` line and every later scan of the
same text.
- **Masked.** A marker inside a fenced code block, an indented code block,
  inline code or a `>` quote is not a control line, whether the text is
  scanned live on stream events or in the final result.
- **Streaming.** Masking state is kept across stream chunks: a fence opened in
  one chunk masks the next.
- **Line shapes.** Outside masked contexts, a marker at line start or as a
  list item is a control line for `NEED_INFO` and `NEED_DECISION`. `VERDICT`
  keeps today's rule of excluding list items.
- **Timing.** Each marker keeps its current trigger timing: `NEED_DECISION`
  stays live, and the others stay where they are.
- **Intentional change.** An indented marker line no longer triggers
  `NEED_INFO`; today it does, because an indented line is a code block in
  Markdown.
Verified by:
- one test per masked context (fence, indented code, inline code, quote) for
  each of the three markers, live and final, where nothing triggers;
- a fence split across two chunks;
- one positive test per marker and line shape that still triggers;
- the existing tests stay green, apart from those that pinned the unmasked
  behaviour, which the tester updates deliberately:
  tests/test_tb_a_needs_info_hold.py, tests/test_tb_a_additional.py,
  tests/test_core.py around lines 2329 and 3473, and
  tests/test_vr_verdict_recording.py around lines 287 and 307.

Reviewer inspection: the three parsers share one rule.

**RM-R2.** A run started with `start_agent(verifies=<agent_id>)`, where the id
names an `ag-*` run the caller may see and that run has a branch or a
captured commit, is seeded from one exact commit. That commit is resolved
once at start and recorded on the reviewing run.
- The worktree's HEAD is that commit, not the base branch. The reviewer's own
  commits go on its own branch, as today.
- If the target is still running, its current tip is used; the recorded
  commit tells the caller exactly what was reviewed.
- For a node run whose branch lives in a managed checkout, the commit is
  resolved through the host's capture authority.
- The start is refused, with an error naming the cause, when the target is
  unknown, not visible, disposed, has no branch and no captured commit, or
  its tip cannot be resolved. It is also refused when `workdir` is
  overridden alongside `verifies`. It never falls back to the base branch.
- A run started without `verifies` is unchanged. An `nd-*` id is refused
  with a message pointing at the node's run ids.
Verified by:
- a test where run B verifies run A, and B's worktree HEAD equals A's tip at
  start, recorded on B;
- the same for a node run;
- one refusal test per cause.

**RM-R3.** A standalone run started with `verifies` can call `give_verdict`
with only `verdict` and `findings`.
- **Identity.** The reviewing run, the verified run and the reviewed commit
  are derived from the host's launch record (RM-R2). The caller cannot name
  them or redirect the verdict.
- **Vocabulary and findings.** The verdict is `approved` or `rejected`, as
  for loops. Findings are validated as the loop tool validates them.
- **Repeated calls.** A second identical call is idempotent. A different
  second verdict is refused with `conflict`, and the first one stands.
- **Text fallback.** A text `VERDICT` line (RM-R1) is used only when the tool
  was never called. If the tool was called, a contradicting text line is
  ignored and noted in the result.
- **Where it shows.** The verdict is persisted with the reviewed commit and
  the verified run's id. It survives a later failure of the reviewing run,
  and `collect_agent` and `check_agent` return it as a structured field.
- **No authority.** It has no authority over any node: it closes, merges and
  reopens nothing.
- **Loops unchanged.** The loop-designated API and its authorisation are
  unchanged. A run with neither a loop designation nor `verifies` still gets
  today's refusal.
Verified by:
- a standalone `give_verdict` returned by `collect_agent`, with the reviewed
  commit;
- idempotent and conflicting repeat calls;
- the text fallback, and a contradicting text line;
- that node state is unchanged;
- a refusal without `verifies`;
- the existing loop verdict tests stay green.

**RM-R4.** Agent guidance text, the shipped agent briefs:
- **reviewer:** run the suite and wait for it to finish. If the shell moves a
  command to the background, poll it until it exits; never end the turn while
  a command is still running. Report the counts observed, or state plainly
  that the suite was not run and why. An approval with no observed counts
  must say so in its first line.
- **implementer**, all three tiers, **and tester:** never commit logs,
  validation output or scratch files. Keep them outside the worktree, or in a
  gitignored path.
Verified by: a golden or content test over the shipped briefs for each rule.
Brief goldens that change are updated deliberately. These tests prove the
guidance ships, not that agents follow it; compliance is watched in reviews.

## Out of scope

- Letting agy reviewers install dependencies; they use the project's `.venv`.
- Any change to loop-designated `give_verdict` semantics.

## Decisions

1. A standalone verdict is advisory and recorded, never authoritative
   (RM-R3). Node authority stays with loops and with the root.
2. No "markers only in the final section" rule (RM-R1). It would risk
   missing a genuine request.
3. The standalone verdict vocabulary is the loop one, `approved` or
   `rejected` (advisor, 2026-10-10). No third value is added.
