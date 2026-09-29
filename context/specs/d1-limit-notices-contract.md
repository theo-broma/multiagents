# D1: limit-hit notices, the contract

**Status:** contract, written by the orchestrator on 2026-09-29, from the
requirements in `limit-notices.md` (LN-R1..R6).
- Ids are `LN-C*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.
- Research: ag-a71656 made the enforcement-site table, and found no source
  lines surviving config load and no OOM detection.
- Depends on H14 (`h4-h14-routing-limits.md`, LM-R1/LM-R2). It is
  implemented after H14 lands.

## Behaviours

**LN-C1: one event shape.** When a covered limit (LN-C6) constrains
execution, the runner emits a `limit_hit` tree event. Constraining means
that something is refused, queued or deferred, tripped to stuck, killed,
or cut short (LN-R1). The event carries:
- `key`: the dotted config key, e.g. `limits.max_concurrent`. A budget-tag
  ceiling uses `budget_tag.<tag>`, e.g. `budget_tag.phase6`. Other
  examples:
  `executor.docker.memory`, or `agents.<name>.timeout`;
- `value`: the effective value;
- `effect`: one of `refused`, `deferred`, `stuck`, `killed`,
  `kill_uncertain`, `stopped`;
- `scope`: the dedup scope of LN-C4. It is `tree`, a node id, or a budget
  tag;
- `node`: when a node is concerned;
- `source`: see LN-C2;
- `message`: one human line, see LN-C3.

A value that was only *near* its limit never emits the event.

Verified by: for each covered key, a test drives the constraint and finds
exactly one `limit_hit` with those fields. A run that stays under every
limit emits none.

**LN-C2: provenance (LN-R2).** `source` is exactly one of:
- `{layer: "agent", file, line}`: the per-agent value in an `agents.yaml`
  layer;
- `{layer: "project", file, line}`: a `project.yaml` layer, either the
  project's own or the global config's;
- `{layer: "builtin", file, line, override_file, override_key}`: the
  shipped defaults file and line.
  - `override_file` is the project's `.multiagents/config/<file>.yaml`
    where the user would set it, and `override_key` is the key to set
    there.
  - That file need not contain the key yet.
  - For a value with no line in any shipped file, such as a dataclass
    default, `file` and `line` are null. The notice then says "built-in
    default" and still names the override.
- `{layer: "call", tool, argument}`: the value came from a tool call, such
  as `start_agent(timeout=…)` or a `budget_tag` ceiling.
  - `override_key` names the config key to use instead, when one exists.
  - For a budget tag, the **first** ceiling set wins (`tree.py` ~645–665).
    So `source` identifies that original call, meaning the node that set
    it, not the later call that was refused.

Paths are absolute. Lines are 1-based and point at the key's own line.

Provenance is kept **only for the covered keys**, not for every key.
- `effective_limits` from LM-R2 gains the same `source` detail for its
  three keys.

Verified by:
- a temporary project whose `project.yaml` sets `limits.max_concurrent` on
  a known line yields that exact file and line;
- removing the key yields `builtin`, pointing at the shipped defaults line,
  with `override_file` set to the project's `project.yaml`;
- an agent-level `timeout` yields `agents.yaml` and its line;
- an explicit `start_agent(timeout=…)` yields `call`.

**LN-C3: surfaces (LN-R3). The user sees the notice without asking.**
- **The event**, in `events.jsonl`, is the log entry.
- **The tool result.** The tool call that hit the limit carries the
  notice's `message`:
  - on `start_agent`, that includes the refusal error text. Today these
    are bare `RuntimeError` or `PermissionError` texts (`runner.py`
    ~792–828); they now include the message.
  - `wait_for_agents` results carry `limit_notices`: the notices emitted
    since that caller's previous `wait_for_agents`, covering new hits and
    clears (LN-C4).
- **The terminal.** `multiagents run` prints each `limit_hit` and
  `limit_cleared` as one terminal line, the same line `multiagents watch`
  already prints.
- **The monitor.** It shows active limit notices as alerts, meaning hit
  and not yet cleared, and keeps the last cleared ones visible in the
  event view. The route is a snapshot field that the TUI renders; merely
  emitting the event is not enough.

The message format names what was limited, the key, the value and the
place to change it. Examples:
- `limit: limits.max_concurrent = 4 (/…/.multiagents/config/project.yaml:235) — start of 'tester' refused; raise it there to allow more.`
- For a built-in value: `… = 900 (built-in default, …/defaults/project.yaml:236; set limits.default_timeout in /…/.multiagents/config/project.yaml to change it)`.

Verified by:
- the `start_agent` refusal text contains the message;
- `wait_for_agents` returns a new notice once, and not again on the next
  call;
- the monitor snapshot contains the active notice;
- **integration:** a live `multiagents run` session, driven by a fake
  provider CLI, prints the `limit_hit` line to its terminal while the
  provider is still active. A printer test fed a synthetic event is not
  sufficient, because `run` does not tail events today (`cli.py`
  ~1564–1588).

**LN-C4: deduplication (LN-R4).** The dedup key is `(key, scope)`.
- The first constraint emits `limit_hit`.
- Further constraints under the same key while it is still constraining
  increment a counter. They emit no new event and no new result line,
  apart from the counter.
- When the constraint stops, `limit_cleared` is emitted with `key`,
  `scope` and `count`. The constraint stops when the next attempt
  succeeds, or when the node ends or the pause clears.
- **Shared state** (advisor, turn 13). Each agent's runner is its own
  process, and the monitor reads a separate snapshot. So the active-notice
  state (key, scope, count, first-hit time) lives in project state that
  every runner and the monitor read and write consistently, under
  `.multiagents/`, not in one process's memory. It survives a restart, so
  a hit that is still active is not announced again.
- `events.jsonl` stays the history.
- `wait_for_agents`'s `limit_notices` uses a per-caller cursor, one per
  calling node, over that history. Two callers each see a notice once.
- Verified by: ten refused starts under `max_concurrent`, spread across two
  Runner instances over the same project, give one `limit_hit`. The monitor
  snapshot shows it as active. A successful start then gives one `limit_cleared` with
  `count: 10`.

**LN-C5: container memory (LN-R6).**
- When an agent process in the docker container dies by SIGKILL (exit
  137), the host compares the container cgroup's `memory.events` `oom_kill`
  counter with the value it read when that run started. The counter is
  resolved per cgroup layout; systemd with cgroup v2 is the minimum.
  - The counter is **container-wide**. An increase is attributed to this
    run only when there is run-specific evidence: this was the only
    process in the container that the runner started which was alive
    during the interval, and it died with SIGKILL while the counter
    increased. Only then is the notice `effect: killed` on
    `executor.docker.memory`, with its provenance.
  - In every other case the notice is `effect: kill_uncertain`, on the
    neutral key `process.sigkill` with `source: null`. That covers a
    counter that did not increase, an unreadable counter, and an increase
    during a run that had concurrent siblings.
    - It reports a SIGKILL of unknown cause.
    - When the counter did increase, it may say that the container hit
      its memory limit at the time and name `executor.docker.memory` as a
      possible cause, but it never attributes the kill to it.
- **Test seam** (a tester question): the docker executor exposes one
  method, `oom_kill_count() -> int | None`, which reads the container
  cgroup's `oom_kill` counter. `None` means unreadable. Every OOM decision
  goes through it, so tests monkeypatch it. Whether the run was the sole
  occupant is decided from the runner's own record of which runs it had
  alive in that container.
- `docker inspect … State.OOMKilled` is not evidence, because it describes
  the container, not the exec'd process.
- Verified by, with a stubbed counter reader:
  - a sole run with an increase gives `killed` on
    `executor.docker.memory`;
  - an increase while a sibling ran concurrently gives `kill_uncertain` on
    `process.sigkill`;
  - no increase gives `kill_uncertain`;
  - an unreadable counter gives `kill_uncertain`.

**LN-C6: coverage (LN-R5).** Covered:

| Key | Effect | Site today |
|---|---|---|
| `limits.max_concurrent` | refused | runner ~797 |
| `limits.max_depth` | refused | ~792 |
| `limits.max_children` (the parent's cap, LM-R1a) | refused | ~805 |
| `limits.budget_tokens` | refused | ~811 |
| budget tag ceiling (`call` source) | refused | ~817 |
| `budget.reserve_headroom` | deferred, when it causes a defer or pause | ~1525–1569 |
| timeout: agent, `limits.default_timeout` or call | stuck | ~2498 |
| silence timeout: agent or `limits.silence_timeout` | stuck | ~2498 |
| `max_steps`: agent or limits | stuck | supervisor ~163 |
| `limits.doom_loop_repeats` | stuck | supervisor ~145 |
| `limits.commit_fix_attempts` | stopped, on exhaustion only | ~2254–2286 |
| `limits.commit_fix_timeout` | stopped, when it cuts a fix attempt short | ~2254–2286 |
| `limits.wind_down_seconds` | deferred, when it is what makes a provider unavailable and work is deferred | ~560–586 |
| `executor.docker.memory` | killed or kill_uncertain | LN-C5 |

Not covered, and why:
- `executor.docker.cpus`: throttling never refuses or stops anything, so
  it fails LN-R1.
- `executor.docker.pids_limit`: a fork failure inside a tool cannot be
  attributed from the host without guessing. This is recorded as future
  work.
- `compact_at_tokens` and `context_wind_down_tokens`: they already have
  their own dedicated notices (`compacted`, `context_wind_down`).
- `doom_loop_rearm`: a parameter of the doom-loop trip, which is covered
  under `limits.doom_loop_repeats`. It constrains nothing by itself.
- `readonly_paths`: a protection, not a limit, and already reported as
  `readonly_violations`.
- Driver retry and turn settings (`restart_attempts`, `supervised_turns`,
  `limit_max_waits`) govern driver loops, not agents. They are revisited
  if the user asks.

Verified by: LN-C1's per-key tests, one per covered row.

**LN-C7: nothing else regresses.** Existing events (`stuck`, `deferred`,
`paused`, `commit_fix_attempt`, …) keep being emitted as today.
`limit_hit` is added alongside them, not instead of them. The existing
suite stays green, apart from the known reds.
