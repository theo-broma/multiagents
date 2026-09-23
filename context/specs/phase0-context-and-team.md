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
| G | P0-R8c, P0-R8d, P0-R8e | `driver.py` (`_supervise`), `defaults/providers/*.sh`, `defaults/providers/README.md`, `defaults/providers.yaml`, `providers.py` only if R8e needs it |

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
  (ignored by `_select`). Putting the terminal into raw mode and restoring it
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
