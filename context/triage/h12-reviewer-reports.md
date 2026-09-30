# H12 triage input: reviewer reports (copied 2026-09-30 for a read-only researcher)

## reviewer ag-98e037

This commit implements the requirements of `bug-2cebea`, but introduces several severe correctness and performance hazards into the async lifecycle code, primarily around how events are handled across yields and hot loops.

## Findings

1. `src/multiagents/runner.py:1610`
   Overwriting `retried.done` with `run.done` after yielding to `_launch` orphans the event object that was visible to concurrent tasks during the yield points.
   *Concrete failure*: If a background task (such as a stream reader or an API poller) accesses `self.runs[node_id].done` during the `await self._launch` execution, it captures the newly created event; after `_launch` returns, this event is overwritten on the object with the old one, leaving the background task hanging forever on an event that will never be set.

2. `src/multiagents/runner.py:2887`
   The `classify()` helper inside `wait_for_agents` duplicates the concurrency logic of `_occupies_slot` but incorrectly omits the `procs.alive` check required by SL-R4.
   *Concrete failure*: If an agent's process dies unexpectedly but its status in the tree remains `running` or `stuck`, `wait_for_agents` will mistakenly treat it as a live process and hang indefinitely waiting for it to finish, directly violating the SL-R4 contract.

3. `src/multiagents/runner.py:1437`
   `_maybe_clear_stuck` performs a disk-backed `self.tree.get()` lookup on every stream event while a run is stuck, and fails to clear `run.trip_kind` if the status is changed externally.
   *Concrete failure*: If an operator manually un-sticks an agent (e.g. via `steer_agent`), `node.status` changes but `run.trip_kind` remains truthy. This causes every subsequent stream event to trigger a `self.tree.get()` read for the remainder of the run, severely degrading the performance of the async event loop on the hot consume path.

4. `src/multiagents/runner.py:1596`
   The broad `contextlib.suppress(Exception)` block silently swallows all exceptions raised during the retry `_launch`.
   *Concrete failure*: If the second attempt fails to launch due to a configuration error, quota exhaustion, or a missing workdir, the error is discarded and the original failure is reported instead, leaving the operator with no logs or stack traces to understand why the retry failed.

VERDICT(rejected, 4): four defects, the first blocking

## reviewer ag-e4919f

## Findings

1. `src/multiagents/executor/docker.py:318`: `_versions_dir` unconditionally returns `resolved.parent` for any symlink pointing outside its launcher directory, directly contradicting its docstring which claims nested per-version directories (e.g. `versions/1.0.0/bin/cli`) read as unversioned. If a user has a nested CLI structure, `mounts()` bind-mounts only the old version's `bin` directory at container creation; upon host upgrade, `_versioned_argv` rewrites the spawn command to the new unmounted directory, causing the agent to crash with an executable not found error.
2. `src/multiagents/server.py:494`: The `_refresh` method is invoked on every MCP tool call but lacks any form of concurrency control. Concurrent tool calls (e.g. from parallel subagent workflows) will simultaneously observe a config change and enter `run.reload()` in parallel, resulting in a race condition that non-atomically mutates `run.config` and `run.providers` and floods the transcript with duplicate `config_reload` events.
3. `src/multiagents/server.py:466`: Computing the config fingerprint executes a recursive directory traversal (`rglob("*.md")`) over the `agents` folder. Since `_refresh` evaluates this on the critical path of every single MCP tool call, it imposes an unboundedly growing filesystem latency cost as the user's agent definitions grow.
4. `src/multiagents/server.py:517`: A hardcoded English error string (`"config failed to load after a change to..."`) is added directly to `_refresh`, violating the stated invariant that no error string is added to `src/multiagents/*.py`.

VERDICT(rejected, 4)

## reviewer ag-e8565d

I have completed the code review of Phase 0 contract B on the `refactor/split-consume` branch. Here are the findings based on the provided diff.

## Findings

1. **Severity 1 (Correctness/Data Loss): Terminal left dirty on SIGTERM**
   `src/multiagents/driver.py:204` (and `cli.py` cbreak picker)
   `_run_attached` swallows `SIGINT`, `SIGQUIT`, and `SIGHUP` to pass them to the child, but it fails to handle `SIGTERM`. If the orchestrator receives a `SIGTERM` (e.g., system shutdown or user running `kill`), it dies immediately without executing the `finally` block, leaving the user's terminal wedged in raw/cbreak mode.
   *Fix direction: Handle `SIGTERM` explicitly (e.g., raising `SystemExit`) so the `finally` block executes.*

2. **Severity 1 (Correctness): Leaked provider processes on exception**
   `src/multiagents/scripts.py:186`
   `run_action` uses `child.communicate()` without a `finally` block or context manager. If an asynchronous exception like `KeyboardInterrupt` occurs while waiting for the provider script (e.g., user hits Ctrl-C during a slow `compact` probe), the exception propagates out and the running child process is leaked in the background.
   *Fix direction: Wrap the `Popen` lifecycle in a `finally` block or `with child:` to ensure it is killed and waited on.*

3. **Severity 1 (Correctness): Race condition overrides deliberate CLI exit**
   `src/multiagents/driver.py:810`
   If the child CLI exits cleanly (e.g., the user types `/quit`) exactly as the compaction grace period expires, `stalled()` returns `True` and sets `compaction.requested`. `_run_attached` correctly returns the clean exit code 0, but `_attached` sees the requested compaction and re-launches the session, ignoring the user's explicit quit.
   *Fix direction: Check the exit code in `_attached` and do not compact/restart if the child exited cleanly before it could be killed.*

4. **Severity 1 (Correctness): Provider vocabulary invariant violation**
   `src/multiagents/transcripts.py:138`
   `_usage_of` hardcodes the dictionary keys `"message"` and `"usage"` which are specific to Claude Code's transcript format. This will break on providers with different transcript schemas, violating the contract that provider-specific vocabulary must not be hardcoded in Python.
   *Fix direction: Delegate usage extraction to the provider script or `providers.yaml`.*

5. **Severity 3 (Simplification): Repeated config traversal on every tool call**
   `src/multiagents/server.py:224`
   `_context_reading` calls `_launched_spec(run.config, ...)` on every single MCP tool call to resolve the provider. While checking the token count is cheap due to the `_readings` cache, repeatedly traversing the config tree to resolve the agent spec is unnecessary repeated work.
   *Fix direction: Cache the resolved `provider` or `spec` on the `Runner` instead of looking it up every time.*

6. **Severity 3 (Simplification): Duplicated parsing of transcript JSON**
   `src/multiagents/defaults/providers/claude.sh:545`
   `claude.sh` uses an inline Python script to parse the JSON transcript looking for `compact_boundary`, duplicating the schema knowledge (e.g., `type == 'system'`) that conceptually belongs in the Python layer alongside `transcripts.py`.
   *Fix direction: Expose a helper in the CLI or Python layer to extract compaction stats instead of parsing it in bash.*

7. **Severity 4 (Style): Shell quoting / echo interpretation in error output**
   `src/multiagents/defaults/providers/claude.sh:523`
   `echo "compact failed: $(printf '%s' "$out" ...)"` relies on `echo`. On shells like `dash`, `echo` interprets backslash escapes (e.g., `\n` or `\033` from the CLI's output), mangling the reported error message.
   *Fix direction: Use `printf 'compact failed: %s\n'` instead of `echo`.*

VERDICT(rejected, 4): four correctness defects, the first blocking

--- review.md on branch ---
## Findings

1. **Severity 1 (Correctness/Data Loss): Terminal left dirty on SIGTERM**
   `src/multiagents/driver.py:204` (and `cli.py` cbreak picker)
   `_run_attached` swallows `SIGINT`, `SIGQUIT`, and `SIGHUP`, but fails to handle `SIGTERM`. If the orchestrator receives a `SIGTERM` (e.g., system shutdown or user `kill`), it dies immediately without executing the `finally` block, leaving the user's terminal wedged in raw/cbreak mode.
   *Fix direction: Handle `SIGTERM` explicitly (e.g., raising `SystemExit`) so the `finally` block executes.*

2. **Severity 1 (Correctness): Leaked provider processes on exception**
   `src/multiagents/scripts.py:186`
   `run_action` uses `child.communicate()` without a `finally` block or context manager. If an asynchronous exception like `KeyboardInterrupt` occurs while waiting for the provider script (e.g., during a slow `compact` probe), the exception propagates out and the running child process is leaked in the background.
   *Fix direction: Wrap the `Popen` lifecycle in a `finally` block or `with child:` to ensure it is killed and waited on.*

3. **Severity 1 (Correctness): Race condition overrides deliberate CLI exit**
   `src/multiagents/driver.py:810`
   If the child CLI exits cleanly (e.g., the user types `/quit`) exactly as the compaction grace period expires, `stalled()` returns `True` and sets `compaction.requested`. `_run_attached` returns the clean exit code 0, but `_attached` sees the requested compaction and re-launches the session, ignoring the user's explicit quit.
   *Fix direction: Check the exit code in `_attached` and do not compact/restart if the child exited cleanly before it could be killed.*

4. **Severity 1 (Correctness): Provider vocabulary invariant violation**
   `src/multiagents/transcripts.py:138`
   `_usage_of` hardcodes the dictionary keys `"message"` and `"usage"` which are specific to Claude Code's transcript format. This will break on providers with different transcript schemas, violating the contract that provider-specific vocabulary must not be hardcoded in Python.
   *Fix direction: Delegate usage extraction to the provider script or `providers.yaml`.*

5. **Severity 3 (Simplification): Repeated config traversal on every tool call**
   `src/multiagents/server.py:224`
   `_context_reading` calls `_launched_spec(run.config, ...)` on every single MCP tool call to resolve the provider. While checking the token count is cheap due to the `_readings` cache, repeatedly traversing the config tree to resolve the agent spec is unnecessary repeated work.
   *Fix direction: Cache the resolved `provider` or `spec` on the `Runner` instead of looking it up every time.*

6. **Severity 3 (Simplification): Duplicated parsing of transcript JSON**
   `src/multiagents/defaults/providers/claude.sh:545`
   `claude.sh` uses an inline Python script to parse the JSON transcript looking for `compact_boundary`, duplicating the schema knowledge (e.g., `type == 'system'`) that conceptually belongs in the Python layer.
   *Fix direction: Expose a helper in the CLI or Python layer to extract compaction stats instead of parsing it in bash.*

7. **Severity 4 (Style): Shell quoting / echo interpretation in error output**
   `src/multiagents/defaults/providers/claude.sh:523`
   `echo "compact failed: $(printf '%s' "$out" ...)"` relies on `echo`. On shells like `dash`, `echo` interprets backslash escapes (e.g., `\n` or `\033` from the CLI's output), mangling the reported error message.
   *Fix direction: Use `printf 'compact failed: %s\n'` instead of `echo`.*

VERDICT(rejected, 4): four correctness defects, the first blocking


## reviewer ag-bf6004

Here is the review of the D3 diff on branch `agents/implementer-deep/149e0a`.

## Findings

1. `src/multiagents/executor/docker.py:612` (in `wrapper`)
   - **Severity 1 (Correctness/Blocking)**
   - **Defect**: `os.setsid()` throws a `PermissionError` inside the container wrapper because `docker exec` processes are already session leaders.
   - **Concrete failure**: When the probe runs, the wrapper crashes immediately with `PermissionError: [Errno 1] Operation not permitted`, causing the target binary to never execute and the probe to fail unconditionally.
   - **Fix suggestion**: Remove `os.setsid()` (the process is already the process group leader) or wrap it in `try: os.setsid() except PermissionError: pass`.

2. `src/multiagents/executor/docker.py:625` (in `cleanup`)
   - **Severity 2 (Correctness)**
   - **Defect**: The one-liner `cleanup` script has a time-of-check to time-of-use (TOCTOU) race condition where it calls `int(open(p).read())` on a potentially empty file.
   - **Concrete failure**: The `wrapper` opens the pidfile with `O_CREAT` before writing to it. If the cleanup script runs in that split second, `int("")` raises `ValueError`, crashing the cleanup and leaving the container process permanently leaked. 
   - **Fix suggestion**: To keep it a test-friendly one-liner without list comprehensions, handle the empty string gracefully: `pid = open(p).read().strip() if os.path.exists(p) else ""; os.killpg(int(pid), signal.SIGKILL) if pid else None; os.unlink(p) if os.path.exists(p) else None`.

3. `src/multiagents/executor/docker.py:637` (in `exec_in_running`)
   - **Severity 2 (Correctness)**
   - **Defect**: Missing cleanup for exceptions other than `TimeoutExpired` during `child.communicate()`.
   - **Concrete failure**: If `communicate()` raises `KeyboardInterrupt` or `OSError`, the `child` process is left running indefinitely and its pipes leak because there is no `finally` block or `except Exception:` to reap the process.
   - **Fix suggestion**: Add a `finally` block that calls `child.kill()` and `child.communicate()` to ensure process reaping and pipe closure on all exception paths.

4. `src/multiagents/manifest.py:919` (in `_shipped_providers`)
   - **Severity 3 (Correctness)**
   - **Defect**: `_shipped_providers` swallows `yaml.YAMLError` and silently returns an empty dictionary.
   - **Concrete failure**: If the shipped `providers.yaml` introduces a syntax error, the parser silently disables the "overridden" state checks instead of reporting a malformed configuration.
   - **Fix suggestion**: Catch `OSError` to return `{}` for missing files, but let `yaml.YAMLError` propagate or log a warning.

5. `src/multiagents/manifest.py` (in `_shell_index`)
   - **Severity 3 (Fragility)**
   - **Defect**: Shell script parsing uses fragile line-by-line regexes instead of an AST parser, strictly assuming functions and `case` arms start at column 0.
   - **Concrete failure**: A perfectly valid bash script that formats an arm like `  pattern)` or puts `esac` on the same line will fail to parse, resulting in false negatives for `.sh::case/fn` validation.
   - **Fix suggestion**: Rely on `shfmt --to-json` to extract AST nodes instead of manual regex matching.

6. `src/multiagents/manifest.py` (overall structure)
   - **Severity 4 (Structure/Duplication)**
   - **Defect**: The file violates separation of concerns by combining static JSON/AST validation, regex parsing, and dynamic docker/local execution into one massive 1,220-line file, leading to duplicated state machine logic.
   - **Concrete failure**: The probe evaluation state assignments (`timeout`, `missing`, `probe_failed`) and `_extract_version` calls are duplicated almost identically across both the docker and host execution paths.
   - **Fix suggestion**: Extract dynamic execution and probe state logic into a separate `prober.py` module, leaving `manifest.py` focused strictly on data structures and schema validation.

VERDICT(rework, 5): six defects found, five insisted upon, the first blocking
