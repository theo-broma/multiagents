# Handoff — C1 executor selection & agent environment characterizer

Interrupted before any test file or findings file was written (quota cutoff).
No files existed yet for this task; this handoff is the only artifact. Next
run should treat this as the starting point, not re-derive it.

## Status

**Nothing committed to `tests/test_c1_executor_characterization.py` or
`context/review/C1-sandbox-executor.md` yet — both still need to be created.**
All work so far was reading `src/multiagents/executor/{__init__,base,docker,local}.py`
and `tests/support/c1_harness.py`. Findings below are from reading only —
**none have been run/confirmed yet**. Treat every one as a hypothesis to
verify with the harness before writing it up as `F<n>` (per the
characterizer's own honesty rule: never assert something not observed).

## What I was about to test, in priority order

1. **`get_executor`** — `"local"` ignores config/paths/providers; `"docker"`
   passes them through; unknown/empty-string/`None` kind all fall through to
   `raise ValueError(f"Unknown executor kind {kind!r}...")` (checked by
   reading — `kind == "local"` / `kind == "docker"` string comparisons, so
   `None` just fails both and raises, doesn't crash on `.lower()` or similar).
   Need to actually run this to confirm no earlier TypeError.

2. **`DockerExecutor.network` vs `network_mode` — naming trap worth pinning.**
   `network` property returns the docker network *name*
   (`multiagents-net-{slug}`). `network_mode` property reads
   `config.get("network", "allowlist")` — i.e. the config key literally named
   `"network"` controls what `network_mode` returns, NOT what `.network`
   returns. Two same-ish-named properties, different jobs. Worth a
   characterization test spelling this out explicitly since it's an easy
   thing for a future reader/editor to get backwards.

3. **`mounts()` dedup — likely a real widen-the-boundary finding, HIGH
   PRIORITY to verify first:**
   ```python
   seen: dict[Path, bool] = {}
   for path, read_only in out:
       if path.exists() and path not in seen:
           seen[path] = read_only
   ```
   `out` starts with `(self.paths.root, False)` (writable) before
   `extra_mounts` is appended. So if a user's `extra_mounts` config names the
   *same path* as `paths.root` with `read_only: true`, the dict-insertion
   guard (`path not in seen`) means the **first** occurrence wins and the
   later read-only declaration is silently dropped — the path stays writable.
   This needs an actual harness test: build a `DockerExecutor` with
   `extra_mounts=[{"path": str(tmp_path), "read_only": True}]` where
   `tmp_path` is also `paths.root`, call `.mounts()`, assert the result is
   `(root, False)` not `(root, True)`. If confirmed, this is a real F<n> —
   "read_only in extra_mounts config is silently ignored when the path
   collides with a built-in writable mount."

4. **`extra_mounts` string-form entries can never be read-only** — only the
   dict form (`{"path":..., "read_only": true}`) can set `read_only`; a bare
   string entry always becomes `(Path(entry).expanduser(), False)`. Worth
   pinning as behavior (not necessarily a bug — may be intentional shorthand
   — but flag if it surprises).

5. **Relative paths in `extra_mounts` are NOT resolved to absolute** —
   `Path(entry).expanduser()` only expands `~`, does not `.resolve()`. A
   relative entry stays relative through `mounts()` and into `run_args()`'s
   `-v {source}:{path}` flag, which would be nonsensical to a real `docker
   run` (untested — docker not on PATH here) but is observable in the argv
   string itself. Pin the argv shape.

6. **Nonexistent extra_mount paths are silently dropped** (the
   `path.exists()` guard in the dedup loop) — no error, no warning. Confirm
   and pin.

7. **Duplicate mounts across groups** (e.g. an extra_mount pointing at the
   same absolute path as one of the CLI-binary or home_links mounts) — same
   `seen` dict logic, first-inserted wins. `mounts()` iteration order is
   root/worktrees/homes, then extra_mounts, then CLI/home_links (if
   `mount_cli_from_host`), so *those* built-ins always win over anything a
   config-writer tries to override via extra_mounts. Confirm with a test.

8. **Path containing `:` or spaces** — no validation anywhere in `mounts()`
   or `run_args()`. A path with `:` would corrupt the `-v` flag's
   `source:dest[:ro]` syntax when docker parses it (unreachable to actually
   run here, but pin the constructed argv string showing the ambiguity).

9. **`run_args()` — `--init` flag: grep confirms `docker.py` never emits
   `--init` anywhere in `run_args()`. Pin this directly** (a ticket already
   turns on it per the task description) — a simple test asserting
   `"--init" not in ex.run_args()` for various configs. This is cheap, do it
   first thing next run.

10. **`pids_limit`/`cpus`/`memory` handling**: `for key, flag in (...): value
    = self.config.get(key); if value: argv += [flag, str(value)]` — note
    `if value:` (truthy check, not `is not None`). So `pids_limit=0` is
    **falsy** and silently omitted (no `--pids-limit 0` flag at all) — same
    for `cpus=0`/`memory=0`. A negative value (`pids_limit=-1`) IS truthy in
    Python so it WOULD be passed through as `--pids-limit -1` with no
    validation — docker would presumably reject it, but multiagents does not
    guard against it. This is a good pin + possible finding (negative/zero
    limits treated inconsistently — zero silently means "no limit
    requested", not "limit to zero", which may or may not be what a config
    author expects).

11. **`build_env`** — deny-by-default base env (`BASE_ENV_KEYS`), then
    `passthrough` entries: `KEY=value` sets a literal, bare `KEY` forwards
    `os.environ.get(KEY)`. `key in blocked` check happens on the *stripped
    key* — verify: `key.strip()` before the `in blocked` check, so blocked
    matching is exact-string (no whitespace tolerance issues) — pin. Also
    check: what does a passthrough entry whose value contains `=` do, e.g.
    `"FOO=bar=baz"`? `key, sep, literal = key.partition("=")` — `partition`
    splits on the *first* `=` only, so `literal = "bar=baz"` — the rest of
    the `=` characters stay in the value. Confirm and pin — this is the
    "contains an `=` in its value" case the task calls out explicitly.
    Unset-var passthrough (`os.environ.get(key)` returns `None`) → key
    skipped (the `if value is not None` guard). Empty-string env var
    (`FOO=` in the real environment, forwarded via bare `FOO`) → `value = ""`
    which passes the `is not None` check, so an *empty string* IS forwarded
    and appears in the child env as `FOO=""`. Pin this distinction
    (unset → omitted, empty → forwarded-as-empty) — likely surprising and
    worth a test each.

12. **`prepare_home`** — returns `None` immediately for `policy != "per-agent"`
    (need to check what `build_env`/callers do with `home=None` — already
    covered: `build_env` only sets HOME/XDG vars `if home is not None`).
    For `per-agent`: creates `home`, chmods `0o700` (swallows `OSError`).
    symlink loop: `if target.is_symlink() or target.exists(): continue` — so
    calling `prepare_home` twice on an existing target is idempotent/no-op,
    does NOT re-link or update. If target exists as a *regular file*
    (not symlink, not what the source is), it's still skipped silently — no
    error, no overwrite. Confirm: what if `home` itself already exists as a
    *file* (not a directory) when `prepare_home` is called —
    `home.mkdir(parents=True, exist_ok=True)` would raise `FileExistsError`
    (not caught) since `exist_ok=True` only tolerates an existing directory,
    not an existing file at that path. This is an uncaught-exception path —
    good boundary test, task explicitly asks for "target already exists, is
    a file, or is not writable."
    Not-writable target (parent dir chmod 0o500 before calling) — mkdir
    would raise `PermissionError`, uncaught. Confirm.
    `copies` loop: `if not source.is_file() or target.exists(): continue` —
    wait, actually the code reads `if not source.exists() or target.exists():
    continue` for copies (re-check exact source — I read `source.is_file()`
    earlier for the *first* filter inside `container_private_seed` handling
    in docker.py, that's a DIFFERENT method (`_copy_settings`/`seed_private_state`
    in docker.py) — do NOT conflate with `prepare_home`'s own `copies` loop
    in base.py, which uses `source.exists()`. Re-read base.py's `prepare_home`
    copies loop carefully next run before writing the test — I had it open
    but did not double check which existence check it uses; base.py:222-ish
    in the copy above shows `if not source.exists() or target.exists():
    continue` — that's confirmed from the Read tool output already captured
    above in this transcript, so it's fine, just flagging to re-verify against
    the file directly rather than from memory.
    gitconfig: only written `if not gitconfig.exists()` — so a pre-existing
    `.gitconfig` under a reused home dir is left untouched, even a garbage
    one. Pin.

## Files to create (none exist yet)

- `tests/test_c1_executor_characterization.py` — new file, add
  `sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))`
  (or copy harness's own sys.path line) then `import c1_harness as h`.
  Use `h.make_docker_executor(tmp_path, providers=..., **config)` for
  DockerExecutor construction (no I/O until a method is called — safe for
  `run_args()`, `mounts()` without docker on PATH). Use `h.LocalExecutor`,
  `h.get_executor`, `h.executor_for`, `h.build_env`, `h.prepare_home`,
  `h.ProjectPaths` directly (all re-exported).
- `context/review/C1-sandbox-executor.md` — findings, **ids F30 onwards**
  (stay in this range — parallel characterizers own F1-F29ish, check
  `context/review/C1-sandbox.md` current max before numbering; F1/F2 are
  already taken there by the harness-builder).

## Concrete next steps (in order)

1. Read `context/review/C1-sandbox.md` again to confirm current max F-id
   (F1, F2 confirmed taken; ledger.yaml may have more — check
   `context/review/ledger.yaml` for the running max across ALL contexts,
   not just C1, since IDs are project-global based on the numbering
   instructions).
2. Write the test file scaffold with `sys.path`/import block first, run
   `pytest --collect-only` on it to make sure the harness imports cleanly.
3. Work through items 1–12 above in order — each becomes one or a few small
   `def test_...():` functions using `h.make_docker_executor` /
   `h.build_env` / `h.prepare_home` directly, asserting on observed argv /
   dict / filesystem state (not on what "should" happen).
4. For each behavior that looks like a boundary-widening surprise (items 3,
   8, 10, 11's `=`-in-value case, 12's uncaught-exception paths), write the
   test AND a corresponding `F<n>` entry in the findings file, cross-
   referenced by test name, following the exact format already used in
   `context/review/C1-sandbox.md` (F1/F2 above) — `Class`, `Severity`,
   `File`, `Trace`, `Evidence`, `Reasoning`, `Recommendation`.
5. Run `uv run --frozen python -m pytest -q tests/test_c1_executor_characterization.py`
   and iterate — ignore the 17 unrelated `can_spawn is false` failures
   elsewhere in the suite (that's `test_core.py`, not this file, per the
   task's own environmental note).
6. Commit, then write the `## Result` summary the task asks for (WRONG-
   looking pins first with their F<n>, then blind spots — the biggest
   expected blind spot is anything actually shelling out to `docker`:
   `ensure_running`, `ensure_proxy`, `ensure_auth_proxy`'s real `docker run`
   calls, `list_containers`, `docker_state`, `container_state`, etc. — all
   unreachable, `docker` not on PATH in this sandbox, guard with
   `h.docker_available()` exactly like `test_core.py` does).

## Nothing was committed to git yet from this task

`git status --short` was clean before this handoff file — i.e. this
handoff itself is the first change on disk for this task. It will show up
as a new untracked file; commit it.
