# Detached jobs (DJ)

The orchestrator and the initializer run long commands themselves: a full
suite takes about 45 minutes. Today they start them as Claude Code
`run_in_background` tasks, because a finished task wakes the session. Two
facts make that fragile (BRIEF.md, 2026-10-11 07:55Z to 09:10Z):

- **Restart compaction kills them.** The driver stops the Claude CLI to
  compact it (driver.py:292-299). Claude Code then kills every task it tracks,
  even one it had already put in its own session. A tested `claude -p` under
  SIGTERM shows this. On 2026-10-11 a suite died at 66% this way.
- **Nothing brings them back.** Background tasks are not restored on
  `--resume`. Nothing woke the session afterwards.

Two more facts were tested on Claude Code 2.1.296 and shape this design:

- **A detached process survives.** A `setsid nohup … &` started by an ordinary
  Bash call, one that has already returned, survives the CLI.
- **A `Stop` hook can wake an idle session.** With `asyncRewake: true`, exit
  code 2 delivers the hook's stderr to the model, interactive or not.

That hook re-fires at every turn end. The same hook on `SessionStart` blocks
startup and wakes nothing.

So work and notification are separated:

- **The work** runs detached, recorded in a host-only registry.
- **The notification** comes from a watcher installed as that `Stop` hook. The
  watcher carries no work, so losing it to a compaction costs nothing. The
  next turn end starts it again.

The contract was put to the advisor on 2026-10-11 (ag-aa1387). Its findings
are folded in below, and Decisions 4 to 9 record them.

## Terms

- **Root session.** The orchestrator's or the initializer's Claude CLI,
  launched by the driver (`multiagents run`, `multiagents init-agent`,
  supervised or `--no-supervise`).
- **Registry.** The per-project directory
  `~/.multiagents/bg/<project-id>/` on the host, user-only (0700, files 0600).
  `<project-id>` is the project id used by the scheduler store. It is outside
  the project tree, so no agent container can see or write it.
- **Job.** One command started by `bg run`, identified by an id the registry
  allocates. Every path belonging to a job (record, log, exit file) is derived
  from its id inside the registry. No path is ever read from a record.

## Behaviours

**DJ-R1 — `multiagents bg run` starts a detached job.**
`multiagents bg run --owner ROLE [--name NAME] [--key KEY] -- CMD [ARG...]`.
ROLE is `orchestrator` or `initializer`.

- **Launch transaction.**
  1. The record is written in state `starting` before anything is spawned.
  2. The command is spawned in a new session and its own process group, with
     stdin from `/dev/null` and stdout and stderr appended to the job's log. It
     keeps the caller's working directory.
  3. The command's pid and process start time are committed to the record,
     which becomes `running`.
  4. `bg run` prints the job id, the pid and the log path, then exits 0
     without waiting for the command.
- **Exec failure.** If the command cannot be executed, `bg run` exits
  non-zero with the reason, and the record ends as `failed_to_start`.
- **Interrupted launch.** A `starting` record with no committed pid, older
  than 60 seconds, reads as `lost`.
- **Idempotent retry.** With `--key`, a second call with the same key while a
  job holding it is `starting` or `running` starts nothing: it prints that
  job's id and exits 0.
- **Environment.** The command inherits the caller's environment, except the
  variables the driver sets for the root session's MCP and safe-point
  authentication. Those are stripped, and the code names them in one list.
- **Exit file.** When the command ends, its status is written to the job's
  exit file. A job killed by a signal records `signal:<NAME>`.
- **Independence.** The job shares neither the session nor the process group
  of the process that called `bg run`.

Verified by:
- `bg run` returning while CMD still runs;
- the job surviving SIGTERM and then SIGKILL of the caller's whole process
  group;
- the job's session and process group differing from the caller's;
- the exit file holding 0, a non-zero status, and `signal:TERM`;
- an unexecutable command giving `failed_to_start` and a non-zero exit;
- an orphaned `starting` record reading as `lost`;
- `--key` not starting a second job;
- stdout and stderr both in the log, and the cwd preserved;
- a stripped control variable absent from the job's environment, while an
  ordinary variable is present.

**DJ-R2 — the registry and its states.**

Each record holds:
- `id`, `key`, `name`, `owner`, `argv`, `cwd`;
- `origin_session`, the root session's id from the driver's binding (DJ-R5),
  or `null` outside a root session;
- `pid` and `pid_start` of the command;
- `started_at`, `state`, `exit_code`, `ended_at`;
- `emitted_at` and `emitted_session`;
- `acked_at`.

`state` is derived on every read, in this precedence:

1. `stopped`, if `bg stop` ended it, whatever an exit file says.
2. `finished`, if the exit file exists.
3. `failed_to_start`, as in DJ-R1.
4. `running`, if the pid is alive with the recorded start time.
5. `lost`, otherwise: a reused pid, a reboot, a killed wrapper, or an orphaned
   `starting` record. `ended_at` is then the time of detection.

**Concurrency.** Writes are atomic and serialised by one registry lock that
`run`, `stop`, `ack`, `watch` and pruning all take. Concurrent calls never lose
or corrupt a record.

**Malformed records.** A record that is malformed, or of an unknown version,
is listed as `invalid` and never acted on: it is not signalled, read through
or pruned.

**Pruning.** A record is pruned only when it is both ended and acknowledged,
more than 7 days ago. Pruning removes only paths derived from the id inside
the registry, and never follows a symlink. Unacknowledged records are never
pruned.

Verified by:
- records surviving a fresh process;
- a reused pid reading as `lost`;
- the state precedence, one case per rule;
- 20 concurrent `bg run` calls giving 20 intact records;
- the file and directory modes;
- an invalid record listed and left untouched;
- pruning sparing an unacknowledged record and a symlink planted in the
  registry.

**DJ-R3 — `bg list`, `bg wait`, `bg stop`, `bg ack`.**
- **`bg list [--owner ROLE] [--json]`.** Shows every record with its derived
  state, and with `unacked` on ended jobs whose end has not been
  acknowledged.
- **`bg wait ID [--timeout S]`.** Blocks until the job has ended. It then
  prints the report (DJ-R4) and acknowledges the end. It exits 0 for
  `finished` with 0, 1 for any other end, and 124 on timeout.
- **`bg stop ID`.** First verifies the pid and its start time. Then it sends
  SIGTERM to the job's process group, and SIGKILL after a 10-second grace.
  - The record becomes `stopped`.
  - On a job already ended, it changes nothing, reports the state and exits 0.
  - On an unknown id, it exits 1.
  - Descendants that left the job's process group are documented as out of its
    reach.
- **`bg ack ID...`.** Records `acked_at` on ended jobs.

Verified by:
- each exit code of `bg wait`, and `bg wait` acknowledging;
- `bg stop` ending a job that ignores SIGTERM;
- `bg stop` leaving another job running;
- `bg stop` refusing to signal when the pid's start time no longer matches;
- a repeated `bg stop` being a no-op;
- `bg ack` clearing `unacked`.

**DJ-R4 — `bg watch`, the notifier.**
`bg watch` is run as the root session's `Stop` hook (DJ-R5). It reads the hook
input (JSON on stdin) for the session id, then looks up the driver's binding
for that session, which gives the CLI's pid and start time. Exit 2 wakes the
session; exit 0 is silent.

1. **Invalid binding.** No binding, or a binding whose CLI is not alive with
   its recorded start time: exit 0 at once, touching nothing.
2. **Duplicates.** Another live watcher for the same owner holds the watcher
   lock (an atomic lock carrying pid and start time; a stale lock is taken
   over): exit 0 at once.
3. **Due ends.** Any of the owner's ended, unacknowledged jobs is *due* if
   either:
   - it has never been emitted, or
   - it was emitted to a different session, or more than 15 minutes ago.

   If any job is due, report all due jobs, set `emitted_at` and
   `emitted_session`, and exit 2. Emission is not acknowledgement: an end
   stays unacknowledged until `bg ack`, `bg wait` or a later due emission
   window, so a crash between emission and delivery loses nothing. Delivery is
   at least once, and duplicates are tolerated.
4. **Nothing to watch.** If the owner has no `starting` or `running` job, exit
   0 at once.
5. **Waiting.** Otherwise wait.
   - When one of the owner's jobs ends, apply rule 3.
   - When the bound CLI disappears, exit 0, recording nothing. If both happen
     at once, the CLI's disappearance wins.
6. **Deadline.** At the hook timeout minus 60 seconds, exit 2 with a heartbeat
   listing the jobs still running and how long each has run. That turn end
   re-arms the watcher.

**A report** goes on stderr and is capped at 4,000 characters. For each job it
gives:
- the id, name, state, exit code and duration;
- the log path;
- the last 20 lines of the log, each cut to 300 characters, with control
  characters stripped.

**Ownership.** Jobs belong to the role, not to a session. A fresh root session
of the same role adopts the earlier session's unacknowledged ends; they are
due to it by rule 3. Two simultaneous sessions of one role share one watcher
through the lock.

Verified by:
- each rule above, with its exit code;
- a due end re-emitted to a new session, and not re-emitted to the same session
  within 15 minutes;
- an end that occurred before the watcher started being reported at once;
- the watcher exiting 0 within a few seconds of its bound CLI dying;
- the report cap and sanitisation, on a log of long, binary lines.

**DJ-R5 — the hook is installed for root sessions only.**
- **Launch.** When the driver launches a root session, it writes a settings
  file for that session in the host state directory, outside the project
  tree. The file declares one `Stop` hook with `asyncRewake: true` and timeout
  `bg.watch_timeout`.
  - `bg.watch_timeout` comes from the project config, default 3600.
  - A value under 120 is refused at config load.
- **The hook command** names the absolute path of the interpreter that runs
  the driver, the module entry point of `multiagents bg watch`, the role, and
  the absolute project path. It does not rely on `multiagents` being on PATH,
  nor on the hook's working directory.
- **Passing the file.** The claude provider's launch passes the file with
  `--settings`, beside its other root-only flags at
  defaults/providers/claude.sh:705-717. This holds supervised and
  `--no-supervise`, on a first launch and on every restart, including an OA
  profile switch and a fresh session id.
- **Binding.** After spawning the CLI, the driver records the CLI's actual pid
  and start time against the session id. Under `--no-supervise`, which execs,
  it records its own pid and start time just before the exec.
- **What it never touches.** `~/.claude/settings.json`, the project's
  `.claude/settings.json`, any hook they declare, and the configuration of
  spawned agents.
- **Other providers** get no hook. Their launch is unchanged.

Verified by:
- the launch argv for both roles carrying `--settings`, with a file whose
  `Stop` hook has the right command, flags and timeout;
- the hook command running with an empty PATH and from another cwd;
- the binding naming the real CLI pid, supervised and `--no-supervise`;
- the binding refreshed on a restart;
- a spawned agent's argv not carrying the file;
- user and project settings byte-identical after a launch;
- `bg.watch_timeout: 60` refused.

**DJ-R6 — the compaction return names the jobs.**
- **Eligibility.** compact_return.py counts unacknowledged ends and
  `starting`/`running` jobs as work in flight. A return message is therefore
  produced even when detached jobs are the only work (driver.py:1135 today
  returns none without in-flight work).
- **Content.** The message lists, with states read at delivery time:
  - the running jobs, with id, name, how long each has run and log path;
  - the unacknowledged ends, with id, name, exit code and log path.
- **Limits.** The return message's existing 4,000-character cap still holds.
- **No acknowledgement.** The return message does not acknowledge anything.

Verified by:
- a return produced when one detached job is the only work;
- the message's content with one running job and one unacknowledged end;
- the cap holding with 50 jobs;
- no job lines when there are none.

**DJ-R7 — deferred: no compaction under a live `run_in_background` task.**
The idea was that the claude `compact` probe answers "not now" while a
classic background task runs.

The advisor found two blockers:
- the repository has no established signal for that state;
- explicit OC requests skip the probe (`driver.py:_explicit_due`).

The Stop hook's input documents a `background_tasks` field. Before this is
contracted, real 2.1.296 payloads must be captured for running, finished and
stopped tasks. It moves to a later spec, DJ-b, which must also cover:
- every compaction path: automatic, explicit OC, unattended and native;
- periodic rechecks, with a durable deferral clock;
- a forced deadline that still passes the CW safe-point barrier.

**DJ-R8 — the briefs say to use it, with a fallback.**
The shipped orchestrator and initializer briefs, and AGENTS.md's test-suite
section for root sessions, say:
- a command expected to run longer than a few minutes is started with
  `multiagents bg run`, not `run_in_background`;
- its end normally arrives as a hook notification;
- the hook does not fire after a user interrupt or an API error stop, so at
  every wake the session also reads `bg list` for unacknowledged ends;
- an end is acknowledged with `bg ack` once handled.

Verified by: a test asserting the shipped briefs contain these instructions.

**DJ-R9 — callers inside an agent container are refused.**
Every `bg` subcommand refuses to run, with exit 1 and a one-line reason, when
it detects it is running inside the docker executor's agent container. Agents
neither see nor can address the host registry.

Verified by: each subcommand refused under the container's detection signal.

**DJ-R10 — live evidence for what tests cannot show.**
A script, `scripts/dj-live-check.sh`, run by hand and never in the suite,
drives a throwaway `claude -p` session on the cheapest model through four
checks:
- a `bg run` job surviving the CLI's SIGTERM;
- the `Stop` hook waking the idle session when the job ends;
- the heartbeat at a short `bg.watch_timeout`;
- the hook command working with an empty PATH.

It prints a pass or fail line for each check and the Claude Code version. The
orchestrator runs it before merging and records the output in BRIEF.md.

Verified by: the script existing, and its dry-run mode (no `claude` call)
passing in the suite.

## Out of scope

- **Spawned agents** (implementers, testers, …). They run in the container
  under their own watchdogs, and their long commands are the silence-watchdog
  item queued in BRIEF.md.
- **Claude Code `--bg` background sessions, channels and Monitor.** They were
  considered on 2026-10-11 and set aside: they change how the root session
  runs, need opt-in flags, or cap at 30 minutes.
- **Restoring a job's output into the transcript.** The log path is reported
  and the session reads it.
- **DJ-R7**, which goes to DJ-b.

## Decisions

1. **Notifier hook: `Stop` with `asyncRewake`, not `SessionStart`.** It was
   the only variant that woke an idle session in the 2026-10-11 test.
2. **Heartbeat at the deadline rather than silence.** A silent expiry would
   leave a running job unwatched until the next turn. One wake per
   `bg.watch_timeout` is the price.
3. **The registry is on the host, under `~/.multiagents/`, not under `/tmp`
   or the project.** `/tmp` is a tmpfs, lost at reboot. The project's
   `.multiagents/` is writable from the agent container, so a forged record
   there could steer host signals or deletions (advisor, finding 1).
4. **Delivery is at least once.** Emission and acknowledgement are separate,
   and a due end is re-emitted to another session or after 15 minutes. The
   alternative, marking an end reported as it is printed, loses it whenever
   the session dies between the hook's exit and the model's turn (finding 2).
5. **Jobs belong to a role, not a session.** Fresh sessions adopt them, since
   a session may restart with a new id. Simultaneous sessions of one role
   share a single watcher.
6. **R7 is deferred to DJ-b.** No real signal is established yet, and a test
   on a synthetic transcript would encode an invented format (findings 3 and
   4).
7. **The hook runs by absolute interpreter and module path** (finding 6).
8. **The command's process group is signalled only after verifying the pid's
   start time** (finding 10).
9. **Root-session control variables are stripped from job environments**
   (finding 11).
