# D1 — limit-hit notices

**Status: requirements, not a contract.** This is the user's request (made
2026-09-27 and again on 2026-09-28), shaped by the initializer on
2026-09-28. The contract may be written at any time in phase 6. It is
implemented after H4 (`phase6-hardening.md`).

## What the user asked for

Whenever a limit set in configuration is reached **and actually constrains
execution**, two things happen:

- a log entry records it;
- the user is told, with the **config file's path and the line** of the
  setting that limited it, so they can change it easily if they want.

The user's examples:
- `limits.max_concurrent`, `max_depth` and `max_children`;
- a watchdog timeout or step cap (`default_timeout`, `silence_timeout`,
  `max_steps`);
- `commit_fix_attempts`;
- a budget tag ceiling;
- the container's `memory`.

## Requirements

- **LN-R1 — only real constraints.** A notice fires when a limit changes
  what happens: something is refused, queued, killed, stopped or cut short.
  A value that was merely close to its limit does not fire one.

- **LN-R2 — where the value came from.** Each notice names:
  - the setting's key;
  - its effective value;
  - the file and the line it came from.

  The file can be one of three layers:
  - the per-agent override in `agents.yaml`;
  - the project's `project.yaml`;
  - a built-in default.

  When the value is a built-in default, the notice names the shipped
  defaults file and line, and says which user file would override it and
  under which key. The user's file may not contain the key at all yet.

  The config loader has to keep the file and line for every limit it
  resolves.

- **LN-R3 — where it surfaces.** Proposed default:
  - an event in the tree's event log, which is the log entry;
  - a line in the result of the tool call that hit the limit (`start_agent`,
    `wait_for_agents`, `merge_agent`, ...), so the orchestrator sees it and
    can relay it;
  - a line in the `multiagents run` terminal and in the monitor.

  The contract may change the surfaces. **The user must see it without
  asking.**

- **LN-R4 — deduplication.** A limit that is hit repeatedly, for example a
  spawn refused every poll by `max_concurrent`, produces:
  - one notice when it first constrains;
  - one when it stops constraining;
  - between the two, a count rather than a flood.

  The key is (setting, node or scope).

- **LN-R5 — coverage.** Every limit the user listed is covered. The contract
  lists the remaining limits in the config schema and says, for each one,
  whether it is covered or why not.

- **LN-R6 — container `memory`.** An out-of-memory kill (exit 137 or the
  cgroup's OOM counter) is reported as this limit, naming `executor.docker.memory`.
  It must not be reported as a generic failure.

## Suggested pipeline

Full pipeline, no adversary. It adds reporting, and changes no boundary.
