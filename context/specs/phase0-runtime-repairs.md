# Phase 0, contract A — runtime repairs (BRIEF R1–R6)

Source: `BRIEF.md` § "Phase 0", items R1–R6; findings `F200`, `F201`, `F202`
(`context/review/C4-runtime-observed.md`); tickets `bug-b1c130`, `bug-2138e6`,
`bug-b864b8`. R7 and R8 are a separate contract, written after this one lands.

**Ids.** R1–R23 are already cited by phases 1–3 tests, so every id here carries
the prefix `P0-`. Never renumber; retire with `P0-Rn.m — withdrawn: <why>`.

**The invariant that governs all six items** (`context/review/BRIEF-review-phase.md`):
providers are plugins. No provider name (`claude`, `agy`, `opencode`), no
provider tool name, and no provider error string may appear in
`src/multiagents/*.py` or `src/multiagents/executor/*.py` as a result of this
work. Provider-specific facts live in `providers.yaml`. Every item below has a
`Verified by:` line that includes this where it applies.

Work groups, which touch disjoint files and may be built in parallel:

| group | items | files it will touch |
|---|---|---|
| A | P0-R1 | `executor/docker.py` |
| B | P0-R2, P0-R4 | `supervisor.py`, `providers.py` (Event, parse_line), claude block of `providers.yaml`, `runner.py` (Supervisor construction, the timer loop) |
| C | P0-R3 | `providers.py` (Provider fields), `runner.py` (`compose_prompt`), agy block of `providers.yaml` |
| D | P0-R5, P0-R6 | `server.py`, `runner.py` (`wait_for_any`) |

B and C both touch `providers.py` and `runner.py` in different functions; the
merge order is B then C.

---

## P0-R1 — the container executes the CLI the host would execute today (`F200`)

Today `mount_cli_from_host` (`executor/docker.py:~340`) mounts the launcher and
`Path(binary).resolve()`. Docker resolves a symlink bind at creation, so the
container runs the version that was current when it was created, and the
resolved path pins that version into the declared mount list.

A **versioned launcher** is a provider binary found on PATH that is a symlink
whose resolved target is a file in a directory other than the launcher's own
(`~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.280`). The
directory is called the *versions directory* whatever its name, and it counts
even when it holds a single entry. The executor must not know which provider
this is. *(Amended 2026-09-22 after the group A tester asked: no sibling count,
no name test.)* Out of scope: a target nested below a per-version directory
(`versions/1.0.0/bin/x`); such a launcher keeps today's behaviour, and no test
is written for it.

- **P0-R1.1** The declared mount list is **stable across a CLI update**: computing
  it before and after the launcher symlink is retargeted from one version to
  another (both inside the same versions directory) yields the same list. No
  mount path contains the version component.
  *Verified by:* unit test on the mount computation with a temporary versions
  directory and a symlink retargeted between two calls.
- **P0-R1.2** After such a retarget, **without recreating the container**, the
  next agent spawn executes the **new** version. The container never executes a
  superseded version while the host launcher points at a newer one.
  *Verified by:* test on the exec argv / command the executor issues after the
  retarget (the path executed resolves to the new version); plus one live check
  by the orchestrator at delivery.
- **P0-R1.3** `multiagents docker up` does not refuse a container solely because
  the host CLI updated. It still refuses on any other mount mismatch (the
  `f963ba3` behaviour is kept, not weakened).
  *Verified by:* test that a retarget produces no mismatch report, and the
  existing mismatch tests stay green.
- **P0-R1.4** Nothing named after the provider remains unresolvable: inside the
  container the provider CLI is still invokable by the name the provider
  declares, so anything that execs it by name (provider scripts included) still
  works.
  *Verified by:* test that the launcher name resolves to an executable path in
  the computed container layout.
  (Why: `docker exec` launches the agent by the provider's bare command name.
  Provider *scripts* run on the host, `scripts.py:~186`, so they are not the
  reason.)
- **P0-R1.5** A launcher that is **not** a symlink, or whose target is not in a
  versions-like directory, is mounted exactly as today (byte-identical mount
  list for that provider).
  *Verified by:* characterization test pinning today's list for a plain binary.
- **P0-R1.6** The mounts added are limited to what the provider's binary
  resolves into. **Do not mount the launcher's whole directory** (`~/.local/bin`
  holds unrelated binaries, and every mount is readable by every agent in the
  container). All CLI mounts stay read-only.
  *Verified by:* test that no mount equals or contains the launcher's parent
  directory unless that directory is itself the versions directory; all CLI
  mounts are `read_only`.
- **P0-R1.7** No provider name in the executor.
  *Verified by:* `grep` test over `src/multiagents/executor/*.py` for the three
  provider names, scoped to lines added by this work (a test that fails if the
  count grows).

**Decided (amended 2026-09-22):** the executor resolves the launcher **on the
host, at each spawn**, and the command it issues to the container names that
resolved absolute path, which lies under the mounted versions directory. So
P0-R1.2 is tested on the issued command. The launcher path itself stays mounted
as today, which is what P0-R1.4 asks; the agent's own command does not go
through it.

---

## P0-R2 — the watchdog reports per condition, and re-arms only what can recur (`F201`)

Today `_trip` latches for the run and `check_timers` returns `None` once
anything tripped; the runner's timer loop also stops after the first timer trip.
So the first trip is the only one ever reported.

Trip reasons today: `doom_loop`, `runaway_steps`, `silence`, `timeout`.

- **P0-R2.1** A trip whose **reason differs** from every reason already reported
  in this run is reported (returned by `observe` / `check_timers`, and the
  runner emits a `stuck` event for it). A doom loop followed later by a
  wall-clock timeout yields two `stuck` events.
  *Verified by:* supervisor unit test: loop trip, then advance the clock past
  `wall_timeout`, `check_timers` returns a `timeout` trip; runner-level test
  that two `stuck` events are emitted.
- **P0-R2.2** `doom_loop` **re-arms**: after a `doom_loop` trip, it is reported
  again once a further `doom_loop_rearm` repeats of a looping signature are
  observed. `doom_loop_rearm` is a new key under `limits:` beside
  `doom_loop_repeats`, default **equal to `doom_loop_repeats`**, documented with
  a one-line comment in `defaults/project.yaml`. Any tool call that is not part
  of the loop, or any change of the working tree, resets the re-arm count.
  *Verified by:* unit test: 5 identical calls → trip; 4 more → nothing; 5th → a
  second trip; and a test that a different call in between resets the count.
  *Amended 2026-09-22:* for an A,B cycle the unit of repetition is **one pair**,
  the unit that tripped. A call on a **different** signature is not part of the
  loop: it resets the re-arm count, and a loop on that new signature is a fresh
  detection governed by `doom_loop_repeats`. The Supervisor takes the value as
  a constructor keyword `loop_rearm` (beside `loop_repeats`).
- **P0-R2.3** `runaway_steps` and `timeout` are **terminal**: reported at most
  once per run, however many further events arrive. (`self.steps` only grows;
  a naive re-arm trips on every later event.)
  *Verified by:* unit test: exceed `max_steps`, feed 100 more events, exactly one
  `runaway_steps` trip in total; same for `timeout` across repeated
  `check_timers` calls.
- **P0-R2.4** `silence` is **per episode**: reported once per quiet period; it
  re-arms only after a stream event has arrived (the agent spoke again). A
  single long quiet period yields one report, however many polls see it.
  *Verified by:* unit test: silence trip, further polls with no events → no new
  trip; one event, then silence again → a second trip.
- **P0-R2.5** The run's `stuck` status and reason reflect the **most recent**
  trip. The runner's timer loop keeps running after a timer trip so that later
  conditions can still be detected (it ends when the run ends, as it does
  today).
  *Verified by:* runner test that a timeout after a loop updates the reason.
- **P0-R2.7** "Terminal" means terminal **for that turn**. A turn started by
  `steer_agent` (which relaunches through `_launch`) begins with a fresh
  watchdog: every latch cleared, the wall clock and step count restarted. This
  is how an orchestrator extends a run, and the extended run is monitored.
  *Verified by:* runner test: trip `timeout`, steer, the new turn can trip
  `timeout` again.
- **P0-R2.8** A timer loop that keeps running after a trip writes nothing to
  the tree or the events log unless a new trip is reported.
  *Verified by:* test counting tree writes across idle polls after a terminal
  trip.
- **P0-R2.9** The runner's timer loop runs for the whole life of the run and
  never dies on its own exception. *(Added 2026-09-22 from the group B tester's
  probe:)* `supervisor.py` defines `quiet_for` both as a method and as a
  property; the property wins, `runner.py:~1668` calls `quiet_for()`, and the
  loop dies with `TypeError: 'float' object is not callable` on its first poll
  of any run with a worktree. So **no `timeout` or `silence` trip reaches the
  tree today**, on any provider. Fix the collision; and an exception inside one
  poll must be recorded (an event) rather than silently ending the loop.
  *Verified by:* runner test with `timeout=1` and a fake agent alive for ~7 s
  producing a `stuck`/`timeout` event (the tester's probe); a test that an
  injected exception in one poll is recorded and the next poll still runs.
- **P0-R2.6** The first trip of a run is reported exactly as today (same reason
  strings, same detail format), so existing watchdog tests stay green.
  *Verified by:* the existing supervisor tests, unmodified.

---

## P0-R3 — provider-specific prompt guidance (`F202`)

- **P0-R3.1** A provider block in `providers.yaml` may carry a new key
  **`agent_guidance:`** (a string). When present and non-empty, the prompt
  composed for a run **executing on that provider** contains it, verbatim, as
  its own section placed after the role instructions and before `## Task`.
  "Executing on" means the provider the run actually launched on after routing
  and fallback — an agent pinned to opencode that fell back to agy gets agy's
  guidance and not opencode's.
  No heading is mandated; tests assert the text appears verbatim and in that
  position.
  *Verified by:* `compose_prompt` test with two providers, only one declaring
  the key; and a routing test where fallback changes the provider.
- **P0-R3.2** **Absent or empty key → the prompt is byte-for-byte what it is
  today.**
  *Verified by:* golden test comparing the composed prompt to today's output for
  a provider without the key.
- **P0-R3.3** `notes:` is **never** sent to a model. A provider with `notes:`
  and no `agent_guidance:` produces no guidance section.
  *Verified by:* test with `notes:` set, asserting its text is absent from the
  prompt.
- **P0-R3.4** `agent_guidance:` is inherited through the provider `extends:`
  mechanism like every other key, and overridable in the project layer.
  *Verified by:* test with an `extends:` child.
- **P0-R3.5** agy's block carries guidance for the `view_file` truncation
  message: that the message means "request the next range" and how to request
  it with agy's actual pagination parameters (the implementer must find their
  real names in agy's tool schema — **do not invent them**; if they cannot be
  established, stop with `NEED_INFO`).
  *Verified by:* test that agy's merged provider has non-empty `agent_guidance`
  mentioning `view_file`.
- **P0-R3.6** agy's tool name and its truncation string appear in
  `providers.yaml` and nowhere in `src/multiagents/*.py`.
  *Verified by:* `grep` test over `src/multiagents/*.py` for `view_file` and for
  `does NOT show the entire file`.

---

## P0-R4 — the claude step counter counts turns, not stream deltas (`bug-b1c130`)

Today every claude `assistant` / `user` / `system` / `rate_limit_event` line is
`as: step` with no step index, so `Supervisor.observe` does `steps += 1` per
line. Measured: `ag-329af1` recorded 238 step events for 23 tool calls.

**Authoring rule for `providers.yaml`**, stated because it is what makes the
count correct: `turn` must be lifted from an event that *opens* a turn (or from
every event of it). A provider that only reveals its turn id at the end of a
turn would have its first turn's untagged events counted; such a provider must
not declare `turn`.

**Definition.** A *step* is one model turn. For a provider that reports a step
index (agy), that index is the step, as today. For a provider that does not,
the provider's `providers.yaml` may declare a **turn identifier**: a field
lifted by a stream rule, named **`turn`**, whose value is the same on every
event belonging to one model turn and different between turns. For claude this
is the assistant message id (`message.id`).

- **P0-R4.1** The stream rule `fields:` map accepts a `turn:` path. A parsed
  event carries the lifted value (string; absent → empty).
  *Verified by:* `parse_line` test on a claude assistant line.
- **P0-R4.2** Step counting, in order of precedence: an event with a step index
  counts as today (`max(steps, index+1)`); otherwise, **if the provider's stream
  rules declare `turn` on any rule**, only a non-empty `turn` counts, and only
  the first time that value is seen in the run — untagged events never count,
  including those that arrive before the first turn; otherwise (a provider that
  declares neither) it counts as today (`+= 1` for kind `step`).
  *Amended 2026-09-22:* the earlier wording ("once the run has seen any turn
  value") let claude's ~43 pre-turn `system`/`rate_limit_event` lines count and
  contradicted P0-R4.3; R4.3 wins. Whether a provider declares `turn` is read
  from its `providers.yaml` rules and handed to the Supervisor — never decided
  by provider name. The parsed value is exposed as `Event.turn` (str, default
  `""`).
  *Verified by:* supervisor unit tests for each branch, including a stream of
  10 events sharing one turn id counting as 1, and a provider with no turn
  declaration keeping today's count exactly.
- **P0-R4.3** The claude block declares `turn` on the rules whose events carry a
  message id. Replayed against a recorded claude stream, the step count equals
  the number of distinct assistant message ids, and is within a factor of 2 of
  the number of tool calls on a tool-heavy run (today it is ~10×).
  *Verified by:* replay test over a fixture built from a real claude stream-json
  capture (the tester must capture or construct one with realistic
  `message.id` values; the stored `stream.jsonl` files do **not** keep the raw
  payload and cannot serve).
- **P0-R4.4** No `if provider == …` in the supervisor or the parser; the
  behaviour is selected entirely by what `providers.yaml` declares.
  *Verified by:* the provider-name grep test.
- **P0-R4.5** `max_steps` semantics are unchanged for agy and opencode:
  replaying their recorded streams gives the same count before and after.
  *Verified by:* replay/characterization test.

**Delivery note (orchestrator's, not an agent's):** the gitignored
`.multiagents/config/project.yaml` carries `max_steps: 600` as a workaround.
It is reverted at merge. A claude run that still needs 600 afterwards means
P0-R4 did not work.

---

## P0-R5 — the MCP server never runs on a stale config silently (`bug-2138e6`)

Today `server.runner()` builds the `Runner` once with `load_config` and never
re-reads. The CLI re-reads per command.

**Decision: reload, not refuse.** The operator who edits a config file wants the
new value; refusing would turn every edit into a forced `/mcp`.

- **P0-R5.1** Before handling any MCP tool call that reads configuration
  (spawning, routing, budget, roster, limits), the server detects whether any
  config layer file (`.multiagents/config/project.yaml`, `agents.yaml`,
  `providers.yaml`, and the agent instruction files the roster reads) has
  changed since the config in use was loaded. If so, it reloads before acting.
  *Verified by:* test: build the server runner, edit `limits.max_steps` on disk,
  the next `start_agent` constructs its Supervisor with the new value.
- **P0-R5.2** A reload is **announced**: the result of the tool call that
  triggered it carries a field naming the files that changed. Silence is ruled
  out by the brief.
  *Verified by:* test on the tool result shape.
- **P0-R5.3** Agents already running keep the config they were started with; a
  reload affects only what happens after it.
  *Verified by:* test with a running (fake) agent across a reload.
  *Amended 2026-09-23:* "the config they were started with" covers the run's
  provider object, spec and Supervisor. End-of-run policy (`_finalize`,
  cooldown lengths, `silent_success_steps`) follows the config current when the
  run ends — that happens after the reload, and pinning it per run is not
  required.
- **P0-R5.4** A config that fails to load (invalid YAML, failed validation) is
  **not** swapped in: the previous config stays in force, and every tool call
  that would have used config reports the load error until the file is fixed.
  It never falls back to defaults.
  *Verified by:* test writing invalid YAML, asserting the error is reported and
  the previous value is still enforced.
  *Amended 2026-09-22:* a spawn under a broken config **proceeds on the previous
  config** and reports the error; it is not refused. The error names the file.
  Each call while the file stays broken reports the error; only the call that
  first detects a given change writes the P0-R5.8 event.
- **P0-R5.5** Detection costs no parsing when nothing changed (a cheap
  fingerprint, not a full reload per call).
  *Verified by:* test that `load_config` is not called on a tool call when no
  file changed.
- **P0-R5.7** Everything the runner **derives from config** is rebuilt on
  reload: at least `self.providers` (`runner.py:~243`, built by
  `load_providers` at construction), executors, budget readers, the roster.
  Nothing keeps a reference to the old config or old provider objects except
  runs already in flight.
  *Verified by:* test: add a provider `agent_guidance:` (or change a provider's
  `binary`) on disk; after reload the new spawn uses the new provider object.
- **P0-R5.8** Each reload is **recorded** in the tree's events log (time, files
  changed, success or load error), not only returned to the caller. The project
  root is writable from inside the container, so a subagent can edit
  `.multiagents/config/`; that exposure predates this work (the CLI already
  re-reads per command), but a reload makes it take effect inside the
  orchestrator's session, and it must leave a trace.
  *Verified by:* test that a reload appends one event with the changed files.
- **P0-R5.6** State the runner holds that is not config — the agent tree,
  in-flight runs, the deferred queue, standing advisor sessions — survives a
  reload intact.
  *Verified by:* test that active runs and the tree are the same objects before
  and after.

---

## P0-R6 — `wait_for_agents` under a pause (`bug-b864b8`)

Today `wait_for_any` returns immediately with `paused: true` and no
`still_running` whenever the deferred queue is paused, even while agents run.

- **P0-R6.1** A pause on the deferred queue does not end the wait: agents that
  are running are waited on exactly as without a pause (returns when one of
  them changes state or on timeout).
  *Verified by:* test with a paused queue and a running fake agent that finishes
  after a delay; the call returns that agent in `changed`.
- **P0-R6.2** `still_running` is present in **every** result, paused or not,
  and is a **list of agent id strings** of the watched agents still running
  (empty list when none). Today it is ids on one path and objects on the
  timeout path; both become ids. *(Amended 2026-09-22.)*
  *Verified by:* result-shape test over paused/unpaused × agents/no agents.
- **P0-R6.3** When a pause is in force, the result also carries `paused: true`,
  `reason`, and `retry_after_seconds`, as today.
  *Verified by:* same shape test.
- **P0-R6.4** With a pause in force and **nothing** running or watched, the call
  returns promptly (does not block for the full timeout), with `still_running:
  []`.
  *Verified by:* timing test with a short bound.
