# BRIEF — multiagents repairs itself

**Phase: implement.** The review is done. `context/review/REPORT.md` has the
findings, the ledger has their state, and ~380 characterization tests are merged.
This phase turns the chosen findings into work.

`team: implement` is already set in `.multiagents/config/project.yaml`.

The review phase's brief is kept at
`context/review/BRIEF-review-phase.md` — read it for the two invariants
(providers are plugins, agy carries Gemini only), which still hold.

---

## What this project is

`multiagents` is an orchestration tool: a root orchestrator delegates to
specialist subagents, each in its own git worktree on its own branch, and the
parent merges what it accepts. Agents run inside one Docker container per
project, on an internal network whose only route out is a filtering proxy.

The thing that shapes every task here:

> **The codebase you are changing is multiagents itself.** You are running
> inside it. Your worktree is a checkout of the source that spawned you, merged
> your branch, and metered your tokens.

So a defect you fix is a defect in the machine you are standing on. That is why
phase 1 exists and why it comes first.

---

## Running the suite

```
uv run --frozen pytest
```

**Green is `949 passed, 1 failed, 3 skipped`** — measured 2026-09-17 from a
fresh worktree in the container with an agent's environment. The suite grew from
548 to 953 tests when the review's characterization work merged.

**The 1 failure is expected, and it is `F100`.**
`tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated`
passes in isolation and fails in the full suite, because `budget._cache` is
keyed only by provider name and tests bleed state through it. It is scheduled as
phase 1 item 8. **Until that lands, one red test is the baseline, not a
regression you caused.** After it lands, green means green and the number is 953.

The 3 skips shell out to `docker`, deliberately absent in the container.

`REPORT.md` states the suite fails 17 tests in an agent container with
`PermissionError: can_spawn is false`. That did not reproduce on 2026-09-17 with
`MULTIAGENTS_CAN_SPAWN=false` set — one failure, not seventeen. Treat the 17 as
unconfirmed. If you see them, say so and say what your environment had that this
measurement did not.

Two traps that produce failures which are not defects:

- **Running from the project root on the host.** The root is bind-mounted into
  the container, so host and container share `.venv`, and `.venv/bin/pytest`
  carries a stale container-path shebang. A bare `uv run --frozen pytest` there
  runs a different pytest without `mcp` and reports 7 phantom failures. Use
  `uv run --frozen python -m pytest` in the project root. This does not reach
  you: your worktree has no `.venv` and `uv` builds you a clean one.
- **A bare `docker exec` is not an agent.** No git identity → 6 git tests fail.
  No forwarded PATH → `uv: not found`. Both are the harness.

**If a failure looks environmental, it probably is.** Check before filing.

---

## Before anything: who may touch a test

`project.yaml` ships a default `readonly_paths` list — `tests/**`,
`**/test_*.py`, `conftest.py` and friends. **An agent's own list REPLACES this
one rather than adding to it.** So `tester` is exempt with `[]`, and all three
implementer tiers, which omit the key, INHERIT it.

Creating a **new** file under those globs is allowed. Modifying an **existing**
one is reverted at the merge gate. Your run still reports success. `merge_agent`
reports the loss in `readonly_reverted` and `server.py:863` documents reading it
— **read it**, and treat a non-empty value as a failed merge for those paths,
not a footnote. Two runs were lost this way during the review and one left a red
test behind; that is `bug-08f9b3`.

**Three scheduled items require changing existing tests** and cannot be done by
a restricted agent: `bug-08f9b3`'s own test fix, `F50`'s suite rewrite, and
`F150`'s wrong assertion.

**The obvious workaround does not work.** A new test file that supersedes the old
one leaves the old one in the suite, where it runs against the fixed code, goes
red, and strands the agent holding a failure it may not touch.

So: `.multiagents/proposals/agents.yaml` proposes `readonly_paths: []` on
**`implementer-deep` only**, scoped to this phase and given back when the three
rewrites merge.

**It is needed before phase 2, not before phase 1.** Phase 1 can start without
it, because `tester` already carries `readonly_paths: []` and the one assertion
phase 1 has to change is a *contract* error, which is tester's job by
definition:

```python
assert "readonly_paths" not in harness, \
    "the harness builder is the one review agent that may edit shared files"
```

The message claims the agent may edit shared files while the check asserts the
key is absent — which produces the opposite. Send that to `tester`. The other
half of `bug-08f9b3`, landing the fix in
`src/multiagents/defaults/agents.yaml`, is not a test file and blocks nobody.

The exemption earns itself at **`F50`**, where the function and its 48 tests
change together and splitting them across two agents costs a handoff per
iteration.

Everything else keeps the separation. `tester` writes the NEW tests each ticket
names, red, and an implementer makes them green. That is the normal loop and it
needs no exemption.

---

## Phase 0 — the runtime the team runs on

**Inserted 2026-09-22, ahead of everything.** The work described in Phases 1–4
is paused, not abandoned: `refactor/split-consume` carries the R14–R18
implementation and is committed and clean. Come back to it when this phase
lands.

**Eight items, from three channels.** R1–R3 come from
`context/review/C4-runtime-observed.md` (`F200`, `F201`, `F202`); R4–R6 are the
three bug tickets still `awaiting_user` (`bug-b1c130`, `bug-2138e6`,
`bug-b864b8`); R7 and R8 are features the user asked for on 2026-09-22 and are
the only things in this phase that are not repairs. None of R1–R6 was reachable by reading the code — each needed
the logs of real runs — and they are not independent chores: R2 and R4 are the
same function, R4 is why R5 cost two runs, and R1/R2/R3 are one failure
arriving in three steps. That is why they are one phase.

**Two contracts, not one.** The advisor's judgement, taken: R1–R6 are repairs
against evidence that already exists, R7–R8 are features whose shape is still
being decided, and R8 alone reaches the runner, the config layer and the
provider script contract. One contract spanning both would be negotiating a
design while landing fixes. So **R1–R6 first, as one contract; R7–R8 second, as
another.** Nothing in R7 or R8 is blocked by that order, and R1 has to land
before either of them can be tested anyway.

**Close each ticket with `resolve_ticket` when its fix merges.** A ticket whose
fix landed and still reads `awaiting_user` gets refiled by the next review —
which is exactly how `bug-49c1c1`, `bug-7c4b78`, `bug-2a0af0` and `bug-087fee`
came to be declined duplicates.

**Before any agent can be launched at all**, a human runs, once:

```
multiagents docker rm && multiagents docker up
```

The container was created against a claude CLI version that no longer exists
and refuses every spawn. Nothing is running (checked: the newest result-less
run is hours old), so this costs nothing today. It is a manual step because
recreating a container kills whatever is inside it, and that is not a decision
an agent gets to make.

### Status — contract A (R1–R6) DONE, 2026-09-23

Contract: `context/specs/phase0-runtime-repairs.md` (ids `P0-R1.1`…`P0-R6.6`,
with dated amendments from the testers, the adversary and the reviewer).
All merged on `refactor/split-consume`; verified live after a container
recreate and an MCP reconnect. Tickets `bug-b864b8`, `bug-b1c130`, `bug-2138e6`
resolved `fixed`; findings F200–F202 `fixed`. The local `max_steps: 600`
workaround is removed. Full suite: 1179 passed, 73 failed = 72 in
`test_phase2_entry_semantics.py` (Phase 2 R14/R15, paused) + 1
environment-dependent `test_core` test that already failed before Phase 0.
Mutation checks: 12 of 12 caught.

Found on the way: the watchdog's timer loop had been dying on its first poll
(a `quiet_for` method/property collision), so no `timeout` or `silence` trip
had ever been reported. Fixed under P0-R2.9.

**Open follow-ups, small, not yet scheduled** — decide before or alongside
contract B:

1. **agy guidance may be too weak.** In one run (`ag-eafe1e`)
   gemini-3.8-flash-medium had the `agent_guidance` in its prompt and still
   re-read one file head 5×. The seam works; the content may need to say
   "read ranges with the shell". One run is a hint, not evidence.
2. **`stuck` is sticky.** Once set it stays even when the agent resumes normal
   work, and `wait_for_agents` then returns immediately for that agent, so it
   can no longer be waited on. Contract A said when to *report*, never when
   to *clear*.
3. **claude 2.1.280 emits `tool_progress` heartbeats** that no stream rule
   classifies (they land as `raw`). One rule in `providers.yaml`.
4. **The local `doom_loop_repeats: 3`** (gitignored project config) is stricter
   than the shipped 5 and trips on agy's `manage_task` updates. The user's call.
5. **Tooling:** an `implementer-deep` run reported having no `consult` tool to
   reach `dev-advisor`, contrary to the orchestrator protocol. Needs a ticket.
6. **Phase 3 leftovers already on disk:** branch `agents/tester/cf02a1` holds
   R19/R20 tests (25 red), produced when a deferred task restarted itself;
   unmerged because Phase 3 is paused.

### Status — contract B (R7, R8), started 2026-09-23 05:05, unattended

Contract: `context/specs/phase0-context-and-team.md` (P0-R7.1–R7.12,
P0-R8a–R8e). Decisions taken in it, for the user to see:

- **R8c applies to `--unattended` only.** Interactively the CLI holds the
  session for the whole conversation, so there is no gap between turns the
  driver controls; interactive protection is the wind-down notice (R8a), the
  brief (R8b) and the automatic threshold (R8e).
- **R8d opencode is deferred:** `opencode.sh compact` exits 64. The HTTP route
  needs an `opencode serve` process and cannot be verified while opencode's
  monthly quota is exhausted (until 2026-10-05).
- Thresholds shipped: `compact_at_tokens: 120000`,
  `context_wind_down_tokens: 150000` (tunable, 0 disables).

**Result, 2026-09-23 ~05:40.** P0-R7 (team choice) and P0-R8a–R8e are all
implemented and merged on `refactor/split-consume`. The contract tests total
190: 78 for R7 and 112 for R8. Full suite, run by the orchestrator with
`test_phase2_entry_semantics.py` excluded: **1348 passed, 1 failed, 2 xfailed**.
The one failure is
`test_core::test_the_claude_script_uses_the_container_profile_only_where_it_should`,
which already failed at 5249107, before contract B.

The orchestrator reviewed the R7 diff. It found two real-terminal defects the
tests cannot see: `setraw` made the redraw staircase, and a lone Escape
blocked. Both are fixed: the reader now uses cbreak mode, and reads the fd
with a `select` timeout.

**Still open on contract B:**
- **Adversary, reviewer and advisor.** None has looked at the finished diff:
  all three run on agy or opencode, and those were down. R8c/R8d is the part
  that deserves the attack. A compaction is irreversible, and the claude
  script decides success by parsing the transcript.
- **Live check.** `claude.sh compact` has never run against a real session.
  Only a fake CLI has exercised it; the invocation itself was verified by
  hand on 2026-09-22.
- **Step counter looks inflated in the wild.** A tester (`ag-48f51a`) tripped
  `runaway_steps` at 251 in about 5 minutes. Its stream shows bursts of about
  25 `step` events within the same millisecond, which should be impossible
  under P0-R4's turn-only counting. Nothing was killed and the work finished.
  This needs the raw CLI stream to diagnose; it probably joins follow-up 3
  (`tool_progress`).
- **Claude's 5-hour window.** It went from 0% to 88% in about 35 minutes of
  two or three Opus/Sonnet agents; the wrap-up at 05:38 was legitimate. It
  resets at 09:59.

**Progress, 2026-09-23 ~10:50, paused for a `multiagents run` restart
(the user re-authenticated agy):**

- **P0-R8f** (interactive compaction, stop → compact → resume, 30 s grace).
  The user asked for it; it is in the contract, with the tester's amendments.
  Its tests are merged (`tests/test_phase0_interactive_compact.py`, 56 tests,
  30 red). **Not implemented yet. Next step:** `implementer-deep` on
  P0-R8f.1–R8f.7, with `verifies` = ag-2c808f.
- **The attack on R8a/R8c/R8d is merged** (`tests/test_phase0_contract_b_attack*.py`).
  The adversary was unreachable (`model` cannot cross providers), so
  `tester` ag-57985b stood in for it. Ten real defects, red, ranked:
  1. A compaction that times out keeps running as an orphan while the next
     turn starts on the same session.
  2. The slug rule is wrong for paths with spaces or `+@~` or non-ASCII:
     Claude replaces every non-alphanumeric character, and hashes paths
     over 200 characters. This affects `watchdog.transcript_source` and
     `claude.sh`.
  3. Non-UTF-8 output from a script crashes `run_action`, which promises
     never to raise.
  4. `wc -l` is off by one on an unterminated last line, which yields a
     false success.
  5. A malformed limit (`120k`) crashes `_compact_if_due`.
  6. One non-UTF-8 byte in the transcript turns a real compaction into a
     failure.
  7. A huge line is read in quadratic time.
  8. `Infinity` in a usage field raises `OverflowError` on every tool call.
  9. String usage fields are concatenated instead of added.
  10. The failure line is printed unbounded.
  Four findings are accepted as `xfail(strict)`, with reasons in the tests.
  **Next step:** route 1, 3, 5, 10 (driver/scripts) and 2, 4, 6, 7, 8, 9
  (reading/claude.sh) to an implementer. Finding 2 also touches the
  pre-existing `launch` arm of `claude.sh`.
- The reviewer and advisor pass on contract B is still to do, now that agy
  is back.
- `runaway_steps` fired falsely again (ag-57985b at 251), which confirms the
  inflated step counter noted above.

**Progress, 2026-09-23 ~11:30 (after the restart):**
- **Advisor review of contract B (ag-25c350, turns 3–4).** It first said to
  drop R8f and claimed the invariant was broken and the thresholds inverted.
  After checking, it withdrew three of those points: the cited lines predate
  this work, compaction only happens while idle so the wind-down still fires
  while agents run, and the tester did find the slug bug. It kept one point:
  unsubmitted typing is invisible to the driver. Decided with the user in
  c886f79: idle default 300 s, a bell (`compact_bell`), "send … to cancel"
  wording, and all values configurable (R8f.8, R8f.9). The tester's questions
  were decided in e3c1b96.
- **Merged:** the updated R8f tests (4800277, 47 red until implemented).
  Attack findings 2, 4, 6, 7, 8 and 9 are fixed (52105e7). The long-path
  (>200 chars) hashed slug is **not** done, because no test asks for it. It
  stays open.
- **In flight:** `implementer-deep` ag-829577 on R8f and driver findings
  1, 3, 5 and 10. `implementer-quick` ag-9c3885 on the monitor history sort
  (the user decided it). `implementer-quick` ag-77113c on the researcher
  brief.
- **The monitor history "missing" advisors.** Consult runs are in the
  history but buried: roots are sorted by `started_at`, and a conversational
  agent keeps one node across days (ag-25c350 started 2026-09-22 20:42). The
  user decided to sort roots by last activity.
- **Tooling defects seen today** (Phase 0 follow-ups):
  1. `stuck` stays on a node after the agent recovers or finishes.
  2. The silence watchdog fires while an agent waits on its own long
     background test run.
  3. `doom_loop` false positive on agy: `view_file` events carry the path
     but not the line range, so paging through a file looks like a loop.
  4. `total_tokens` excludes cache reads. ag-4548ac showed 1.86M, but
     processed about 8.1M.
  5. An agy agent can end its turn while its own background job is still
     running, and so commit nothing (ag-9c3885).
- **Researcher token cost; the experiment is running.** ag-4548ac (a
  researcher on agy) used 128 steps, with 1.8M uncached input plus 6.3M
  cache reads, and ended with a context of about 157k. It paged whole large
  files (`runner.py` 10×, `tree.py` 7×) instead of searching. Its brief
  said "read widely", and my task broke "one question per run". The user
  approved a brief change: locate with `rg -n`, read line ranges only, stop
  once answerable (ag-77113c). **Evaluate it on the next researcher runs**:
  record steps, uncached input and cache reads, and the final context, per
  run, against the ag-4548ac baseline. A small single-question baseline is
  ag-85e40d, at 34k.
  **ag-77113c was DISCARDED; the change is not applied yet.** It force-added
  a new `.multiagents/config/agents/researcher.md` to git. `.multiagents/`
  is gitignored, and that path already holds an old untracked project
  override from 2026-09-05, which a merge would have clobbered. Its version
  also lacked the "Calling this agent" section. Its edit of
  `src/multiagents/defaults/agents/team/researcher.md` was correct; redo it
  alone. The live copies are the global
  `~/.config/multiagents/agents/team/researcher.md` (identical to the old
  default) and that old project override. First find out, with a researcher
  or by reading the config loader, which one wins. Then have the user update
  the live copy, or run `multiagents` config sync if there is one. Never
  commit under `.multiagents/`.
  **If it works, consider the same rule for other read-heavy agents**:
  reviewer, advisor, dev-advisor, auditor, cartographer, characterizer.
  Their briefs have not been checked for similar "read widely" wording yet.

**Handoff, 2026-09-23 ~12:40 (orchestrator context wind-down at 155k):**
- **Merged since ~11:30:**
  - R8f and driver findings 1, 3, 5 and 10 (d4ae4ec).
  - The monitor history sorted by last activity (29218c6).
  - The F110/F111 pins inverted (137f37f); the ledger still needs
    `set_finding_status` for F110 and F111 → fixed, naming d4ae4ec.
  - Spec decisions b23fb61 and c483d51: a malformed limit now falls back to
    its **shipped default**, not off. **The code still falls back to off.**
- **Next implementer task** (driver.py/scripts.py/claude.sh, one run,
  `implementer` tier):
  1. Implement c483d51: malformed `compact_at_tokens`,
     `context_wind_down_tokens` and `compact_timeout_seconds` fall back to
     the shipped default. Check the server's `_limit` too.
  2. Safe-parse `restart_attempts`, `restart_delay_seconds` and
     `limit_max_waits` (same crash class as finding 5, `driver.py` ~505).
  3. Reviewer ag-e8565d findings (VERDICT rejected; its report is in
     `review.md` on its branch, which is not merged):
     - SIGTERM leaves the terminal dirty (`driver.py` ~204, `cli.py`
       picker);
     - `run_action` leaks the child on KeyboardInterrupt (`scripts.py` ~186);
     - a clean CLI exit racing with grace expiry is relaunched instead of
       honoured (`driver.py` ~810);
     - `claude.sh` ~523 uses `echo` on CLI output: use `printf`.
     The tester should first write red tests for the first three. Declined:
     `transcripts._usage_of` using "message"/"usage" (it predates this work,
     and the invariant lists `compact_boundary`, `compactMetadata` and
     `/compact`); caching `_launched_spec`; moving the claude.sh parse into
     Python (it would move provider vocabulary INTO Python).
- **Not yet run:** the adversary on R8f (task text: attack stop → compact →
  resume, signals during grace, compaction and relaunch, loops, the
  process-group kill). Also a live check of `claude.sh compact` against a
  real session.
- **The advisor reviews stale code.** A conversational agent's worktree stays
  at the commit where its conversation began (ag-25c350: 2026-09-22 20:42),
  and it could not run `git show`. Its contract-B review read pre-contract-B
  code. File a ticket with the bug-reporter, or start a fresh advisor
  conversation. Its turn-5 advice was partly taken (c483d51); its
  `setsid` → `setpgrp` point was declined, because `login` uses
  `exec_action` and a tty read in a background group gets SIGTTIN.
- **More tooling defects:**
  6. The implementer-deep toolset lacked `consult` (dev-advisor), ag-829577.
  7. Stuck and idle nodes seem to count against `max_concurrent`:
     `start_agent` refused with "4 running" when 2 were really running.
  8. `test_core.py` has 18 tests that read the live `MULTIAGENTS_CAN_SPAWN=0`
     etc., so they fail inside any agent that cannot spawn (ag-b3d873).
  9. agy agents repeatedly end their turn with a background test still
     running (ag-9c3885, ag-77113c twice).
- **Leftover nodes to clean:**
  - ag-4548ac, a researcher: done but marked stuck; nothing to merge.

**Progress, 2026-09-23 ~13:50:** the red tests have merged: R8f.10-13
(42fa3de), CF (consult refreshes the worktree, bug-7f6ba7, 62e5a37), and the
adversary's R8f findings (12d893f). The decisions are cd59fec. The researcher
brief merged as 5603870, but the project copy
`.multiagents/config/agents/team/researcher.md` still shadows it. **Next, at
15:01 (a pause the user asked for):**
- implementer-deep on R8f.10-17, plus echo→printf (driver.py, scripts.py,
  cli.py, claude.sh, server.py);
- a second implementer-deep, in parallel, on CF-R1 to CF-R7 (runner.py).
The tickets bug-7f6ba7 and bug-2cebea are parked for the user.
**15:01:** the R8f implementer-deep was DEFERRED (tree paused until 15:16,
claude's reading still stale), and it restarts on the next `wait_for_agents`.
The CF implementer-deep (runner.py, tests/test_consult_fresh_worktree.py,
spec consult-fresh-worktree.md) has NOT been started yet: start it once the
pause clears. The researcher brief is now live: the project copy was
overwritten at the user's request.

**Progress, 2026-09-23 ~17:00:**
- **Merged:**
  - R8f.10-17 (a8e695f), with the review fixes in 478f276;
  - the CF fix for bug-7f6ba7, where consult refreshes a conversational
    worktree (6035c65, round 2 in 6b24462);
  - the tests: 42fa3de, 62e5a37, 12d893f, 3832697, 4d860fa, 575193e.
  The full suite was green on the round-2 branch (1549 passed).
- **Declined:**
  - an empty commit counts as absorbed;
  - `fcntl` on Windows, since the project is POSIX-only already;
  - the `stopping` attribute on a callable (style only);
  - the headless "no shrink" loop test, because R8c.3 wins.
- **Not yet done:**
  - a live check of CF, which needs the MCP server restarted (/mcp),
    because the running server still has the old runner code;
  - then one consult of the advisor, to confirm it reads current code;
  - a live `claude.sh compact` check against a real session.
- **Noted, not fixed:**
  - `claude.sh launch` still looks under `$HOME/.claude`, not
    `CLAUDE_CONFIG_DIR`;
  - the usage-limit stop has the same exit race as R8f.12;
  - `limit_wait_seconds`, `restart_min_runtime_seconds`,
    `supervised_turns` and `spend_limit_pause_hours` still parse unsafely;
  - implementer-deep again had no `consult` tool (ag-d67496), which is
    tooling defect 6, still without a ticket.

**Handoff, 2026-09-23 ~17:30 (orchestrator wind-down at 205k):**
- **CF verified live after the /mcp restart.** The advisor ag-25c350 was
  moved from 7e6b8b1 to d5c9a82 and got the "updated" line. Its final
  review of `22fed69..HEAD -- src/` found nothing to regret. bug-7f6ba7 is
  resolved as fixed.
- **Standing rule (user):** tickets are fixed here, not sent upstream.
- **The merged base is sound:** 1548 passed and 4 failed. The 4 fail the
  same way on 22fed69, because the orchestrator's environment leaks
  `MULTIAGENTS_*` into them (3 `test_doctor_clear_*` tests and
  `test_the_claude_script_uses_the_container_profile_only_where_it_should`).
- **Next, in the order I would take them:**
  1. **bug-2cebea:** finished runs marked `stuck` keep holding
     `max_concurrent` slots, and the `stuck` label never clears. It costs a
     manual discard several times a session. Also fold in the agy doom_loop
     false positive: `view_file` arguments carry line ranges that the
     comparison drops.
  2. **Test isolation:** make `test_core.py` independent of the ambient
     `MULTIAGENTS_*` / `CLAUDE_*` environment. That covers the 18
     CAN_SPAWN tests and the 4 above. It is cheap, and it makes every
     agent's "suite green" trustworthy.
  3. **Tooling defect 6:** implementer-deep has no `consult` tool, so it
     cannot reach dev-advisor (ag-829577, ag-d67496).
  4. **R8f leftovers:**
     - `claude.sh launch` should honour `CLAUDE_CONFIG_DIR`;
     - the usage-limit stop has the same exit race as R8f.12;
     - safe parsing for `limit_wait_seconds`,
       `restart_min_runtime_seconds`, `supervised_turns` and
       `spend_limit_pause_hours`.
  5. **bug-c050b0:** the burn-rate projection is built from a 39 s burst,
     which triggers a premature wrap-up and pause. Its proposed fix is in
     the ticket.
  6. **agy agents end their turn** with a background test still running,
     which has happened repeatedly.
  7. **A live `claude.sh compact`** against a real session.
  8. **Agents survive a restart of the orchestrator's CLI** (the user
     agreed to this, 2026-09-23 ~20:50). Today the Runner lives in the MCP
     server, a stdio child of the CLI. When the CLI stops cleanly, the
     running agents are cancelled (`runner.py` ~1326-1353). After a crash,
     the next `multiagents run` reaps them as `orphaned` (`cli.py`
     ~970-1013). No spend runs away, but the work in progress is lost.
     Cases where this costs us:
     - R8f compaction can only fire while the tree is idle;
     - a usage-limit restart of the orchestrator's own CLI
       (`driver.py` ~1010-1030);
     - a `multiagents run` restart to pick up new code or re-auth;
     - a stray `/exit`.
     The advisor's analysis is ag-25c350, turns 11-12. The smallest
     version:
     - agents write their stream to a file in their run dir, not to a pipe
       owned by the server;
     - on an intentional stop (compaction, limit wait, restart) the server
       leaves live agents running;
     - the next server adopts live nodes (replay the file, then follow it)
       instead of reaping them.
     Rejected, for now: a separate daemon (lifecycle, auth prompts), and
     an HTTP MCP server hosted by the driver (the driver itself dies on
     `/exit`). To settle in the contract:
     - a lock on `tree.json` (two servers, if the old CLI was suspended);
     - following a file across the docker bind mount;
     - whether a docker agent really loses its output when its
       `docker exec` dies (the advisor's claim, unverified);
     - exactly what a limit restart does to agents in each mode.
     Full pipeline, on `implementer-deep`. Adversary: yes, since it deals
     with processes and concurrency.
- **Researcher experiment:** the new brief is live (the project copy was
  overwritten). No researcher has run on it yet; compare the next run
  against ag-4548ac and ag-f2cb6d.

### Awaiting the user

- **(re-authenticated by the user ~10:40)** **agy could not authenticate in the container (2026-09-23 05:03).** The stored
  token expired at 00:10 UTC and the silent refresh fails with
  `Post "https://oauth2.googleapis.com/token": Unable to connect`
  (`~/.multiagents/container-state/shared/agy/.gemini/antigravity-cli/log/cli-20260923_030345.log`),
  although `googleapis.com` is in `egress_allowlist`. `auth_status` still says
  "authenticated". Fix: `multiagents auth login agy` (needs a person), then
  find out why the refresh cannot reach Google through the proxy — probably
  a multiagents defect worth a ticket (the bug-reporter itself runs on
  opencode/agy, so it could not be filed tonight). Consequence: advisor,
  reviewer, dev-advisor, researcher, adversary and bug-reporter were all
  unavailable; contract B was written without an advisor review, and runs on
  claude only.
- **`autocompact:` on the orchestrator** (P0-R8e.2): no shipped default;
  whether to set one locally is your call.
- Follow-up 4 above (`doom_loop_repeats`).

### R1 — the versioned mount (`F200`)

`src/multiagents/executor/docker.py:340-352` mounts both a provider's launcher
and the path its symlink resolves to. The second is resolved at container
creation, which pins one version number into a mount list that is fixed for the
container's life. The claude CLI updates itself; the next update bricks the
project until someone does the manual step above, killing every in-flight agent
with it.

**Mounting the versions directory is necessary and NOT sufficient.** The
advisor raised this and it checks out, though not for the reason it gave. It
said docker binds the launcher symlink and the container would keep a stale
symlink. Measured instead, inside the live container:

```
host:      ~/.local/bin/claude  ->  symlink, 52 bytes, -> versions/2.1.280
container: /home/.../.local/bin/claude  ->  regular file, 233,709,640 bytes
```

Docker **resolved** the symlink at mount time. The launcher path inside the
container is a single-file bind of one version's *inode*, wearing the
launcher's name. So mounting the versions directory makes new versions visible
and changes nothing about which one gets executed — the container would still
run the binary it was born with.

And the old versions are not cleaned up (`2.1.261`, `2.1.273`, `2.1.274`,
`2.1.278`, `2.1.280` are all on disk today), so nothing would fail. **The
container would silently keep running a superseded CLI**, which is worse than
the refusal we have now. The refusal is ours (`f963ba3` compares the declared
mounts against the config); the kernel would not have complained.

So the question the contract has to answer is not which path to mount, it is
**how the container resolves the binary at exec time instead of at creation
time**. Mounting `~/.local/bin` wholesale is one answer and a blunt one — it is
a directory of unrelated binaries. Resolving inside the container, against a
mounted versions directory, is another. Settle it there, with the constraint
below.

**Do not special-case claude by name.** Any provider whose launcher resolves
into a versioned directory has the same exposure, and naming one in the
executor is the hardcode this project exists to avoid.

### R2 — re-arm the watchdog (`F201`)

`src/multiagents/supervisor.py:199-203`. `_trip` latches for the whole run, so
the first alert is the only alert — including for a *different* condition
firing later. Measured: `ag-179bc2` tripped correctly at 5 identical
`view_file` calls and then ran to 116 events in silence.

Three requirements, and the third is the one that is easy to get wrong:

- a condition that differs from the one already reported is reported (a doom
  loop followed by a wall-clock timeout is two facts, not one);
- a *recurring* condition re-arms after a further N repeats, with N
  configurable and sitting beside `doom_loop_repeats`;
- **a monotone condition never re-arms.** `runaway_steps` fires on
  `self.steps > self.max_steps`, and `self.steps` only grows — so a naive
  re-arm makes it trip on *every subsequent event* for the rest of the run.
  Same for `timeout`. These are terminal states, not recurring ones: report
  once, never again. Only `doom_loop` is genuinely repeatable.

That third requirement came out of the advisor's review, which argued R2 and R4
should not land together because R2 would "weaponize" R4 — an inflated step
count plus a re-arming watchdog equals `runaway_steps` spam on every claude
agent. The premise is right and the conclusion does not follow: the spam comes
from re-arming a monotone condition, which is wrong on its own terms whatever
R4 does. Fix the re-arm rule and the interaction disappears, which is why R4
stays scheduled rather than parked.

**This will still make runs noisier.** That is the point. It is scheduled
before R3 because it is provider-agnostic, and because it is the only thing
that will tell us whether R3's compensation actually worked.

### R3 — a seam for provider-specific prompt guidance (`F202`)

`notes:` is parsed off every provider (`src/multiagents/providers.py:153` and
`:196`) and read by nothing. So a provider's configuration cannot influence what
its own agents are told, and `compose_prompt` has no provider-dependent branch
at all.

That matters because of a defect we do not control. agy's `view_file` returns,
on truncation:

> The above content does NOT show the entire file contents. If you need to view
> any lines of the file which were not shown to complete your task, call this
> tool again to view those lines.

It says "call this tool again" and names no pagination argument. A model that
follows it literally re-reads the same head forever — and this is **not a model
problem**: two of the seven loops measured on 2026-09-22 were
`agy/claude-opus-4-6-thinking`, and the same model under the claude CLI never
loops. It is the tool's message.

Add a key to `providers.yaml` that the runner appends to the prompt **for that
provider's agents only**. Requirements:

- absent key means the prompt is byte-for-byte what it is today;
- the fragment reaches agents on that provider and no others;
- **it is not `notes:`** — configuration commentary written for a human reader
  must not start being sent to models because the two shared a field;
- agy's tool name and its English error string appear in `providers.yaml` and
  **nowhere in `src/multiagents/*.py`**. That is the providers-are-plugins
  invariant, and it is the specific thing the user asked the review team to
  watch.

Then carry the pagination guidance in agy's block.

### R4 — the claude step counter (`bug-b1c130`)

**Same function as R2** (`Observer.observe`), so land them in an order that
keeps the diffs legible — but they are **not** coupled, once R2's third
requirement holds. The advisor argued for parking R4 until R2 shipped, on the
grounds that R2 would amplify it; that amplification is the monotone re-arm
bug, and R2 fixes it rather than causing it. R4 stands on its own evidence.

`supervisor.py:71-72` has two counting paths: `self.steps = max(self.steps,
event.step + 1)` when the provider reports a step index, and `self.steps += 1`
otherwise. agy maps one (`providers.yaml:174,179` →
`step_update.step_index`), so its count is monotonic per turn. The claude block
maps `assistant`, `user` and `system` to `as: step` with `fields: {}`, so
`event.step` is always `null` and every streaming delta increments. Verified in
`.multiagents/runs/ag-329af1/stream.jsonl`: every `step` event carries
`"step": null`, and they arrive in bursts sharing a millisecond.

Cost so far: `ag-329af1` killed at 121 steps after 16 tool calls, uncommitted
work dropped; `ag-6f5a9c` reached 295 step events across 37 tool calls.

**The open question the contract phase has to settle**, because it is not a
mapping: claude's stream-json carries no step index to map. Counting distinct
`message.id` values would give turn semantics, but `providers.yaml` has no way
today to express "count distinct values of a field" — only "lift this path".
So either the stream rules gain that, or the supervisor derives a turn boundary
from something claude does emit. **Whichever it is, the answer belongs in
`providers.yaml`, not in a `if provider == "claude"` in the supervisor.**

**There is a live workaround to unwind.** `.multiagents/config/project.yaml`
carries `max_steps: 600`, raised by hand to survive this; the shipped default is
250. That file is gitignored, so no agent can see it — the person merging this
has to revert it, and a run that still needs 600 afterwards means the fix did
not work.

### R5 — config staleness in the MCP server (`bug-2138e6`)

The MCP server calls `load_config` once at startup and holds it for the life of
the process; `multiagents run` reloads per command (`cli.py:72,204,474`). So an
operator who edits `project.yaml`, verifies it through the CLI, and then spawns
agents through MCP gets the old value with no warning.

This is not hypothetical and it is not cheap: on 2026-09-22 an operator raised
`max_steps` from 120 to 600 to work around R4 and lost two more runs to a
ceiling that no longer existed on disk.

The precedent for what to do is already in this codebase and is named in the
ticket: `executor/docker.py:961-975` refuses a container whose mounts no longer
match the config and says so, rather than running stale (that is also what
produced R1's diagnosis). Either reload, or refuse and say which key went
stale — but silence is the one option ruled out.

### R6 — `wait_for_agents` and the paused queue (`bug-b864b8`)

A pause recorded by an unrelated deferred task makes `wait_for_agents` return
immediately with `paused: true` and **no `still_running` field at all**, while
`check_agent` on a live agent in the same tree reports it running and making
progress at the same timestamp.

Two requirements:

- a pause on the deferred queue does not mean the tree is idle; live agents are
  still waited on;
- `still_running` is reported whether or not a pause is in force. Omitting a
  field is how a caller concludes there is nothing running.

Filed 2026-09-17 and set aside then for budget. It is scheduled now because R1
through R5 will have the orchestrator waiting on agents constantly.

### R7 — choose the team when `init-agent` starts

**The one feature in this phase**, and it is here rather than in a queue of its
own because it lives in the same command surface as R1–R6 and because the
mistake it prevents is expensive: `init-agent` today shapes a project under
whatever `team:` happens to be left in the config from last time. Getting that
wrong is not a typo — it is an initializer having a long conversation about the
wrong phase.

`cmd_init_agent` (`src/multiagents/cli.py:69`) loads the config, reports the
state, runs its checks and launches. It never mentions the team. Make it offer
the choice first, write the answer, then launch.

**What it shows.** `teams:` in `project.yaml` already carries a `description:`
per team (`src/multiagents/defaults/project.yaml:192+`), reachable as
`config.teams` (`src/multiagents/config.py:415`). Today that is `implement` and
`review`. The list is therefore self-documenting and **must be read from
config** — a hardcoded list of two names is a third place to update when a
third team is added.

**How it behaves.**

- A cursor moving over the list, the current `team:` highlighted on entry, so
  Enter on an unchanged project is a no-op.
- Cancelling (Escape, `q`, Ctrl-C) leaves the config untouched and does not
  launch. Choosing the team already set writes nothing.
- **Not a tty → no prompt.** Keep the configured team, print which one is in
  force, carry on. Every other prompt in this file is guarded that way
  (`cli.py:313`, `:367`, `:492`, `:509`) and for a stated reason: a closed stdin
  must never let `make init` change a project's setup with nobody deciding.
- `--team <name>` for scripts and for re-running without the prompt. An unknown
  name fails with the list of real ones rather than launching on a default.

**The trap, which has a precedent in this file.** Writing `team:` back must not
round-trip the YAML. `project.yaml` is mostly comments explaining the choices,
and dumping it through the parser deletes all of them — `_set_executor`
(`cli.py:442-460`) already solved exactly this with a targeted line edit and
says so in its docstring. Follow it: a `_set_team` beside it, same shape, same
reason. Note one difference — `executor.kind` is always present to substitute,
but a project that has never set `team:` inherits it from the defaults layer and
has no line to replace, so this one has to insert as well as substitute, and a
test should cover the empty case.

**Reconcile the initializer's brief.**
`src/multiagents/defaults/agents/team/_initializer.md:94-95` tells the
initializer that changing the team is "a one-line change to `team:` in
`project.yaml` — put it to the user rather than editing it yourself". That stays
true and stays right, but it now reads as if hand-editing were the only route.
Say instead that the user picks the team when they start `init-agent`, and that
what the initializer proposes is the team for the **next** phase.

### R8 — the orchestrator and its own context window

**Compaction can be triggered from outside the session. Verified on
2026-09-22**, end to end, against `claude 2.1.280`:

```
claude -p "/compact" --resume <session_id> --output-format json
```

returns `subtype: success`, `num_turns: 0`, an empty `result` and the **same**
`session_id` — the slash command is consumed by the CLI rather than sent to the
model, so it costs no turn of its own. The session's transcript then carries:

```json
{"type": "system", "subtype": "compact_boundary",
 "compactMetadata": {"trigger": "manual", "preTokens": 27729,
                     "postTokens": 1607, "durationMs": 23771,
                     "cumulativeDroppedTokens": 26122}}
```

`trigger: "manual"` is the same value the binary carries as
`compactionRequestKind: "manual"`. A real compaction, on demand, 27.7k → 1.6k in
24s, and the next `--resume` continues from the summary.

**So the orchestrator does not do this — multiagents does, between turns.** A
session cannot resume itself while it is running, and it does not have to:
`--unattended` already spawns a turn, waits for it to end, and starts another
(`driver.py:317-320`). The gap between two turns is where a compaction belongs —
no re-entrancy, and the same place that already knows the session id.

The second lever is `--autocompact <auto|tokens>` (documented in `claude
--help`, accepts `auto` or 100k–1M), which sets where *automatic* compaction
fires. That one is **one line in `providers.yaml`** and no Python at all: the
claude block already has an `optional:` map turning a config key into a flag
(`providers.yaml:66-67`, `max_budget_usd`). Adding `autocompact` there is the
plugin seam working exactly as intended.

The requirement is therefore both halves: **never be in a state where a
compaction loses something**, and **compact deliberately, at a boundary we
choose**, rather than being surprised by the automatic one.

**This is the quota problem again, and the answer is already written.** When a
provider window is about to close, `_wind_down` (`runner.py:318-340`) stops
sending new work and `WRAP_UP` (`runner.py:62-77`) tells the agent to commit,
write a handoff, and stop — because *"the work resumes from your branch and
this handoff, not from your memory of this conversation."* That paragraph is
exactly as true of a context boundary as of a quota one. `_wind_down`'s
docstring also carries the lesson that matters most here, learned from an
advisor: interrupt **early**, because a handoff written while the window is
still draining is cut off too.

Three parts, in order of value.

**R8a — a wind-down for context.** The sensor exists:
`transcripts.context_tokens(usage)` (`transcripts.py:123`) computes it, and
`walk_session` already reads the live transcript whose location the provider
declares (`providers.yaml`, claude's `transcript.dir`). When the orchestrator's
own context crosses a lead threshold — a `context_wind_down` beside
`wind_down_seconds`, same shape, same reason — it gets the `WRAP_UP` treatment
adapted to this pressure: finish the merge in hand, record the statuses, write
the handoff, start nothing new.

**State the limit rather than hiding it:** only claude declares a transcript
location. opencode keeps sessions in sqlite and agy in an opaque brain
directory, and `providers.yaml` says so already. So this senses a claude
orchestrator and nothing else. That is fine today — the orchestrator is pinned
to `claude/opus` — but it must degrade to "no reading" rather than to "plenty
of room", which is the same mistake `budget_status` warns about with
`known: false`.

**R8b — name what does not survive.** The brief should say which state is
durable and which is only in the conversation, because the orchestrator cannot
judge that in the moment. Durable, and therefore free: `BRIEF.md`, the ledger
(`list_findings`), tickets (`list_tickets`), the tree (`agent_tree`), branches
and commits. Not durable: which agents it is waiting on and why, the reasoning
behind a merge it has decided but not yet made, a finding it has judged but not
yet given a status, a question it meant to ask the user. The instruction is
**record it when you decide it, not when you are about to lose it** — a habit,
not a boundary ritual. A `set_finding_status` costs one call; reconstructing the
judgement after a compaction costs a re-read of the evidence.

**R8c — the right moment, in interactive mode.** The right moment to compact is
not a token count, it is **a work boundary that has just closed with its result
on disk**: a phase finished, a branch merged, a ticket resolved. A compaction
there loses nothing by construction, because everything the next turn needs is
in a file. Mid-task it is the opposite — the reasoning that has not been written
down yet is precisely what compaction drops first, and `transcripts.py:51-52`
already records that tool results go first.

So the driver compacts there, and only there: between turns, when the last turn
ended at a closed boundary and context is over the threshold. Interactively it
says what it did and what the figures were, the way `compactMetadata` already
reports them. It must **not** nag, and it must never compact mid-task — a turn
that ended with work still in the orchestrator's head and not on disk is the one
turn where this is destructive, because compaction drops tool results first
(`transcripts.py:51-52`).

Requirement, stated because it is the part that can silently regress: the
compaction is **verified**, not assumed, and a missing confirmation is a failure
rather than a success.

**The verification belongs in the provider script, not in the core** — the
advisor caught this and it is right. `compact_boundary`, `compactMetadata`,
`trigger: "manual"` and the transcript layout that carries them are all claude's
vocabulary; parsing them in `src/multiagents/` would put provider logic back in
the core one item after R3 took it out. So `providers/claude.sh compact <sid>`
performs the call, reads back its own record, and reports through its exit code.
The driver learns success or failure and never learns what a `compact_boundary`
is. `preTokens`/`postTokens` make the script's own check exact, and it may print
them for the log.

**R8d — the other two providers, which do not work like claude.** Measured on
2026-09-22 against the installed binaries. Three providers, three unrelated
mechanisms — which is the whole argument for putting this behind the provider
seam rather than in the runner.

| provider | route | state |
|---|---|---|
| `claude` | `claude -p "/compact" --resume <sid>` | **works**, verified above |
| `opencode` | `POST /session/{id}/summarize` on its HTTP server | route confirmed in the binary; the CLI route is broken |
| `agy` | none found | compaction is internal |

**opencode** is a client/server design, and the server exposes the operation:
the literal `"/session/{id}/summarize"` is in the binary, alongside
`session.compact`, `session.summarize` and `session.compacting`. `opencode
serve` starts that server and `opencode run --attach <url>` joins one, so the
call is reachable. The documented CLI route is **not** usable as it stands:
`opencode run --command compact --session <sid>` is recognised — it does not
report an unknown command — and returns

```json
{"type":"error","error":{"name":"UnknownError",
 "data":{"message":"Unexpected server error. Check server logs for details.",
         "ref":"err_0ab962ed"}}}
```

against a healthy 9,888-token session. Sending `/compact` as an ordinary message
just reaches the model, which answers it. So: use the HTTP route, and treat
`--command compact` as unavailable. That failure is a third-party defect, not
ours — no `TICKET`, which is the channel for multiagents' own bugs — but it is
worth reporting upstream and worth re-testing on each opencode release.

**agy has no external trigger, and the attempt is expensive.** `agy
--conversation=<id> -p "/compact"` does not compact: print mode *expands* slash
commands into the prompt (`agy --help`: "Disable slash command and skill
expansion in print mode"), so the text reached the model, which ran two turns,
tried to invoke a command tool, was auto-denied for lack of a permission rule,
and produced nothing. **It cost 42,752 tokens.** Do not retry it. agy's
compaction is configured through a protobuf message —
`genai.AntigravityAgentConfig.AntigravityCompactionConfig` and
`antigravity.localharness.CompactionConfig` are both in the binary — which is
internal, versioned with the CLI, and not something to depend on. For agy the
honest answer is that the context wind-down (R8a) and the durable-state
discipline (R8b) are the whole protection, and R8c does not apply.

**Where this goes.** Not in the runner. A provider script already answers
actions — `check`, `login`, `budget`, with `exit 64` meaning "I cannot, use the
fallback" (`providers/claude.sh:374-382`). Add a `compact` action on the same
contract: `providers/claude.sh compact <session_id>` runs the CLI invocation,
`providers/opencode.sh compact <session_id>` makes the HTTP call, and
`providers/agy.sh compact` exits 64. The driver asks the provider and does not
know which of the three it got. Any `if provider == ...` in Python here is the
hardcode the plugin invariant exists to forbid, and this is the case that would
tempt it most, because the three mechanisms genuinely have nothing in common.

**One concrete gap, raised by the advisor and confirmed:** a `compact` action
needs the session id, and no action takes one today — `run_action`
(`scripts.py:172-175`) passes only the action name. The seam for it already
exists though: the same signature ends in `extra_env`, which `build_env` merges
into the script's environment. So the session id reaches the script as an
environment variable, the way the action contract already carries everything
else, and no new parameter is needed.

**Under `--unattended` this matters most**, not least: nobody is there to notice
a window filling, and the automatic compaction will fire mid-task at whatever
moment it chooses. Deliberate compaction at a boundary is the whole difference
between the two.

### Not in this phase

`F203` (four opencode agents fall back onto agy, which is the looping CLI, and
opencode is at 99% of its monthly cap until 2026-10-05) and `F204` (the prompt
preamble ordering) are both **accepted, with reasons in the ledger**. R2 and R3
between them remove what was damaging about F203. F204 was measured rather than
judged: cache read beats creation 26.7:1, the addressable surface is under 3.5%
of claude-side tokens, and reordering recovers almost none of it.

`critic` has no fallback and stops when opencode's cap binds. Raised with the
user on 2026-09-22; it is not used on this project, so that is accepted and is
not work.

**None of these three is a cost optimisation and none should be sold as one.**
Measured on 2026-09-22: all seven looping runs were agy, which is unmetered, so
the loops cost nothing in money. R2 will *increase* orchestrator requests, not
reduce them. These are robustness fixes.

---

## Phase 1 — repair the tool, before using it — **DONE**

**Landed. Kept here because it records what was decided and why, and an agent
that cannot tell finished work from planned work will redo it.**

All seven tickets below read `fixed` in `list_tickets`, and two were spot-checked
in the code rather than trusted: `--init` is at
`src/multiagents/executor/docker.py:989`, and the `readonly_paths` corrections
are at `src/multiagents/defaults/agents.yaml:426` (`harness: []`) and `:501`
(`reporter: ["src/**", "tests/**"]`). The assertion at `tests/test_core.py:10592`
that used to encode `bug-08f9b3` is gone.

Phase 0 above is the current phase.

The review filed **7 blocking tickets** against multiagents itself. They are not
an upstream queue here — the user is the maintainer, and three of them actively
break the tool this team is running on. Fixing the allowlist while the container
is killing its own agents is work you will do twice.

Read each with `list_tickets`. **Every one carries a proposed fix and names the
test that should have caught it** — write that test, not merely a test.

| # | ticket | what it costs |
|---|---|---|
| 1 | `bug-cfdc71` | The workspace container has no PID 1 reaper. Fork-heavy work — running this suite, i.e. your normal job — leaves zombies until `pids_limit: 512` is exhausted. Measured: 509 processes, 505 zombies, then every agent dies with signal-shaped exits whose text blames Bun and innocent providers. Proposed fix: add `--init` to `run_args()`. **Do this one first.** |
| 2 | `bug-565863` | `budget_tag` enforcement and reporting sum raw usage keys instead of `token_count()`, which already exists and fixes exactly this elsewhere. Claude spend counts as zero. **This is why the review covered 2 of 7 contexts.** |
| 3 | `bug-08f9b3` | **Do this second, right after `--init`.** `harness` and `reporter` cannot revise their own output. Already fixed in the LIVE config during the review (`harness: []`, `reporter: ['src/**','tests/**']`) — so the fix is proven and the work is landing it in `src/multiagents/defaults/agents.yaml`, plus correcting `tests/test_core.py:10592`, which asserts `"readonly_paths" not in harness` and so encodes the bug. **Send that last part to `tester`**, which is already exempt and for which a wrong contract is its own remit. |
| 4 | `bug-ad011c` | Steering a fallback-routed run rebuilds the command with the *preferred* provider's model and effort flag, killing the run. |
| 5 | `bug-97a0c7` | The documented recovery for a truncated run (`steer_agent`) fails with `Cwd must be an absolute path`, turning a truncated run into a permanent loss. |
| 6 | `bug-8195f2` | `wait_for_agents` reports a live, being-resumed agent as terminally `cancelled: "stopped by parent"`, because `steer()` reuses the user-facing `stop()` path. |
| 7 | `bug-e1cb10` | `budget_status` omits claude's short rolling window, reporting `severity: normal` and "all providers have headroom" while the quota guard sees minutes to empty. |
| 8 | **`F100`** | `budget._cache` keyed only by provider name; tests bleed state. The one live red test. Scheduled here because it is what makes the suite trustworthy for everyone after it. |

**Close each ticket with `resolve_ticket` when its fix merges.** A ticket whose
fix landed and which still reads `awaiting_user` will be refiled by the next
review.

---

## Phase 2 — the three rewrites

Accepted by the user, 2026-09-17, as rewrites rather than fixes. They are the
expensive decisions and they were taken deliberately rather than sliding past
inside a list.

All three live in `write_proxy_config`. Ten of the review's findings come from
one line: `host.replace(".", r"\.")` as the entire escaping strategy.

**1. Escape every ERE metacharacter** — `F13`, and with it `F1`, `F12`, `F14`.

Today only the literal dot is escaped. POSIX ERE alternation has the lowest
precedence of any operator, so an entry `evil.com|.*` produces a pattern
matching any host — `F1`, and it is critical. Unbalanced `(` or `[` produce an
invalid line that crashes tinyproxy's evaluation on every request — `F13`.

One change to a complete escape resolves all four. **Verify each id explicitly
against its own reproduction; do not assume three went green because the fourth
did.**

**2. Make the proxy suite load-bearing** — `F50`, and with it `F51`–`F54`.

`F50` is the review's most consequential finding: deleting one line,
`FilterDefaultDeny Yes`, inverts the proxy from allow-list to open relay, and
**all 48 characterization tests still pass**. The suite validates pattern
generation and never validates the directives that decide what those patterns
mean.

Rewrite the function and its suite together. The tests must assert
`FilterDefaultDeny Yes`, `FilterType ere`, `FilterCaseSensitive Off`,
`FilterURLs Off` and the `FilterFile` path are each present and correct.
`F51`–`F54` are the same work: each is a directive mutation nothing catches.

**3. Reject bare generic suffixes** — `F10`.

An entry like `com` acts as a wildcard across unrelated hosts, because the regex
anchors on `(^|\.)` and accepts any prefix. Separate from item 1 on purpose:
escaping metacharacters does not make `com` safe — the defect is the anchor, not
the escaping. Either validate each entry as a plausible hostname, or require a
dot-separated structure.

---

## Phase 3 — the rest of the six the report led with

**1. The cache that defends its own defect** — `F150`, `F122`, `F154`.

`F150` first, and as the opening move rather than a surprise:
`test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`
asserts the broken behaviour under a name that reads like a deliberate
invariant. Whoever fixes `F122` without knowing this sees a red test and
reverts. Delete or invert it, then fix `F122`, then pin `F154` — the correct
fresh-read merge is itself untested, so a fix matched against it leaves the
thing it was matched against undefended.

`F50` and `F150` together are the review's real lesson: a characterization suite
can fail in both directions, by not noticing a defect and by protecting one.

**2. Unreachable multi-account quota** — `F120`, critical and cheap.

`read_claude(config_dir=...)` exists to read per-account credential files, but
the builtin branch calls `builtin()` with no arguments and silently discards the
caller's `config_dir`. The `builtin is read_claude` identity check that causes it
is the same wart the review-phase brief flagged as where the fallback table leaks
into the dispatcher.

**3. `build_env` hands out the ambient environment** — `F112`, with `F33`.

`scripts.build_env` copies `dict(os.environ)` into every provider script call,
so a project-local script — which wins by precedence — receives the calling
process's live credentials. `F33` is the same shape in `base.build_env`: the
`blocked` list guards only the passthrough loop, so `BASE_ENV_KEYS` are
forwarded even when explicitly blocked.

**Treat `F33` at `F73`'s severity (medium/security), not its own
(low/correctness).** The review recorded that disagreement deliberately rather
than resolving it; forwarding a variable the configuration blocked is the
security reading, and that is the one to act on.

**4. Presence is not validity** — `F130`, `F131`, `F132`, `F140`.

All three shipped provider scripts and the Python `_claude_token` treat "the
file exists and I could not read a clock from it" as "authenticated". The runner
finds out when the agent's first turn 401s. One pattern, four sites, one change.

**`F131` overrides its author's proposed `accept`**, and that answers the report's
open question. The author's reasoning is that agy's token format may carry no
expiry — a claim about the format, so establish what the token actually contains.
If it genuinely has no expiry, the honest result is `cannot verify`, not
`authenticated`.

---

## Phase 4 — amending a filed bug report

**Requested by the user on 2026-09-22.** Not a defect in what multiagents does;
a capability it lacks.

Today `submit_ticket` and `resolve_ticket` are the only things that can touch a
ticket once a `bug-reporter` has filed it, and neither changes its content.
When a filed ticket turns out to contain an error — a wrong figure, a claim
that does not survive checking — there is no way to correct it in place.

**Steering the `bug-reporter` does not do it.** Measured, twice, on
2026-09-22: a steer asking for a correction produced a **second ticket**
rather than an amended one. Five tickets existed for three defects until the
duplicates were declined by hand, and declining leaves the wrong version in
the record with a note pointing elsewhere. The two survivors were `bug-b1c130`
and `bug-2138e6`; the discarded ones `bug-2a0af0` and `bug-087fee`.

That matters because the orchestrator is the one reader who checks a ticket
before it is published, and finding an error is the expected outcome of
checking rather than an exception. A review step whose only remedy is "file it
again" is not a review step.

**What is wanted:** an orchestrator can revise a filed ticket's body, title,
severity or proposed fix, keeping its id and its filing time, so that the
version the user sends is the corrected one and the history shows it was
corrected. Whether that is a new tool, an argument to `submit_ticket`, or a
`bug-reporter` mode that targets an existing id is the contract's question,
not this note's.

**Where to look:** `submit_ticket`, `resolve_ticket` and `list_tickets` in
`src/multiagents/server.py`, and whatever holds the ticket store underneath
them.

---

## What is NOT scheduled

**54 findings remain `open`. That is honest: nobody has decided about them yet**
— not "rejected", and not "unimportant". Do not treat the ledger's silence as
permission to skip them, and do not pick them up opportunistically either.

`list_findings` is the index. `read_finding(F12)` gets one. **Never open a
findings file to browse**: each holds every finding for a whole context, and
reading it to answer a question about one loads all of them.

Two are already `accepted` with reasons recorded — `F71` and `F73`, both
duplicates (of `F24` and `F33`). The report's "Defects in the review itself"
section resolves `F74` (withdrawn, never existed as a distinct defect) and
`F134` (a skipped number, nothing retracted).

---

## What the review did not cover

Not a criticism of the review — a statement of what is unknown, so nobody reads
a clean ledger as a clean codebase.

| context | state |
|---|---|
| C1 sandbox & egress | full review — 38 findings |
| C2 provider seam | full review; adversary hit `budget.py` only, 70 of 188 tests. **The seam and auth surfaces have never been attacked.** |
| C3 agent lifecycle | harness merged and proven, 7 green proof tests. **No characterization, no audit, no adversary, no findings.** `runner.py` is 61% covered with 435 uncovered statements — the largest block of untested logic in the codebase. |
| C4–C7 | mapped and ranked, never started. C5 (MCP server) has the lowest coverage at 36%. |

Both gaps were caused by `bug-565863`, not by the work being too large. Fixing it
in phase 1 is what makes finishing the review affordable.

---

## Housekeeping

Eleven agent worktrees from the review are still on disk with unmerged branches
(`git worktree list`). One is named in the report: `agents/reporter/15f9e7`
carries a REPORT.md amendment reverted by `bug-08f9b3`. **Check it before
deleting anything** — the rest are spent, but that one holds work.

**Nothing prunes the per-run provider state, and it grows without bound.**
multiagents starts a fresh conversation for every agent run, and agy keeps each
one as a directory under `antigravity-cli/brain/`. Measured 2026-09-22:

- host: 262 MB over 142 conversations, one of them 174 MB on its own
- container: 60 MB over 90 conversations, inside
  `~/.multiagents/container-state/shared/agy/`, which is **our** directory
- `~/.multiagents/container-state/` in total: 227 MB

Three weeks, two projects. Nothing deletes any of it, and a run that loops —
which is what R2 and R3 are about — writes the most.

agy ships no pruning of its own; the `/cleanup` skill that looks like it does is
a user-installed helper for the `/resume` menu (see R8d) and is not reachable
from a headless run. So the container side is ours to handle: a retention rule
on `container-state`, applied by age or by count, with the run's own
`.multiagents/runs/<id>/` left alone — that is the evidence a ticket or a
finding cites, and it is small.

Low severity, stated so it is a decision and not a surprise when a disk fills.

---

## Where things are

- `context/review/REPORT.md` — the review. **The index; read it first.**
- `context/review/MAP.md` — the seven contexts, ranked with measurements.
- `context/review/ledger.yaml` — finding state. Prefer `list_findings`.
- `context/review/C4-runtime-observed.md` — F200–F204, found on 2026-09-22 by
  measuring this project's own runs rather than by reading it. Phase 0 works
  from these.
- `context/review/BRIEF-review-phase.md` — the review phase's brief. The two
  invariants in it still hold.
- `docs/open-questions.md` — what this project believes, with the evidence and
  how to check it. **Read the entries for your area before filing anything**;
  several were wrong the first time and the corrections are recorded in place.
- `README.md` — 153 KB of reference manual. Send `researcher` at it.

## This project does not specify before it builds

No `context/specs/`. Requirements are expressed as tests, as findings, and as
`docs/open-questions.md`. The interface contract for each phase is the
orchestrator's to write.

## Progress, 2026-09-23 ~20:05 (handoff)

Order unchanged (items 1–7 of the ~17:30 handoff). Tickets are fixed here,
not sent upstream.

- **Item 1, bug-2cebea:** contract `context/specs/stuck-lifecycle.md`
  (SL-R1–R7, commits bd6c903 and 1285b22). Tests `tests/test_stuck_lifecycle.py`
  are merged (42cc404): 31 tests, 22 red. Implementer **ag-698a01** is running
  (the first attempt, ag-3b4d7a, died at the claude quota with nothing done and
  was discarded). Next: adversary (the status machine), then reviewer, then
  merge, then `resolve_ticket(bug-2cebea, fixed)`.
- **Item 2, test isolation:** tester **ag-d88a82** holds 47602d9 (an autouse
  conftest fixture that clears MULTIAGENTS_*/CLAUDE_*). It was steered to
  verify both ways. Merge once the counts match.
  Note: the 72 failures in `test_phase2_entry_semantics.py` are the paused
  phase 2, red by design, not a regression.
- **Item 3, tooling defect 6:** root cause found (ag-4faa57). Claude subagents
  are spawned with `--strict-mcp-config` and no config, and opencode subagents
  get no server either, so no claude agent with can_spawn has `consult`.
  Contract `context/specs/subagent-mcp.md` (SM-R1–R5, 3554520). Next: tester,
  then **implementer-deep** (container: no `uv` inside; see the spec's facts),
  then a live check by the orchestrator.
- **Item 5, bug-c050b0:** contract `context/specs/burn-rate-baseline.md`
  (BR-R1–R4, 37ace75). Next: tester, then implementer-quick or implementer.
- Researcher experiment: ag-4faa57 is the first run on the new brief (353k
  uncached, 1.76M cache reads, 195 s, a correct answer). See memory.
- Budget at 20:00: the claude session window has reset, weekly is at 69 %.
  opencode is at its monthly cap until Oct 05. agy is available.
- ~20:30: tester ag-d88a82 ended its turn with two background suite runs still
  in flight. Item 6 therefore also happens on claude, not only on agy. It was
  resumed with steer_agent, told to run in the foreground and not to stop
  before it has the totals. Orchestrator brief rule added (8a6a834): resume an
  interrupted agent with steer_agent, never discard and restart from zero.
- Add to the bug-c050b0 work: the wrap-up message was sent **6 times** to
  ag-3b4d7a (runs/ag-3b4d7a/prompt.1-6.md). The config says once. Each resend
  interrupted its turn, so it never started writing.

## Progress, 2026-09-23 ~21:40 (handoff; claude 5h window at 95 %, resets 01:00 CEST)

- **Item 1, bug-2cebea:** implemented and merged (904e7f0; watchdog-test fix
  d06bd8d). All 31 SL tests pass. Still open:
  - adversary ag-c0f64f (agy) was steered to finish its findings
    (`tests/test_stuck_lifecycle_adversary.py`);
  - reviewer ag-98e037 rejected with 4 points. My triage:
    - (1) the retry takes over the original `done` Event after
      `await _launch`: plausible, cheap to fix;
    - (2) `wait_for_agents`'s `classify()` ignores liveness, contrary to
      SL-R4: real;
    - (3) `_maybe_clear_stuck` calls `tree.get` per event after an external
      status change: minor;
    - (4) `suppress(Exception)` predates this work: declined.
  - **Next:** route adversary findings plus reviewer points 1-3 to an
    implementer (claude, after 01:00), then `resolve_ticket(bug-2cebea,
    fixed)`. The running MCP server still has the old code (the stuck
    labels, and wait returning at once on stuck agents) until
    `multiagents run` is restarted.
- **Item 2, test isolation:** merged (4d80cdf). Verified by the
  orchestrator: `tests/test_core.py` 560 passed with 32 ambient variables
  set, using `.venv/bin/python`. The system python lacks `mcp`.
- **Item 3, defect 6:**
  - tests merged (2e87c4b, 49); decisions recorded (9882b22);
  - implementer-deep **ag-009901** got a legitimate wrap-up at 21:33 and
    stopped with everything committed (433600c): 49/49 SM tests pass, and
    the full suite is NOT yet run (its test_core run was killed, exit 137);
  - **resume it with steer_agent after 01:00**: full suite, then its
    result;
  - then reviewer and adversary (it touches executors and docker), then
    merge;
  - then the live SM-R1 check (steps in its handoff: `collect_agent
    ag-009901`).
- **Item 5, bug-c050b0:** BR-R5 added (c26169b); tests merged (e1d7139,
  39 cases, 33 red). The cause of the 6 resends is confirmed: the flag
  lives on the Run, and steer replaces the Run. **Next:** implementer.
- **Item 8** added (7d7b6a2): agents survive a restart of the
  orchestrator's CLI.
- Local config: `compact_at_tokens: 200000`,
  `context_wind_down_tokens: 300000` (user's request). R8f compaction
  becomes active only after `multiagents run` is restarted (the current
  driver dates from 10:45).
- **~21:50:** adversary ag-c0f64f on bug-2cebea, merged as red tests
  (990fc28, `tests/test_stuck_lifecycle_adversary.py`, 7 red). Findings, worst
  first:
  1. `consult()` loses the reply after a free retry: `_consult_turn` reads
     the dead Run's text;
  2. `wait_for_any` misses a second trip after a clear, because
     `baseline_stuck` is static;
  3. an opaque tool never clears `stuck`, since `last_digest` is unchanged;
  4. the trip reason leaks into `running`/`done`: `set_status` ignores an
     empty reason;
  5. a `stuck` node with `pid=None` holds a slot for ever;
  6. `wait_for_any` hangs on a dead stuck process (the same as reviewer
     point 2);
  7. the `_preflight` `max_children` check counts dead stuck children.
  **Next (after 01:00):** one implementer on these 7 plus reviewer points 1
  and 3, `verifies=ag-698a01`. Done means both SL test files are green.
- **The tree is idle at ~21:50.** A good moment for the user to restart
  `multiagents run`: the server picks up the stuck fix, and R8f compaction
  becomes active.

## Progress, 2026-09-24 ~02:30

- The user restarted `multiagents run` at 21:43; the server now has the stuck fix
  and R8f. User: pace claude on the 5 h window only, ignore the weekly one.
- **Item 3 (defect 6):** implementation merged as 2a39b13 (implementer-deep
  ag-009901). SM 49/49 pass, and the full suite shows only the 72 phase-2
  failures. Adversary ag-da2c22's red tests are merged as e2585a9
  (`tests/test_subagent_mcp_adversary.py`). Findings:
  1. unquoted pid_file/`$@` in `_start_inside` (shell injection);
  2. `DockerExecutor.inside()` trusts `MULTIAGENTS_CONTAINER` alone, so a
     host process with that variable spawns agents outside docker (no proxy,
     no cgroups);
  3. `_hand_server` writes `runs/<id>/mcp.json` through a pre-planted
     symlink into the user's config;
  4. `provider.env` can override `MULTIAGENTS_AGENT_ID`, and the server env
     does not pin it to node_id;
  5. a leftover agy `mcp_config.json` in `homes/<id>` reaches a later
     `can_spawn:false` run;
  6. `OPENCODE_CONFIG` passthrough gives a `can_spawn:false` agent a config;
  7. an unresolvable server command emits no `mcp_unavailable` on
     opencode/agy.

  Reviewer ag-9eacdb (rejected, 2):
  - (1) `stale_mounts` uses `server_mounts([])`, so an explicit mount of an
    install path causes an endless rebuild (`docker.py` ~741);
  - (2) OSError in `_hand_server` crashes the run, contrary to SM-R5;
  - (3) design: `_start_inside` is a hidden second executor;
  - (4) the server env is built ad hoc and has no PATH.

  **Next:** a fresh implementer-deep on all of these (ag-009901's worktree
  is gone once merged: point it at `.multiagents/runs/ag-009901/`). Done =
  `test_subagent_mcp.py` + `test_subagent_mcp_adversary.py` green. Then the
  SM-R1 live check.
- **Running:** ag-004177 (bug-2cebea fixes), ag-d5bc6d (bug-c050b0).
- **Item 6, the claude cause found (ag-d5bc6d's own narration):** Claude Code's
  Bash tool moves a command to the background after 2 minutes unless the call
  passes `timeout` (up to 600000). The agent then waits for a completion
  notice that never comes in `-p` mode, and ends its turn. **The fix is
  guidance** in the claude seam (P0 R3 provider-specific prompt guidance):
  long commands take `timeout: 600000` and are split to fit within 10 min;
  never end a turn with a background job pending. agy's equivalent is
  `manage_task` polling.
- **Tooling defect seen 2026-09-24 (not yet scheduled):** agy run ag-da2c22's
  result was "This request was blocked by Gemini's filters…", with 0 commits.
  It was classified `done` with an empty reason. A provider-declared pattern
  in `providers.yaml` (the plugin seam) should classify it as `failed` with
  reason `content_filter`. Then the orchestrator is not told "done" for work
  that never happened. The adversary's calling contract now covers the wording
  side (6d17362).
