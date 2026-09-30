# D2: tmux viewer windows (step 1), the contract

**Status:** contract, written by the orchestrator on 2026-09-30.
- **Source:**
  - phase6-hardening.md, item D2;
  - BRIEF, "tmux to watch the tasks" (the user, 2026-09-28).
- **Scope: step 1 only.** tmux runs **viewers**. The runner keeps owning
  every agent process. Nothing in launch, supervision, adoption or the
  docker exec path changes.
- **Ids:** `TM-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## Decisions on the "to decide" list

- **One tmux session per project.** It is named `ma-<project slug>`, with
  one window per agent run.
- **The socket lives in the host-only state directory,** never under the
  project's `.multiagents/`, which the docker executor mounts writable
  (`executor/docker.py` ~984). Its path is
  `<state_root()>/host-authority/<ProjectPaths.slug>/tmux/sock`: the same
  protected per-project directory as NoticeState and StartupHealth. The
  `tmux` directory is created with mode 0700, and an existing one with
  wider permissions or another owner is refused.
- **tmux is always invoked with `-S <that socket>`,** never on the user's
  default server.
- **A stale socket,** with no server listening, is removed and recreated.
  Only this socket's server is ever killed.
- **Callers.** For v1 these are the host CLI and the monitor's
  authenticated action only. There is **no MCP tool**, and agents cannot
  create viewers.
- **No windows for command tools.** An agent's tests and builds get no
  window of their own; its viewer shows them as tool calls.
- **No provider is excluded,** because the viewer reads the normalised
  stream that every provider writes.
- **Without tmux,** the CLI says so and exits non-zero, and the monitor
  hides the button. Nothing else degrades.

## Behaviours

**TM-R1: `multiagents view <agent_id>` is a follower that makes the
stream readable.** It needs no tmux.
- **What it reads.** It reads `runs/<id>/stream.jsonl`, which already
  holds normalised events: `kind` is one of text, tool, step, result,
  raw or error.
- **What it prints.** One readable line or block per event:
  - assistant text is wrapped;
  - a tool call shows its name and a one-line summary of its arguments;
  - errors and the final result are marked.
- **Following.**
  - `--follow`, the default when the run is active, keeps reading until
    the run is terminal and then prints the final status.
  - `--no-follow` prints what exists and exits 0.
  - A run whose stream does not exist yet is waited for while the run is
    active.
- **Bad input.**
  - An unknown agent id exits 2 with a message.
  - A malformed or partial line is skipped, not fatal. A line still being
    written is not printed half-read.
- **Read-only.** It never writes to the run directory, the tree or the
  stream.
- **Untrusted input.** The stream is written by the agent's run and
  treated as untrusted. Every rendered string passes through one
  terminal-safe formatter:
  - C0 and C1 control characters, ESC, CR and backspace are escaped
    visibly;
  - newline and tab are kept;
  - no stream content is ever passed to a shell or used as a tmux target.
- **Validation.** The agent id must match `^ag-[0-9a-f]{6}(-[0-9]+)?$`
  before any path is derived from it; otherwise it exits 2.
  `stream.jsonl` is opened only when it is a regular file: a symlink or
  a FIFO is refused.
- **Truncation.** When the file shrinks or is replaced (adoption
  truncates it, `runner.py` ~3652), the follower reopens it from the
  start and prints a one-line marker.
- **Bounds.** One rendered event is at most 4 000 characters, and
  anything longer is cut with a marker.
- **Reuse.** The presentation should follow the transcript rendering in
  `monitor/snapshot.py` ~613 and `tui.py` ~215 where it fits. The
  terminal-safe formatter is new, and the monitor TUI uses it too if it
  prints stream text.
- Verified by:
  - a fixture stream with each event kind renders each one;
  - a truncated last line followed by its completion prints once;
  - `--no-follow` on a finished run exits 0;
  - an unknown id exits 2;
  - a malformed line is skipped;
  - the run directory is byte-identical after viewing;
  - text containing `\x1b]0;x\x07`, `\x1b[2J`, `\r` and `\x9b` is
    printed with no raw control byte, while newlines survive;
  - an id like `../x` or `ag-zzz` exits 2;
  - a symlinked `stream.jsonl` is refused;
  - truncating the stream mid-follow prints the marker and then the new
    content.

**TM-R2: `multiagents tmux open <agent_id>` puts a viewer in the
project's tmux session.**
- **Session and window.** It creates the session on the private socket
  if it is missing. It adds a window named after the agent id that runs
  `multiagents view <id> --follow`, with an absolute interpreter path so
  that it does not depend on the tmux server's PATH.
- **Idempotent.** A second `open` for the same id selects the existing
  window instead of creating another.
- **Output.** It prints the attach command:
  `tmux -S <socket> attach -r -t ma-<slug>:<id>`. The attach is read-only,
  with `-r`.
- **Headless.** It never attaches by itself. It works without a terminal,
  so the MCP server or the monitor can call it.
- `multiagents tmux attach-cmd <id>` prints the same command without
  creating anything. It exits 1 when the window does not exist.
- Verified by:
  - with a fake `tmux` on PATH that records its argv:
    - the session is created once;
    - the window command is the view command;
    - every invocation carries `-S <socket>`;
    - a second `open` does not create a second window;
    - the printed command contains `attach -r`;
  - the socket directory is mode 0700, and is under the host-authority
    directory, not under `.multiagents/`;
  - a socket directory with mode 0777 is refused;
  - a stale socket file is replaced.

**TM-R3: lifecycle and cleanup.**
- **Terminal runs.** A viewer window closes by itself about 60 s after its
  run is terminal: the view exits, and tmux's own `remain-on-exit off`
  closes the window.
- **`multiagents tmux close <id>`** removes one window.
- **`multiagents tmux kill`** removes the session and the socket.
- **Session and agents are independent.** Killing the session or a window
  never affects the agent process. Stopping or finishing an agent never
  requires tmux.
- Verified by:
  - with the fake `tmux`, `close` and `kill` send the right commands;
  - with a real `sleep` agent process, killing the session leaves the
    process alive, when a real tmux is available (skipped otherwise).

**TM-R4: the monitor exposes it.**
- **The button.** Each run row in the monitor gets a "watch in tmux"
  action. It calls the TM-R2 open, and its result shows the attach
  command with a copy control.
- **When tmux is missing** (`shutil.which("tmux")` is None at snapshot
  time), the action is absent from the snapshot and the page.
- Verified by:
  - a snapshot with tmux present lists the action for a run;
  - with it absent, the action is not listed;
  - invoking the action returns the attach command.

**TM-R5: no tmux.**
- `multiagents tmux …` exits 3, with a message naming tmux as missing.
- `multiagents view` still works.
- Verified by: the tmux commands with an empty PATH.

**TM-R6: nothing else changes.**
- Launch, supervision, adoption, the docker executor and stream writing
  are untouched.
- The existing suite stays green, apart from the known reds.

## Out of scope

- tmux as the process supervisor (step 2).
- Windows for command tools.
- Writable or interactive panes.

## Amendments of 2026-09-30, after the test suite (ag-743603)

The tester's assumptions are accepted as contract. Its full list is in
.multiagents/runs/ag-743603/result.json.

**The CLI.**
- `multiagents [--path P] view <id> [--follow|--no-follow]`.
- `multiagents [--path P] tmux open|attach-cmd|close <id>`, and `tmux kill`.
- `open`, `attach-cmd` and `close` validate the id and exit 2 without
  calling tmux.
- A bad socket directory, a symlink, a FIFO or a directory exits non-zero
  (not 3) with a message on stderr.

**The monitor action.**
- It is `tmux_open` in `monitor.actions.ACTIONS`, with the payload
  `{"agent_id": …}`.
- It returns `{"ok", "message", …}` with the attach command in it.
- Run rows in `snapshot()["running"]` list it under `actions`.

**Runs.**
- An unknown agent is one with no tree node and no run directory.
- A run is active when its status is in `ACTIVE`, and terminal when it is
  in `TERMINAL`.
- `view` prints the final status by name.
- `view` exits within 90 s after its run turns terminal. This replaces
  "about 60 s" as the testable bound.

**Windows and the session.**
- Windows are named exactly after the agent id, in the session
  `ma-<slug>`.
- The window command ends with `… view <id> --follow`, and starts with an
  absolute interpreter path.
- `remain-on-exit`, if it is set, is `off`.
- `kill` leaves no socket file.
- `close` and `attach-cmd` create nothing when there is no server.

**Output.** Newline and tab survive as raw bytes.

**Not tested:**
- a socket directory owned by another user;
- the page markup;
- the auto-close timing;
- a symlinked `tmux` dir;
- `close` on a missing window;
- `kill` with no server.
