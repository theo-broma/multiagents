# C1 — Executor selection and the agent environment

Findings from characterizing `get_executor`/`executor_for`, `DockerExecutor`
argument construction (`mounts`, `run_args`), `build_env`, and `prepare_home`.
Every finding below was confirmed by calling the real production code through
`tests/support/c1_harness.py` against a temp directory — see
`tests/test_c1_executor_characterization.py` for the reproduction named in
each entry. IDs continue from `context/review/C1-sandbox.md` (F1, F2) and
`context/review/C1-sandbox-allowlist.md` (F10–F14); F20+ is reserved for the
parallel auth-proxy characterizer.

**F30** — A read-only `extra_mounts` entry naming the project root is silently downgraded to writable
*Class:* security
*Severity:* high
*File:* `src/multiagents/executor/docker.py` (`DockerExecutor.mounts`)
*Trace:* `mounts()` builds a list `out` that starts with `(self.paths.root,
False)` — the project root, writable — and only afterwards appends whatever
`extra_mounts` the config declares. The de-duplication step that follows,
```python
seen: dict[Path, bool] = {}
for path, read_only in out:
    if path.exists() and path not in seen:
        seen[path] = read_only
```
keeps the **first** occurrence of a given path and silently discards every
later one, with no error and no log line. Because `paths.root` is always
first in `out`, a config author who lists that same absolute path in
`extra_mounts` with `read_only: true` — for instance to try to protect the
project root while still exposing it, or through a config-generation bug that
happens to re-list a path already covered by a built-in mount — gets a
writable mount, not the read-only one they asked for. The dict is keyed by
path and the loop never revisits an already-seen key, so there is nothing
downstream that would catch the discrepancy either.
*Evidence:* `tests/test_c1_executor_characterization.py::test_extra_mounts_read_only_is_silently_dropped_when_path_collides_with_root` —
constructs a real `DockerExecutor` with
`extra_mounts=[{"path": str(tmp_path), "read_only": True}]` where `tmp_path`
is also `paths.root`, calls the real `.mounts()`, and confirms the result is
`(root, False)`. `test_duplicate_extra_mounts_entries_first_one_wins` confirms
the same first-wins rule holds for two `extra_mounts` entries naming the same
path with conflicting `read_only` values, independent of the root collision.
The same mechanism runs the other way too:
`test_extra_mounts_wins_over_a_colliding_cli_binary_mount_because_it_comes_first_in_out`
shows an `extra_mounts` entry naming the same absolute path as a provider's
CLI binary (normally mounted read-only, appended to `out` *after*
`extra_mounts`) can turn that binary's mount point writable, because
`extra_mounts` is checked first in `out` and therefore wins the same
first-occurrence dedup. So "first wins" is genuinely iteration-order
dependent in both directions, not just "built-ins beat config" or "config
beats built-ins" — a config author has no consistent rule to reason about
without reading `mounts()`'s source.
*Reasoning:* This is the general shape the C1 review keeps finding: a control
that reads as "read-only wins" or "the narrower mount wins" in the surrounding
code (the config-mount comment a few lines below this exact loop says as much
about `paths.config`) but is actually governed by iteration order, which is
an implementation detail no config author can be expected to reconstruct.
Nothing in `mounts()`, `run_args()`, or the config loader validates that two
`extra_mounts` entries (or an entry and a built-in mount) do not collide, so
the failure mode is silent rather than a rejected config.
*Recommendation:* When two entries in `out` resolve to the same path with
different `read_only` values, either raise (config error, safest — this is
almost certainly not what the author intended) or take the *most restrictive*
value (`read_only = True` wins over `False`) instead of first-wins. At minimum
log a warning naming the dropped mount.

**F31** — Built-in mounts (root, worktrees, homes) are dropped with no warning if the directory doesn't exist yet when `mounts()` runs
*Class:* correctness
*Severity:* medium
*File:* `src/multiagents/executor/docker.py` (`DockerExecutor.mounts`)
*Trace:* The same `path.exists()` guard used for `extra_mounts` also applies
to the three built-in mounts (`paths.root`, `paths.worktrees`,
`paths.homes`) — there is no separate code path that treats these three as
mandatory. `paths.worktrees` and `paths.homes` live under the machine-wide
state root (`state_root()/worktrees/<slug>`,
`state_root()/homes/<slug>`, per `src/multiagents/paths.py`), not under the
project root, and are not created merely by constructing a `ProjectPaths`
object — something else in the codebase has to `mkdir` them first. If
`run_args()` (and therefore `docker run`) executes before that `mkdir` has
happened, the container is created with neither mount, and per the
docstring on `DockerExecutor.stale_mounts`/`mount_drift`
("Bind mounts are fixed when a container is CREATED... the SET of mounts is
whatever was decided at creation, so a configuration change reaches a
long-lived container only when it is replaced"), that container never gains
the mount later even after the directories exist — only `docker rm && up`
would fix it.
*Evidence:* `tests/test_c1_executor_characterization.py::test_mounts_with_no_extra_config_and_nonexistent_worktrees_homes_is_root_only`
constructs a real `DockerExecutor` over a fresh `ProjectPaths` and confirms
`.mounts()` returns only `paths.root` — `worktrees` and `homes` are absent —
until `test_mounts_includes_worktrees_and_homes_once_they_exist` `mkdir`s them
first and confirms they then appear. Both observations are of `mounts()`
itself; whether `ensure_running()` is ever actually invoked before whatever
else in the codebase creates those two directories on a real first run is
**not verified here** — that would require tracing call order in the CLI/
runner code, which is outside this task's surface (executor construction
only). This finding pins the mechanism that makes such a race possible, not
a confirmed end-to-end occurrence.
*Reasoning:* If the ordering risk is real on a first-ever spawn for a
project, the consequence is a container permanently missing its
worktrees or homes mount — silently, since `mounts()` raises nothing and
`ensure_running()`'s only mount-related check (`stale_mounts()`) only runs
against an *existing* container on a later call, not against the one just
created. Flagged at medium rather than high because the actual trigger
condition (mount computed before the directory exists) is unconfirmed outside
this surface.
*Recommendation:* Either have `mounts()` `mkdir(parents=True, exist_ok=True)`
the three built-in paths itself before checking `.exists()` (it already does
exactly this for `private_state()` targets a few lines later in the same
method), or have whatever calls `ensure_running()` guarantee `paths.worktrees`
and `paths.homes` exist first. Cross-reference with a caller-side
characterizer to confirm or rule out the race.

**F32** — `pids_limit`/`cpus`/`memory` of `0` is indistinguishable from "not set"; a negative value is passed to docker unvalidated
*Class:* correctness
*Severity:* low
*File:* `src/multiagents/executor/docker.py` (`DockerExecutor.run_args`)
*Trace:*
```python
for key, flag in (("cpus", "--cpus"), ("memory", "--memory"),
                  ("pids_limit", "--pids-limit")):
    value = self.config.get(key)
    if value:
        argv += [flag, str(value)]
```
`if value:` is a truthiness check, not `is not None`. `pids_limit: 0` (a
config author's plausible attempt to write "no process limit" explicitly, or
an equally plausible attempt to write "no processes allowed") produces no
`--pids-limit` flag at all — identical argv to leaving the key out of the
config entirely. A negative value (`pids_limit: -1`) is truthy in Python and
*is* forwarded as `--pids-limit -1`, with nothing in `run_args()` or
elsewhere on this surface validating it before it reaches `docker run`.
*Evidence:* `tests/test_c1_executor_characterization.py::test_run_args_pids_limit_zero_is_silently_omitted`
and `::test_run_args_pids_limit_negative_is_passed_through_unvalidated` —
both call the real `.run_args()` and inspect the returned argv directly.
*Reasoning:* Low severity because the failure mode is "the limit config
silently does nothing" rather than a boundary widening — a container that
should have been capped simply isn't, which is a correctness gap rather than
a new escape route. Still worth fixing because a `0` is the one value most
likely to be a deliberate, meaningful choice (either extreme) that this code
treats as "absent."
*Recommendation:* Use `if value is not None:` and let docker's own CLI reject
whatever it does not accept (docker itself already rejects
`--pids-limit -1`-style nonsense at the daemon), rather than
multiagents silently swallowing the flag.

**F33** — `build_env`'s `blocked` list only guards the `passthrough` loop; the always-on base environment (`PATH`, `USER`, `TERM`, ...) is forwarded regardless
*Class:* correctness
*Severity:* low
*File:* `src/multiagents/executor/base.py` (`build_env`)
*Trace:* `build_env` has two separate loops. The first, over `BASE_ENV_KEYS`,
unconditionally forwards any of `PATH, LANG, LC_ALL, LC_CTYPE, TERM, TZ,
TMPDIR, SHELL, USER` that are set in the calling process's environment, with
no reference to `blocked` at all. Only the second loop, over the caller's
`passthrough` list, checks `key in blocked`. A caller that includes one of
the `BASE_ENV_KEYS` names in `blocked` — expecting deny-by-default to mean
"nothing named here reaches the child" — does not get that: the name still
arrives via the first loop.
*Evidence:* `tests/test_c1_executor_characterization.py::test_build_env_base_keys_are_forwarded_even_if_named_in_blocked`
calls the real `build_env(passthrough=[], blocked=["PATH"], home=None,
identity={})` with `PATH` set in the environment and confirms it is present
in the result.
*Reasoning:* Low severity because the module's own comment states
`BASE_ENV_KEYS` are chosen specifically because "None of them carry
credentials," so in the one caller this project ships (provider config)
there is probably never a reason to put one of these nine names in `blocked`.
Flagged anyway because the function's docstring — "Deny-by-default: the
child starts with nothing and receives only what is named" — reads as a
blanket guarantee that `blocked` overrides, and it does not for this one
category of variable.
*Recommendation:* Either apply the same `key in blocked` check to the base
loop for consistency, or narrow the docstring to say explicitly that
`BASE_ENV_KEYS` are exempt from `blocked` by design.

**F34** — `prepare_home` raises an uncaught `FileExistsError`/`PermissionError` if the target path is a pre-existing file, or its parent isn't writable
*Class:* correctness
*Severity:* medium
*File:* `src/multiagents/executor/base.py` (`prepare_home`)
*Trace:* `prepare_home` opens with `home.mkdir(parents=True,
exist_ok=True)`. `exist_ok=True` only tolerates an already-existing
*directory* at that path — if `home` already exists as a regular file,
`mkdir` raises `FileExistsError`, which nothing in `prepare_home` (or its
caller-facing contract, an unadorned `-> Path | None` return type) catches.
Likewise, if `home`'s parent directory exists but is not writable by the
calling user, `mkdir` raises `PermissionError`, also uncaught. Every other
per-file operation later in the function (`target.symlink_to`,
`shutil.copytree`/`copy2`) is wrapped in `try/except OSError: pass`, but this
first `mkdir` call is not.
*Evidence:* `tests/test_c1_executor_characterization.py::test_prepare_home_target_already_a_file_raises_file_exists_error`
and `::test_prepare_home_unwritable_parent_raises_permission_error` both
call the real `prepare_home()` and confirm the uncaught exception directly
(the latter skips under uid 0, where permission bits don't apply).
*Reasoning:* Medium rather than low because this is the one call in the
function with no defensive wrapper at all, set against a function whose
entire remaining body is deliberately permissive about pre-existing state
(idempotent symlinks, skip-if-exists copies, leave-alone `.gitconfig`). A
stale or manually-created file at an agent's home path — plausible after an
interrupted run, a manual cleanup gone wrong, or a config change to `policy`
that reuses a path some other tool already touched — turns "prepare this
agent's home" into an unhandled crash instead of the same
silently-permissive behaviour the rest of the function shows for existing
state.
*Recommendation:* Either guard the initial `mkdir` the same way as the rest
of the function (catch, and either proceed treating the file as inert or
raise a clearer, named error such as `"agent home <path> exists and is not a
directory"`), or document explicitly that this is the one precondition
`prepare_home` assumes rather than checks.

**F35** — `extra_mounts` string-form entries can never be marked read-only, and relative paths are never resolved
*Class:* correctness
*Severity:* low
*File:* `src/multiagents/executor/docker.py` (`DockerExecutor.mounts`)
*Trace:* `mounts()` accepts two shapes per `extra_mounts` entry: a bare
string, always turned into `(Path(entry).expanduser(), False)` — writable,
with no way to opt into read-only — or a dict with a `"path"` key, which is
the only shape that can set `read_only`. Separately, `Path(entry).expanduser()`
only expands a leading `~`; it does not call `.resolve()`, so a relative
string (`"relative-dir"`) stays relative all the way through `mounts()` and
into the `-v {source}:{path}` flag `run_args()` builds — a value that would
be interpreted relative to whatever directory the `docker` client process
happens to be running in at `docker run` time, not the project root.
*Evidence:* `tests/test_c1_executor_characterization.py::test_extra_mounts_string_form_can_never_be_read_only`
and `::test_extra_mounts_relative_path_is_not_resolved_to_absolute` — both
against the real `.mounts()`.
*Reasoning:* Low severity: nothing here widens a boundary by itself (a
string-form mount defaults to the *more* permissive option, which is at
least consistent with "you must opt into read-only," and a relative path is
far more likely to break the mount outright — fail closed — than to resolve
to something unintended). Flagged because both are surprising enough to a
config author that they are worth being explicit about rather than left as
an implicit consequence of `Path.expanduser()`'s scope.
*Recommendation:* Document (in `project.yaml`'s schema comments, or
wherever `extra_mounts` is described to a config author) that string entries
are always writable and that relative paths are not resolved against the
project root — or resolve them (`Path(entry).expanduser().resolve()`) so the
generated `-v` flag always contains an unambiguous path regardless of the
docker client's own working directory.
