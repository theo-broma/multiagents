# Advisor catch-up log

Status: open, 2026-09-27. The user decided that agy is not to be used while
its credit is low, and that includes the advisor (ag-25c350, on agy). Instead,
this file records every decision the advisor would normally have seen, with
enough detail to put to it later in a single consult. Each entry is
self-contained, and says what was decided, why, and where it lives.

The advisor last saw: the commit-identity contract at e2dbe19, CI-R4/R5/R6 as
first written. Its turn 19 reply produced the decisions in 9e27280.

## Since the advisor's last turn

1. **9e27280, CI-R5/R6 amendments (from its own advice):**
   - one `result.json` after the loop;
   - `limited` wins over a commit failure;
   - each fix turn has its own bound, `limits.commit_fix_timeout` (300 s);
   - an orchestrator stop or steer ends the loop;
   - the user's `GIT_CONFIG_*` is preserved.

   Declined: the 60 s fix-turn timeout it proposed, as too short for an LLM
   fixing lint.
2. **470402f, CI-R6 decisions after tester ag-bccbf0:**
   - the override wins over a user env entry that asks for signing;
   - an agent's own `git merge --no-ff` counts, and is unsigned;
   - not specified: `-S`, `tag.gpgsign`, `commit-tree`/`rebase`, `restore_paths`;
   - the docker executor is verified by reading the code only.
3. **ed3696c, CI-R6 implemented (ag-ecd0ec, sonnet):**
   - `gitops.commit_all` adds `-c commit.gpgsign=false`;
   - `executor/base.py:build_env` appends `'commit.gpgsign'='false'` to
     `GIT_CONFIG_PARAMETERS`, which outranks `GIT_CONFIG_COUNT`; the last
     entry wins;
   - docker gets it through the same env dict, via `--env-file`.

   Merged after my own read of the 24-line diff. No adversary and no
   reviewer. To check: does anything else call `build_env` for a process that
   should sign, such as orchestrator-side tooling? Can a quoted value in a
   user's `GIT_CONFIG_PARAMETERS` break the append?
4. **df5ed22, CI-R5 red suite (tester ag-c057bd, 24 tests, 14 red):**
   - fake CLI with `--resume`;
   - a real hook blocking on a `BLOCK` file.
5. **eeb376f, decisions on CI-R5 silences:**
   - the event field is `attempt`, counted from 1;
   - a steered run gets a fresh loop;
   - a fix turn cut by `commit_fix_timeout` counts as an attempt and keeps
     the original status;
   - new CI-R2 gap: `commit_all` ignores a failed `git add -A` (with
     `index.lock` held it reports "nothing to commit"). That is to be
     reported as `commit_failed`.

6. **5558946, CI-R4 implemented (ag-63bfb0, sonnet, 4 lines):** the result
   text keeps the first 500 characters of the git output plus
   ` [truncated]`. It keeps the head, not the tail, and hook errors are often
   at the tail; the contract allows either. Merged after my own read.

7. **721642e, tests (tester ag-32983e):** the CI-R2 `git add` gap, plus CI-R5
   amendments pinning the `attempt` field, the cut-off fix turn and the fresh
   loop on steer.
8. **CI-R5 built by implementer-deep ag-b7c2ff (opus), commits 921dfe1 and
   eb88ab2 plus a follow-up after a steer:**
   - Hook detection: an executable `pre-commit`, `prepare-commit-msg` or
     `commit-msg` hook exists, and a `GIT_TRACE2_EVENT` trace shows a hook
     child exiting non-zero. On an old git, the hook's presence alone decides.
   - The loop runs inside the original run's `_finalize`, via
     `_launch(..., done=run.done)`, which is your advice #3.
   - The loop kills an overrunning fix turn itself, because the watchdog only
     marks a run `stuck`.
   - Decisions: a `quota` or `unauthenticated` status from a fix turn is
     propagated, and a failed run is still eligible for the loop.
   - To check: the trace-based detection; the reliability of the
     kill-at-timeout; the run's `done` never being set on a steer during a
     fix turn.

9. **Adversarial pass (tester ag-6ceb2b, since the adversary's providers are
   down or excluded): 2 defects.**
   - HIGH: a hook swaps the `GIT_TRACE2_EVENT` file for a FIFO, and the
     runner freezes. This became CI-R7.
   - MEDIUM: a `NEED_DECISION` in a fix turn is dropped.

   Both fixes were delegated to ag-c2af1d. CI-R5 was merged (989621d) with
   these two known defects, so that the fix could build on it.
10. **A security problem found in passing:** the whole project root, `.git`
   included, is mounted writable, so agent-written hooks and config run on
   the host. See `context/specs/sandbox-git.md`. It is waiting on the user.
   Your view on the three directions is wanted.

11. **cb7e50d, CI-R7 and the fix-turn NEED_DECISION (ag-c2af1d):**
   - the trace is read with O_NONBLOCK and O_NOFOLLOW, regular files only,
     at most 4 MB, and otherwise falls back to the hook's presence;
   - `commit_all` runs through `asyncio.to_thread`;
   - a question in a fix turn parks the run `awaiting_user`.

   27d2fe4: the tester fixed its own helper, which waited only for terminal
   statuses.
12. **The user approved sandbox-git.** The contract SG-R1..R6 is in
    10826c4, written without you. Please review it for silences, above all:
    - SG-R3, which moves agent commits into the executor;
    - SG-R5, where the base's hooks run on a merge, and a hook that runs the
      tests over the merged tree is accepted as a deliberate act;
    - whether SG-R4, pinning GIT_DIR, GIT_COMMON_DIR and GIT_WORK_TREE, is
      sufficient.

13. **83e6bb0, SG-R2/R6 (ag-ab5211):**
   - `project_mounts()`: the root is read-only, with nested writable and
     read-only mounts beneath it;
   - an extra writable mount on `.git/refs`, so `refs` cannot be renamed
     aside;
   - `protect_project()`, which runs before any docker call: it refuses a
     `.git` file, creates missing protected paths, builds the index with
     `read-tree --empty`, and unpacks the base branch by lock-and-rename.

   Known limits: file mounts freeze, so the container can see a stale main
   HEAD, index or config after the host replaces them; protection starts
   only at recreation. 12 old tests asserted a writable root, and the tester
   (ag-a32e5e) is updating them. To check: whether freezing single-file
   mounts matters for nested orchestration.

14. **38b9e98, SG-R4/R5 (ag-9e10d8):**
   - Pinned reads work through a private temporary git dir: `HEAD` and
     `index` are copied, and `commondir` is written to `<root>/.git`. It
     uses `GIT_INDEX_FILE`, a hooksPath pointing at an empty dir,
     fsmonitor off and `GIT_OPTIONAL_LOCKS=0`.
   - The merge copies an in-tree hooks dir aside, and `reset --hard` cleans
     up a refused merge.
   - The implementer also pinned `server.git_status` and the monitor
     snapshot, which I kept.
   - Husky v9 `.husky/_` is a documented limitation.
   - Old tests updated for SG-R2 (ag-a32e5e).

   On the host, the docker-live run after R2/R4/R5: the 4 SG-R2 live tests
   pass, and the 3 SG-R3 tests fail, since that work is in flight as
   ag-ce8b54.

15. **SG-R3 (4b9dbda, ag-ce8b54):** the `gitops.Git` seam, with `HOST` for
    the local executor and `ContainerGit` running `docker exec` as the agent,
    carrying the agent's env file and a trace under the run dir.

    Follow-ups merged:
    - dd12859 (ag-3c75c1): the index cap is 128 MiB, and a FIFO, symlink or
      git failure raises `GitError`; `objects/info` is read-only;
      `protect_project` uses no-follow primitives and `_plain` checks.
    - edccaad (ag-b65538): the runner's poll and refresh are pinned;
      `reset_keep` runs pinned with filters off; SG-R7 env files use
      `_write_beneath` and `_read_beneath`; a watchdog inside the container
      handles the exec timeout by walking `/proc`, because the image has no
      `pkill`.

    On the host, docker-live passes 7/7 at edccaad. Twice, the safety
    classifier stopped testers given an "adversary stand-in" framing;
    narrow contract-test framing worked. Worth your view on: `reset_keep`
    running on the host rather than in the container; the filters-off
    trade-off for LFS; the `/proc` watchdog.

## Pending, for the advisor to weigh in on when back

- Whether CI-R6's single-read review was enough, or whether it should get an
  adversary pass.
- The CI-R5 implementation, once built: the end-of-run relaunch path, which
  is its advice #3, the `_finalize` relaunch pattern.
