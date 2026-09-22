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

These three come from `context/review/C4-runtime-observed.md`. None was
reachable by reading the code — each needed the logs of real runs — and they
are one failure arriving in three steps, which is why they are one phase.

**Before any agent can be launched at all**, a human runs, once:

```
multiagents docker rm && multiagents docker up
```

The container was created against a claude CLI version that no longer exists
and refuses every spawn. Nothing is running (checked: the newest result-less
run is hours old), so this costs nothing today. It is a manual step because
recreating a container kills whatever is inside it, and that is not a decision
an agent gets to make.

### R1 — the versioned mount (`F200`)

`src/multiagents/executor/docker.py:340-352` mounts both a provider's launcher
and the path its symlink resolves to. The second is resolved at container
creation, which pins one version number into a mount list that is fixed for the
container's life. The claude CLI updates itself; the next update bricks the
project until someone does the manual step above, killing every in-flight agent
with it.

Mount the versions *directory* rather than the version. The launcher symlink
still needs its own mount — the existing comment says why and is correct. **Do
not special-case claude by name**: any provider whose launcher resolves into a
versioned directory has the same exposure, and naming one in the executor is the
hardcode this project exists to avoid.

Doing this first means the manual step above is the last time anyone does it.

### R2 — re-arm the watchdog (`F201`)

`src/multiagents/supervisor.py:199-203`. `_trip` latches for the whole run, so
the first alert is the only alert — including for a *different* condition
firing later. Measured: `ag-179bc2` tripped correctly at 5 identical
`view_file` calls and then ran to 116 events in silence.

Two requirements, both to be stated in the contract rather than assumed:

- a condition that differs from the one already reported is reported (a doom
  loop followed by a wall-clock timeout is two facts, not one);
- a repeat of the same condition re-arms after a further N repeats, with N
  configurable and sitting beside `doom_loop_repeats`.

**This will make runs noisier.** That is the point. It is scheduled before R3
because it is provider-agnostic, and because it is the only thing that will
tell us whether R3's compensation actually worked.

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

## Phase 1 — repair the tool, before using it

**This is the whole first phase and nothing else starts until it lands.**

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
