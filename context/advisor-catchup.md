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

## Pending, for the advisor to weigh in on when back

- Whether CI-R6's single-read review was enough, or whether it should get an
  adversary pass.
- The CI-R5 implementation, once built: the end-of-run relaunch path, which
  is its advice #3, the `_finalize` relaunch pattern.
