# Phase 0, contract B — choosing the team, and the orchestrator's context (BRIEF R7, R8)

Source: `BRIEF.md` § "Phase 0", items R7 and R8. Contract A
(`context/specs/phase0-runtime-repairs.md`, P0-R1–R6) is merged.

**Ids.** Every id carries the prefix `P0-`. Never renumber; retire with
`P0-Rn.m — withdrawn: <why>`.

**The invariant** (same as contract A): providers are plugins. No provider name
(`claude`, `agy`, `opencode`), no provider CLI flag, and no provider transcript
vocabulary (`compact_boundary`, `compactMetadata`, `/compact`) may appear in
`src/multiagents/*.py` or `src/multiagents/executor/*.py` as a result of this
work. Provider-specific facts live in `providers.yaml` and the provider scripts.

Work groups, touching disjoint files:

| group | items | files it will touch |
|---|---|---|
| E | P0-R7 | `cli.py` (`cmd_init_agent`, a new `_set_team`, the selector, the `init-agent` parser), `defaults/agents/team/_initializer.md` |
| F | P0-R8a, P0-R8b | `server.py` / `runner.py` (the notice and the reading), `defaults/project.yaml` (`limits`), `defaults/agents/team/_orchestrator.md` |
| G | P0-R8c, P0-R8d, P0-R8e, P0-R8f | `driver.py` (`_supervise`), `defaults/providers/*.sh`, `defaults/providers/README.md`, `defaults/providers.yaml`, `providers.py` only if R8e needs it |

F and G share the context reading (P0-R8a.2). F owns it; G calls it. Build F
before G, or build G against the signature below.

---

## P0-R7 — `init-agent` asks which team, then launches

Today `cmd_init_agent` (`cli.py:69`) never mentions the team, so the
initializer shapes the project under whatever `team:` was left from last time.

**P0-R7.1 — the choice comes before the launch.** On an interactive terminal
(`sys.stdin.isatty()` true — the same guard as every other prompt in `cli.py`),
with two or more teams configured and no `--team` given, `init-agent` shows the
list of teams before it runs its executor checks and before it launches
anything. Each line shows the team's name and its `description:`. The entry
equal to the configured `team:` is the one selected on entry.
*Verified by:* a test driving `cmd_init_agent` with a fake tty and a scripted
key sequence, asserting every configured team name and its description appear
in the output, and that the launch function was not called before the choice
was made.

**P0-R7.2 — the list is read from config, never hardcoded.** The options are
exactly the keys of `config.teams`, in the order they appear there. A third team
added in the project layer appears with no code change.
*Verified by:* a test that adds `teams.audit3: {description: "..."}` to a
project config and asserts it is offered and selectable. And: no string literal
`"implement"` or `"review"` is introduced in `cli.py` by this work.

**P0-R7.3 — the selector is a testable function.** The cursor UI is built on
a function with this contract (the name is binding, the internals are not):

```python
def _select(options: list[tuple[str, str]], current: str,
            read_key: Callable[[], str]) -> str | None
```

`options` is `(name, description)` pairs; `current` is the name selected on
entry (the first option if `current` is not among them). `read_key()` returns
one of the tokens `"up"`, `"down"`, `"enter"`, `"cancel"`, or any other string,
which is ignored. `"up"`/`"down"` move the selection and stop at the ends (no
wrap-around). `"enter"` returns the selected name. `"cancel"` returns `None`.
The real key reader maps arrow keys and `k`/`j` to up/down, Enter to enter,
and Escape, `q` and Ctrl-C to cancel; it restores the terminal mode on every
exit path, including an exception.
*Verified by:* unit tests on `_select` with scripted `read_key` sequences:
enter on entry returns `current`; down, enter returns the next; up at the top
stays at the top; down past the end stays at the end; cancel returns `None`;
unknown tokens are ignored; `current` not among options selects the first.

**P0-R7.4 — cancelling changes nothing and launches nothing.** A `None` from
the selector makes `init-agent` print one line saying nothing was changed and
return exit code `130`, with `project.yaml` byte-identical and no launch.
*Verified by:* a test cancelling, asserting return code 130, the file's bytes
unchanged, and the launch function never called.

**P0-R7.5 — choosing the team already in force writes nothing.** The project's
`project.yaml` is not opened for writing (bytes and mtime unchanged), and the
launch proceeds.
*Verified by:* a test asserting bytes and `st_mtime_ns` are unchanged and the
launch function was called once.

**P0-R7.6 — choosing another team writes it with a line edit.** A new function
beside `_set_executor`, same shape and same reason (its docstring says why):

```python
def _set_team(paths, team: str) -> bool
```

It rewrites only the top-level `team:` line of `paths.config / "project.yaml"`.
Every other byte of the file — comments, blank lines, indentation, the
`teams:` block — is preserved. A `team:` key nested under another mapping is
never touched. Returns `True` on success.
*Verified by:* a test with a heavily commented project.yaml, asserting the
diff between before and after is exactly one line, and that `load_config`
then reports the new team.

**P0-R7.7 — a project with no `team:` line gets one inserted.** A project that
inherits `team:` from the defaults layer has no line to substitute. `_set_team`
then inserts a top-level `team: <name>` line; the result loads, and the rest of
the file is byte-identical apart from the inserted line (and at most one blank
line added with it). If the file does not exist, `_set_team` returns `False`,
and `init-agent` prints why and returns exit code `2` without launching.
*Verified by:* a test on a project.yaml with no `team:` key (including one
whose last line has no trailing newline), and a test with no project.yaml.

**P0-R7.8 — no terminal, no prompt.** With stdin not a tty, `init-agent` shows
no list, writes nothing, prints one line naming the team in force (e.g.
`team         implement (configured)`), and carries on exactly as before.
A closed stdin must never let a script change a project's setup.
*Verified by:* a test with stdin not a tty asserting no key is read, the file
is unchanged, the team name is printed, and the launch function is called.

**P0-R7.9 — `--team <name>` skips the prompt.** `multiagents init-agent --team
NAME` never shows the list, even on a tty. A known name behaves as R7.5/R7.6
(written only if different), then launches. An unknown name prints the list of
real team names to stderr, returns exit code `2`, writes nothing and launches
nothing — it never falls back to a default.
*Verified by:* tests for a known-same, known-different and unknown name, the
latter asserting every configured team name appears in stderr.

**P0-R7.10 — fewer than two teams, no prompt.** With zero or one team
configured there is nothing to choose: no list, no write, the team in force is
printed as in R7.8, and the launch proceeds. `--team` with a name not in an
empty `teams:` is an unknown name (R7.9).
*Verified by:* a test with a single-team config on a fake tty asserting no key
is read.

**P0-R7.11 — the team shown after the choice is the one in force.** Whatever
`init-agent` prints about the team after the choice names the team just chosen,
not the one loaded before the write.
*Verified by:* covered by the R7.6 test asserting the printed line.

**Amendments, 2026-09-23, from the tester's read (ag-c06522):**

- **P0-R7.13 — the key reader is named and testable.**
  `_read_key(stream) -> str` reads one key from a binary or text stream and
  returns a token: `ESC [ A` and `k` → `"up"`; `ESC [ B` and `j` → `"down"`;
  `\r` and `\n` → `"enter"`; `q`, `\x03` (Ctrl-C), and an `ESC` that is not
  followed by `[` (including `ESC` then end of stream) → `"cancel"`; end of
  stream with nothing read → `"cancel"`; anything else → that character
  (ignored by `_select`). Every read consumes exactly one whole key: an
  `ESC [` sequence other than `A`/`B` is consumed through its final byte
  (`@`–`~`) and returns `""`; after a lone `ESC` the next character is
  consumed with it. Nothing left over may register as the next key.
  Putting the terminal into raw mode and restoring it
  is the caller's job, done in a `try/finally`; that restoration is verified
  by review, not by a test.
  *Verified by:* unit tests feeding `io.BytesIO`/`io.StringIO` streams.
- **R7.8 vs R7.9, decided:** `--team` is an explicit, deliberate choice and
  applies with or without a tty — a script that passes `--team review` means
  it. R7.8's rule is about the *absence* of a choice: with no tty and no
  `--team`, nothing is written.
  *Verified by:* a test with no tty and `--team <other>` asserting the one-line
  write and the launch.
- **The printed team line** starts with `team`, then whitespace, then the team
  name; what follows is free. The cancel line must contain "nothing" and
  "changed" (case-insensitive).
- **Cancelling skips everything after the choice**, including the executor
  checks: the choice is the first thing `init-agent` does after reporting the
  brief/context state (R7.1).
- **P0-R7.14 — the launch sees the team just chosen.** After a write, the
  config passed to the launch is reloaded (or otherwise reflects the new
  `team:`); nothing downstream of the choice sees the old team.
  *Verified by:* a test asserting the fake launch receives a config whose
  `team` is the chosen one.
- **Edge values:** an inline comment on the `team:` line is preserved after
  the new value (`team: review  # why`); a quoted value is replaced by the bare
  name; `_select` with an empty option list returns `None` without reading a
  key; `--team ""` is an unknown name (R7.9).

**P0-R7.12 — the initializer's brief is reconciled.**
`defaults/agents/team/_initializer.md` (around lines 94–95) no longer presents
hand-editing `team:` as the only route. It says the user picks the team when
they start `init-agent` (or with `init-agent --team`), and that what the
initializer proposes is the team for the **next** phase, to be put to the user
rather than edited by the initializer itself.
*Verified by:* a test asserting the file mentions `init-agent --team` and the
word "next" in the team paragraph, and the orchestrator's final read of the
paragraph.

---

## P0-R8 — the orchestrator and its own context window

### What exists and what does not

- `transcripts.context_tokens(usage)` computes the tokens a request carried.
- A provider declares where its transcripts live (`transcript.dir`/`glob` in
  `providers.yaml`); today only claude does. opencode and agy do not, and this
  contract does not add a reading for them.
- `MULTIAGENTS_SESSION_ID` is in the environment of a launched role's CLI and
  therefore of the MCP server it starts (`runner.py:~305`).
- Interactively the CLI holds the terminal and the session for the whole
  conversation: **there is no gap between turns that multiagents controls**, so
  the driver cannot compact an interactive session. Only `--unattended`
  (`driver._supervise`) has such a gap. Interactively, the protection is the
  wind-down notice (R8a), the durable-state discipline (R8b), and the automatic
  compaction threshold (R8e).

### P0-R8a — a wind-down for the orchestrator's context

**P0-R8a.1 — two limits keys.** `defaults/project.yaml` `limits:` gains, with
a comment each saying what they are and that they are tokens, not percentages:
- `context_wind_down_tokens: 150000` — the orchestrator is told to land its work.
- `compact_at_tokens: 120000` — the unattended driver may compact at a closed
  boundary (R8c).
`0` disables either. The compaction threshold is deliberately lower: compacting
at a boundary is the planned path, and the wind-down is the warning that the
planned path did not happen.
*Verified by:* a test loading the shipped defaults and asserting both keys and
values.

**P0-R8a.2 — the reading.** One function, provider-agnostic, returning the
context size of the most recent request in a launched role's session:

```python
def session_context(provider, cwd: Path, session_id: str) -> int | None
```

It reads the transcript the provider declares for `cwd`, prefers the file named
by `session_id` when one exists, and returns `context_tokens(usage)` of the
last request that carried usage. It returns `None` — **never `0`** — when the
provider declares no transcript, the file is missing or unreadable, or no
request carries usage. A compaction record in the transcript needs no special
handling: the next request's usage is already the post-compaction size. Where
the function lives is the developer's choice.
*Verified by:* tests on fixtures: a claude-shaped transcript returns the last
request's figure; a transcript whose last lines are a compaction record followed
by a request returns that request's (small) figure; a provider with no
`transcript` block returns `None`; an empty/missing file returns `None`.

**P0-R8a.3 — the notice.** In the MCP server of a launched role (depth 0 and
`MULTIAGENTS_SESSION_ID` set), when the reading is at or above
`context_wind_down_tokens`, the next tool response carries a
`context_wind_down` key, the same way `config_reload` is attached today. Its
value is a dict with `tokens`, `threshold`, and `instruction` — a text adapted
from `WRAP_UP`: finish the merge or decision in hand, record every status and
judgement in its durable home (the list in R8b), write the handoff into
`BRIEF.md`, start no new agent. It also emits one tree event
`context_wind_down` with `tokens` and `threshold`.
*Verified by:* a server-level test with a fixture transcript over the threshold
asserting the key on the next tool response and one event; under the
threshold, no key.

**P0-R8a.4 — once per crossing.** The notice is attached once, then not again
until the reading has dropped below the threshold (a compaction) and crossed it
again. A server restart may repeat it once; nothing else may.
*Verified by:* a test making three tool calls over the threshold asserting
exactly one notice; then a reading under the threshold, then over again,
asserting a second.

**P0-R8a.5 — no reading is not plenty of room.** `budget_status` gains a
`context` block for the calling session: `{"known": bool, "tokens": int|null,
"wind_down_at": int, "compact_at": int}`. With no reading, `known` is `false`
and `tokens` is `null`; no notice is ever attached on the strength of a missing
reading, and no code path treats `None` as `0`.
*Verified by:* a test with a provider declaring no transcript asserting
`known: false`, `tokens: null`, and no notice.

**P0-R8a.6 — subagents are not affected.** A server whose depth is not 0, or
with no `MULTIAGENTS_SESSION_ID`, never reads a transcript for this and never
attaches the notice. (A subagent's own context is not the orchestrator's
problem to sense here.)
*Verified by:* a test with `MULTIAGENTS_DEPTH=1` over the threshold asserting
no notice and no read.

**P0-R8a.7 — the reading is cheap.** The transcript is not re-parsed from the
start on every tool call: a reading is reused while the file's (mtime, size)
is unchanged, and when it has grown, at most the new tail is read. A 50 MB
transcript must not add more than 100 ms to a tool call that finds it
unchanged.
*Verified by:* a test with a large generated transcript timing two consecutive
tool calls, and asserting (by instrumentation or a counter) the second call
did not re-read the file.

### P0-R8b — name what does not survive

**P0-R8b.1 — the orchestrator's brief says it.** `defaults/agents/team/_orchestrator.md`
gains a section headed `## Your own context window` that states:
- Durable, and free after a compaction: `BRIEF.md`, the finding ledger
  (`list_findings`), tickets (`list_tickets`), the tree (`agent_tree`),
  branches and commits, specs under `context/`.
- Not durable: which agents it is waiting on and why; the reasoning behind a
  merge decided but not made; a finding judged but not yet given a status; a
  question it meant to ask the user.
- The rule: **record it when you decide it, not when you are about to lose
  it.**
- What to do on a `context_wind_down` notice (R8a.3).
- Interactively: at a closed work boundary with everything on disk, tell the
  user it is a good moment to `/compact` — once, in one line, never mid-task.
- Unattended: the driver compacts at closed boundaries by itself (R8c), so a
  turn should end with its state on disk.
*Verified by:* a test composing the implement team's orchestrator prompt and
asserting the heading and the names `BRIEF.md`, `list_findings`,
`list_tickets`, `agent_tree` and `context_wind_down` appear under it; the
orchestrator's final read.

### P0-R8c — unattended compaction at a closed boundary

**P0-R8c.1 — when.** In `driver._supervise`, after a turn that exited `0` and
was not stopped by a limit, the driver compacts the session if and only if all
of these hold:
1. `compact_at_tokens` is not `0`;
2. the reading (R8a.2) for this role's session is not `None` and is at or above
   `compact_at_tokens`;
3. the tree is idle: no node other than the driver roles has a status in the
   tree's `ACTIVE` set, and there is no deferred task queued;
4. the provider has not already answered "unsupported" in this driver run
   (R8c.3).
Never after a failed turn, a limited turn, or with a live agent in the tree —
a turn that ended with agents running ended with reasoning in the
orchestrator's head that is not on disk.
*Verified by:* table tests over the four conditions plus exit code and limit,
using a fake provider script, asserting the `compact` action is invoked exactly
when all hold.

**P0-R8c.2 — how.** The driver invokes the provider script's `compact` action
through `scripts.run_action`, with `MULTIAGENTS_SESSION_ID` (and the rest of
the launch context) in `extra_env`, from the same working directory as the
launch, with a timeout of `limits.compact_timeout_seconds` (default `180`,
added to `defaults/project.yaml`). The driver learns only the exit code and
the script's output; it contains no provider name and no transcript vocabulary.
*Verified by:* a test with a fake script recording its argv, cwd and
environment; and the invariant check (no forbidden strings in
`src/multiagents/*.py`).

**P0-R8c.3 — what the exit code means.**
- `0` — compacted. The driver prints one line starting `compacted` followed by
  the script's first stdout line, and emits a tree event `compacted` carrying
  `tokens_before` (its own reading) and `detail` (that stdout line).
- `64` — this provider cannot compact. The driver emits `compact_unsupported`
  once and does not invoke `compact` again for the rest of this driver run.
- anything else, including a timeout — failed. The driver prints one line
  saying so with the tail of stderr, emits `compact_failed` with `code` and
  `detail`, and **continues the loop**: a failed compaction is not a failed
  turn, does not count toward the three-failures stop, and is not retried
  until the next turn that meets R8c.1.
*Verified by:* tests with fake scripts exiting 0, 64, 1 and sleeping past the
timeout, asserting the events, the printed line, that the loop runs its next
turn, and that after a 64 the action is not invoked again.

**P0-R8c.4 — a compaction is not activity.** The idle-turn count (two turns
that change nothing stop the run) is computed exactly as today; the events a
compaction emits must not make an idle turn look productive, nor a productive
turn look idle.
*Verified by:* a test where two idle turns each followed by a compaction still
stop the run after the second.

**P0-R8c.5 — interactive runs are untouched.** `_run_supervised` and the exec
path never invoke `compact`.
*Verified by:* a test driving the interactive supervised path with a fake
script asserting `compact` is never invoked.

### P0-R8d — the provider scripts' `compact` action

**P0-R8d.1 — the contract.** `defaults/providers/README.md` documents a new
action alongside `check`/`login`/`budget`:

```
<provider>.sh compact   non-interactive; MULTIAGENTS_SESSION_ID names the session
                        exit 0  = compacted, and verified; stdout line 1 = figures
                        exit 64 = this provider cannot compact from outside
                        exit *  = attempted and failed; reason on stderr
```

A script that does not know the action already exits 64 through its `*)` arm;
each shipped script's usage line lists `compact`.
*Verified by:* a test running each shipped script with an unknown action and
with `compact` where applicable; a grep of the README.

**P0-R8d.2 — claude compacts, and verifies it.** `claude.sh compact` requires
`MULTIAGENTS_SESSION_ID` (missing → exit 2, reason on stderr) and a transcript
for it in the directory the launch action uses (missing → exit 1). It runs the
CLI non-interactively with `/compact` against that session (the invocation
recorded in BRIEF § R8), then reads the session transcript back and succeeds
**only** if a compaction record with a manual trigger was appended by this
call — a record that existed before the call does not count. On success it
prints `<preTokens> -> <postTokens> tokens` as its first stdout line and exits
0; a CLI exit of 0 with no new record is a failure (exit 1, stderr says no
compaction was recorded).
*Verified by:* tests with a fake `claude` binary on PATH and `HOME` pointing at
a scratch directory: (a) the fake appends a manual compaction record → exit 0
and the figures line; (b) the fake exits 0 and appends nothing → exit 1;
(c) a pre-existing compaction record and a fake that appends nothing → exit 1;
(d) no session id → exit 2; (e) the fake exits non-zero → non-zero.

**P0-R8d.3 — agy cannot, and says so cheaply.** `agy.sh compact` exits 64
without starting the CLI, with a comment recording why: print mode expands
slash commands into a model turn, and the one attempt cost 42,752 tokens and
compacted nothing.
*Verified by:* a test with a fake `agy` on PATH that records whether it was
run, asserting exit 64 and that it was not.

**P0-R8d.4 — opencode: deferred, exits 64 for now.** BRIEF § R8d names the
HTTP route (`POST /session/{id}/summarize`) as the way. It is confirmed only as
a string in the binary, it needs an `opencode serve` process this project does
not run, and opencode's monthly quota is exhausted until 2026-10-05, so it
cannot be verified end to end now. `opencode.sh compact` exits 64 with a
comment naming the route and the reason it is not wired, so the day it is
wired, nothing in Python changes. Recorded as a deferred item in BRIEF.
*Verified by:* a test asserting exit 64 without starting the CLI.

### P0-R8f — interactive compaction: stop, compact, resume (added 2026-09-23, user's request)

The user asked for compaction between turns in interactive mode too. A live
interactive CLI holds its conversation in memory, so compacting its session
from outside while it runs would fork the transcript. Instead the supervised
interactive path ends the CLI at a closed boundary, compacts, and resumes the
same session. It reuses what `_run_supervised` already does for a usage limit:
a `stalled` poll, an announced grace period, a terminate, a relaunch with
`--resume`. **This amends P0-R8c.5:** the attached path now compacts, but only
through this mechanism. The exec path (no supervising parent) never does.

**P0-R8f.1 — the probe: can this provider compact, before anything is stopped.**
The `compact` action gains a check mode. With `MULTIAGENTS_COMPACT_CHECK=1` in
its environment, a script exits `0` if a real `compact` call could succeed now
(for claude: the session id is set and its transcript exists), exits `64` if
the provider cannot compact, and exits anything else for "not now". It compacts
nothing, and it does not start the provider CLI. `agy.sh` and `opencode.sh`
keep exiting 64 unconditionally. The README documents the check mode.
*Verified by:* script tests. Claude with a transcript exits 0, and with no
session id or no transcript exits non-zero and not 64. A fake CLI records that
it was never run. agy and opencode exit 64.

**P0-R8f.2 — when it is proposed.** While the attached CLI runs,
`_run_supervised` schedules a compaction only if all of these hold:
1. `compact_at_tokens` is not 0, and the reading (R8a.2) for this role's
   session is not `None` and is at or above it;
2. the tree is idle, as in R8c.1.3;
3. the session is at rest: the session's transcript file has not changed
   (same mtime and size) for at least `limits.compact_idle_seconds`
   (default `60`, added to `defaults/project.yaml`). That means the model
   has finished its turn and nothing has been submitted since;
4. no usage-limit warning is pending (the limit path wins);
5. the probe (R8f.1) exited 0;
6. this driver run has not already disabled it (R8f.5).
*Verified by:* table tests over each condition on `_run_supervised`, with a
fake launch script (a long-sleeping child), a fixture transcript, and time
under the test's control. Each asserts the child is or is not terminated.

**P0-R8f.3 — the announcement and the grace period.** When the conditions
first hold, the driver prints one line and emits `compact_scheduled` with
`tokens`:

`compacting this session in 30s (<tokens> tokens, nothing running) — type anything to keep it`

The 30 comes from `limits.compact_grace_seconds` (default `30`, added to
`defaults/project.yaml`). If the transcript changes during the grace period
(the user submitted something, or the model is working), the compaction is
cancelled. The driver then prints one line, emits `compact_cancelled`, and
does not propose again until the transcript has changed and then come back to
rest (R8f.2.3). Text typed but not yet submitted cannot be seen; the README
of the driver behaviour and the orchestrator's brief (R8b) say so.
*Verified by:* a test where the transcript is appended during the grace
period, asserting no terminate and one `compact_cancelled`. A test where it
stays unchanged asserts the terminate happens no earlier than the grace
period.

**P0-R8f.4 — stop, compact, resume.** When the grace period passes untouched,
the driver:
1. terminates the CLI the way the limit path does;
2. restores the terminal;
3. runs the `compact` action exactly as in R8c.2 (session id, project root,
   `compact_timeout_seconds`);
4. relaunches the same session, attached, with `MULTIAGENTS_RESUME=1` and
   **no** resume prompt. The orchestrator was idle at its prompt and comes
   back idle at its prompt; nothing is sent to the model.
It prints `compacted    <figures>` before relaunching and emits `compacted`
as in R8c.3. This stop is not a crash, not a deliberate exit and not a
restart attempt: it does not end the driver, does not consume
`restart_attempts`, and does not go through the headless fallback.
*Verified by:* a test asserting the order terminate → compact → relaunch,
the relaunch environment (resume on, no prompt), that the driver keeps
running afterwards, and that `restart_attempts` is untouched.

**P0-R8f.5 — failure never loses the session, and never loops.** If the
`compact` action exits non-zero after the CLI was stopped, the driver still
relaunches the session with resume on, prints the failure line, emits
`compact_failed` (or `compact_unsupported` for 64), and disables interactive
compaction for the rest of this driver run. A session is stopped for
compaction at most once per crossing of the threshold: after a success it is
not proposed again until the reading has fallen below `compact_at_tokens` and
risen above it again.
*Verified by:* a test with a compact action exiting 1 asserting a relaunch,
the event, and no second terminate within the same run.

**P0-R8f.6 — the exec path and the headless path are unchanged.** The exec
handover (`supervise=False`) never compacts. The headless `_supervise` loop
keeps R8c; it does not use the probe or the grace period.
*Verified by:* the existing R8c tests stay green, and a test on the exec
path asserts no probe is run.

**P0-R8f.7 — the orchestrator knows.** The `## Your own context window`
section of `_orchestrator.md` (R8b.1) says that, under `multiagents run`, the
driver may stop and resume the session at a closed boundary after announcing
it, and that the orchestrator should therefore end a boundary turn with its
state on disk rather than in its reply. It no longer tells the orchestrator to
ask the user for `/compact` when that mechanism is active, only when it is not
(exec path, or a provider that cannot compact).
*Verified by:* a test on the composed prompt for the new sentence.

**Amendments to P0-R8f, 2026-09-23, from the tester's read (ag-2c808f):**
- The compaction conditions are evaluated on the existing attached poll
  (`STALL_POLL_SECONDS`), not on a new timer.
- With stdin not a tty, R8f does not apply: nobody can see the announcement
  or cancel it.
- The probe runs at most once per rest episode, i.e. once each time the
  session comes back to rest. A probe answering 64 disables R8f for the rest
  of the driver run. Any other non-zero answer only skips this episode.
- "The README of the driver behaviour" in R8f.3 is withdrawn. Only the
  orchestrator's brief (R8f.7) has to say that unsubmitted text is invisible.
- The token count in the announcement is free-form (`9000` or `9,000`). The
  cancel line must contain "cancel".

**Amendments to P0-R8f, 2026-09-23, from the advisor's review (ag-25c350),
decided with the user.** The driver cannot see keystrokes, only submitted
messages. So a user who reads a diff and then composes a long reply can have
the grace period run out while typing, and lose the unsubmitted text. These
amendments narrow that window and make the warning impossible to miss.
- **R8f.2.3, default changed:** `limits.compact_idle_seconds` now defaults to
  `300`, not `60`, in `defaults/project.yaml`. The grace period stays
  `limits.compact_grace_seconds: 30`.
- **P0-R8f.8 — the announcement rings, configurably.** The announcement line
  ends with a terminal bell (`\a`) when `limits.compact_bell` is true. It
  defaults to `true` in `defaults/project.yaml`. With `false`, no bell
  character is written.
  *Verified by:* a test with the default config asserting the announcement
  output contains `\a`, and one with `compact_bell: false` asserting it does
  not.
- **P0-R8f.3, wording changed:** the announcement must say that cancelling
  needs a **sent** message, because typing without sending is invisible. It
  replaces "type anything to keep it" with, for example,
  `compacting this session in 30s (<tokens> tokens, nothing running) — send any message (e.g. "wait") to cancel`.
  The line must contain "send" and "cancel". The number of seconds shown is
  the configured grace period, not a literal 30.
- **P0-R8f.9 — every R8f value comes from the config.** `compact_idle_seconds`,
  `compact_grace_seconds` and `compact_bell` are read from the project's
  `limits`, like `compact_at_tokens`. A project that overrides one gets its
  value, and a project that omits one gets the shipped default. A malformed
  value (not a number, negative, `inf`, not a boolean) falls back to the
  default and never crashes the driver. This is the same safe parsing the
  attack required of the existing limits (finding 5).
  *Verified by:* tests overriding each key and asserting the observable effect
  (the proposal time, the grace delay and the displayed seconds, the presence
  of the bell), and a test per key with a malformed value asserting the
  default is used.
- R8f.7's brief text also says that only a sent message cancels.
- **Decided, from the tester's read (ag-58ad0b):**
  - **Rest (R8f.2.3)** is measured from the transcript's last modification
    time (mtime), not from when the driver first noticed the file unchanged.
  - **A value of 0** for `compact_idle_seconds` or `compact_grace_seconds` is
    malformed and falls back to the default. A zero grace would stop the
    session with no warning.
  - **`compact_bell`** accepts YAML booleans only. `"false"` (a string) and
    `0` are malformed and fall back to `true`.
  - **The bell** is written on the announcement line, on the same stream as
    the announcement.
- **Decided, from the implementer's read (ag-829577):**
  - **Rest (R8f.2.3)** starts no earlier than the latest launch of the CLI,
    even if the transcript is older. A user who has just been handed a
    session is the one most likely to be typing.
  - ~~A malformed `compact_at_tokens` falls back to 0, which means off.~~
    **Superseded after the advisor's review (ag-25c350, turn 5).** A malformed
    value of any limit in this contract (`compact_at_tokens`,
    `context_wind_down_tokens`, `compact_timeout_seconds`, and the R8f keys)
    falls back to its **shipped default** in `defaults/project.yaml`, the same
    rule as R8f.9. A typo such as `120k` must not silently switch off a safety
    feature. Only an explicit `0` means off, for the keys where 0 means off.
  - **A probe answering 64** disables R8f silently, with no line and no
    event: anything printed would land on the live TUI.
  - **An announced compaction** is also cancelled if the tree stops being
    idle during the grace period.
  - **Captured provider actions** (`scripts.run_action`) run in their own
    session with stdin from `/dev/null`, and on timeout the whole process
    group is killed. This holds for every captured action, not only
    `compact`: they are non-interactive by contract, and the probe runs while
    the CLI owns the terminal.
- **Added from the review of d4ae4ec (reviewer ag-e8565d, 2026-09-23):**
  - **P0-R8f.10 — SIGTERM restores the terminal.** When the process running
    an attached session (`driver._run_attached`) or the interactive team
    picker (`cli.py`, cbreak mode) receives SIGTERM, the terminal attributes
    in force before it started are restored before the process exits, just
    as they are on SIGINT, SIGHUP or a normal return. The process still
    exits (it does not ignore SIGTERM), with a non-zero status.
    *Verified by:* a test that sends SIGTERM during an attached run and
    during the picker, and asserts that the saved terminal attributes were
    written back and the process ended.
  - **P0-R8f.11 — a captured action never outlives its caller.** If
    `scripts.run_action` is interrupted by any exception while waiting for
    the provider script (KeyboardInterrupt included), the script's whole
    process group is killed and reaped before the exception propagates. The
    exception still propagates unchanged.
    *Verified by:* a test that raises KeyboardInterrupt during a slow action
    and asserts that no process of that action's group remains.
  - **P0-R8f.12 — a user's own exit wins over a pending compaction.** If the
    attached CLI exits by itself (any exit the driver did not cause by
    stopping it for R8f.4) while a compaction is announced or has just
    become due, the driver does not compact and does not relaunch: it ends
    the session as it would with no compaction pending. Only a CLI stopped
    by the driver for R8f.4 is compacted and resumed.
    *Verified by:* a test where the CLI exits 0 at the moment the grace
    period expires, asserting no `compact` action runs and no relaunch
    happens.
  - **P0-R8f.13 — the driver's other limits parse safely too.**
    `restart_attempts`, `restart_delay_seconds` and `limit_max_waits`
    follow R8f.9's rule: a malformed value (not a number, negative, `inf`,
    NaN, a string) falls back to the shipped default in
    `defaults/project.yaml` and never crashes the driver. The same rule
    applies to `compact_at_tokens`, `context_wind_down_tokens` and
    `compact_timeout_seconds`, both in the driver and in the server
    (superseding the "malformed → off" decision above).
    *Verified by:* a test per key with a malformed value asserting the
    default's observable effect, and that an explicit `0` keeps its meaning
    where 0 means off.

- **Decided, from the tester's questions (ag-c36b39):**
  - **Explicit 0 (R8f.13).** For `restart_attempts`, `restart_delay_seconds`
    and `limit_max_waits`, 0 is a valid value, not a malformed one: no
    restart, no delay, no wait. For `compact_at_tokens` and
    `context_wind_down_tokens`, 0 means off. For `compact_timeout_seconds`,
    `compact_idle_seconds` and `compact_grace_seconds`, 0 is malformed and
    falls back to the default.
  - **SIGTERM and the child (R8f.10).** On SIGTERM, the attached driver also
    ends the CLI it is holding (its process group) before it restores the
    terminal and exits. It never leaves an orphan CLI on the terminal.
- **Added from the adversary's attack on d4ae4ec (ag-43922d):**
  - **P0-R8f.14 — a cancelled announcement can be proposed again.** After
    an announced compaction is cancelled because the tree stopped being
    idle, it is proposed again once R8f.2's conditions hold anew (idle tree,
    rest measured afresh). It is not suppressed for the rest of the session
    because the transcript did not change.
  - **P0-R8f.15 — a compaction that did not shrink is a failure.** The
    provider's `compact` action exits non-zero when the context after
    compaction is not smaller than before. R8f.5 then applies: no loop in
    the attached path, and the headless path (`_compact_if_due`) does not
    compact again on every turn.
  - **P0-R8f.16 — the transcript location follows the provider's config
    directory.** `claude.sh compact`, and its check mode, find the
    transcript where `transcripts.default_root()` does (`CLAUDE_CONFIG_DIR`
    when set), not only under `$HOME/.claude`.
  - **P0-R8f.17 — the probe answers for what compact will actually need.**
    The check mode exits non-zero when the transcript exists but cannot be
    read, so the session is never stopped for a compaction that must fail.
  *Verified by:* `tests/test_phase0_r8f_adversary.py` (12d893f).
- **Decided, from ag-adac4d's NEED_INFO:** in the headless path, R8c.3 wins.
  A compaction that fails, including one that did not shrink (R8f.15), is
  reported and tried again on the next qualifying turn. That is at most one
  attempt per turn, which is not a loop. The adversary test
  `test_adversary_compaction_succeeding_without_shrinking_causes_headless_loop`
  is withdrawn: it assumed an exit 0 without shrinking, which R8f.15 now
  makes impossible for `claude.sh`, and which the driver cannot tell apart
  from a success.

- **Added 2026-09-24, R8f leftovers (BRIEF item 4):**
  - **P0-R8f.18 — `claude.sh launch` finds the session where the CLI keeps
    it.** Wherever the `launch` action (and any other action of `claude.sh`)
    reads the CLI's session or transcript files, it follows the same rule as
    R8f.16: `CLAUDE_CONFIG_DIR` when set, `$HOME/.claude` otherwise. No action
    of the script still hard-codes `$HOME/.claude` for reading sessions.
    *Verified by:* a test running the `launch` action (or the part of it
    that locates a session) with `CLAUDE_CONFIG_DIR` pointing at a fixture
    directory and `HOME` pointing elsewhere, asserting that the fixture
    session is the one found. Plus a grep-style check that no read of
    sessions under `$HOME/.claude` remains outside the shared helper.
  - **P0-R8f.19 — a user's own exit wins over a usage-limit stop too.** The
    rule of R8f.12 applies to the usage-limit path as well. If the attached
    CLI exits by itself while the driver is about to stop it for a usage
    limit, or has just decided to, the driver ends the session as the user
    asked. It does not wait out the window and does not relaunch. Only a CLI
    the driver stopped for the limit is waited for and resumed.
    *Verified by:* a test where the CLI exits 0 at the moment the limit is
    detected, asserting that no limit wait and no relaunch happen. The
    driver-stopped case still waits and relaunches.
  - **P0-R8f.20 — the remaining driver limits parse safely.**
    `limit_wait_seconds`, `restart_min_runtime_seconds`, `supervised_turns`
    and `spend_limit_pause_hours` follow R8f.13: a malformed value (not a
    number, negative, `inf`, NaN, a string, a list) falls back to the shipped
    default and never crashes. Where 0 has a documented meaning in
    `defaults/project.yaml`, it keeps it. Where it has none, 0 is malformed.
    *Verified by:* one test per key with a malformed value, asserting the
    default's observable effect. For each key whose 0 is meaningful, a test
    that 0 keeps that meaning.

- **Decided, from the tester's questions (ag-283934):**
  - **`supervised_turns`** is added to `defaults/project.yaml`, with today's
    effective default of 50. It falls back to 50, never to 0.
  - **0** keeps the rule of R8f.20. None of the four keys documents a
    meaning for 0, so 0 is malformed for all of them, including
    `restart_min_runtime_seconds` and `limit_wait_seconds`.
  - **Numeric strings** (`"900"`) are accepted, as R8f.13's parser already
    does. "A string" in R8f.20 means a non-numeric one.
  - **`~` in `CLAUDE_CONFIG_DIR`** is out of scope.

### P0-R8e — the automatic threshold, through the plugin seam

**P0-R8e.1 — a per-agent key.** An agent entry in `agents.yaml` may carry
`autocompact:` (the CLI accepts `auto` or a token count, 100k–1M; the value is
passed through, and the CLI validates it). For claude, a spawned agent with the
key gets `--autocompact <value>` through the `spawn.optional` map in
`providers.yaml` — one line there, no Python branch. A launched role (the
orchestrator, the initializer) with the key gets the same flag from the
`launch` action, which receives the value in the environment as
`MULTIAGENTS_AUTOCOMPACT`. An agent without the key gets no flag. Providers
with no such option ignore the key.
*Verified by:* a test building a claude spawn argv with and without the key;
a test running `claude.sh launch` with and without `MULTIAGENTS_AUTOCOMPACT`
set, asserting the flag's presence and value; a test that an opencode spawn
with the key has no such flag.

**P0-R8e.2 — no shipped default.** No agent in the shipped `agents.yaml`
gains the key in this contract; whether to set one is the user's choice and is
recorded in BRIEF under "Awaiting the user".
*Verified by:* a test asserting no shipped agent entry carries `autocompact`.

---

### Amendments, 2026-09-23, from the tester's read (ag-48f51a)

- **P0-R8a.2, decided:** the reading uses the file named by `session_id` and
  nothing else. If that file does not exist the result is `None`; it never
  falls back to the newest transcript, which could be another role's session
  (`run` and `init-agent` share a project directory).
- **P0-R8a.8 — where the server's reading comes from.** The provider is the
  one of the launched role's roster entry (the role named by
  `MULTIAGENTS_ROLE`, as `driver._launched_spec` resolves it), and the
  directory is the project root. Not the process cwd, which an agent can
  change.
  *Verified by:* a test where the process cwd differs from the project root.
- **P0-R8a.5, extended:** the `context` block is present in every
  `budget_status` response; for a server that is not a launched role
  (R8a.6), it is `known: false, tokens: null`.
- **P0-R8c.1, clarified:** "a turn run by `_supervise`" includes the headless
  fallback `_run_supervised` enters when the terminal is lost; once the
  terminal is gone the run is unattended. R8c.5 is about the attached,
  interactive part only.
- **Order within a turn:** the compaction decision is made after every turn
  that qualifies, *before* the loop decides whether to stop (idle turns, the
  turn limit). So the last turn of a run can compact, and the second of two
  idle turns compacts before the run stops.
- **The compact action's cwd** is the project root, whatever mechanism carries
  it there.
- **Deferred tasks** block compaction whether or not they are already due.

- **P0-R8a.3, decided from ag-6befca's NEED_INFO:** `budget_status` neither
  carries nor consumes the `context_wind_down` notice — it already reports the
  reading in its `context` block. The notice goes on the next *other* tool
  response.

### P0-R8 live check (C1, 2026-10-03)

The user ran an interactive `/compact` of the root orchestrator (claude, host) at a closed boundary: nothing running, C16 paused on the codex quota. The check covered the user-initiated path, not the driver's unattended P0-R8c compaction.

- **Task remembered:** yes. The orchestrator resumed with the pending work intact: resume C16 ag-77e8fe after the codex reset at 15:04Z, then its reviewer, the C10 full suite, and the user actions owed.
- **Session identity kept:** yes. Same transcript (`2d371941-…jsonl`), and the tree still shows the same orchestrator session node (`03 Oct 10:30 · running`).
- **MCP works:** yes. `budget_status`, `agent_tree`, `list_questions`, `list_plans` and `list_tickets` all answered, and their state matched the pre-compaction record.
- **Observation, not a failure:** `budget_status.context` reported `known: false` after the compaction, so the wind-down reading (P0-R8a) gave no figure at that moment.

Verdict: passed.

## Silences, answered

- **Existing data.** A project.yaml without a `team:` line is R7.7; a project
  config without the new `limits` keys inherits them from the defaults layer.
  No migration.
- **Who may trigger it.** Only the unattended driver compacts; no MCP tool is
  added that lets any agent compact any session.
- **What is recorded.** `context_wind_down`, `compacted`, `compact_failed`,
  `compact_unsupported` are tree events; `init-agent` writing `team:` is
  printed.
- **Second attempt.** R8a.4 (once per crossing), R8c.3 (64 latches, a failure
  waits for the next qualifying turn), R7.5 (an unchanged team writes nothing).
- **Undo.** A compaction cannot be undone. That is why it happens only at an
  idle tree after a clean turn, and why R8b tells the orchestrator to keep its
  state on disk all the time. A team change is undone by choosing again.
- **Advisor review.** Not done at writing time: agy (the advisor's provider)
  was failing authentication in the container on 2026-09-23, and opencode is
  exhausted. The tester is the second reader; the advisor reviews the finished
  diff when agy is back.
