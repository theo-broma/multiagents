# Commit identity — an agent's work never silently fails to reach its branch

Status: contract, 2026-09-26. Ids `CI-R1`… are stable; never renumber.

## Why

Found by ag-a29f15: `gitops.commit_all` fails when git has no author
identity (no `user.name`/`user.email` in any config, no `GIT_AUTHOR_*` /
`GIT_COMMITTER_*` in the environment). This is the normal state of a fresh
container HOME. When that happens:
- the agent's changes stay staged in its worktree;
- they never reach the branch;
- nothing reports it.

`merge_agent` then merges a branch without that work.

## Behaviours

**CI-R1 — a commit made by multiagents succeeds without a configured
identity.** When neither git config nor the environment supplies an author
or committer identity, `commit_all` still commits. It uses a fallback
identity naming the agent: name `multiagents <role>` (or `multiagents` when
no role is known), email `<agent_id or "agent">@multiagents.invalid`.
- The fallback applies to that one git invocation only.
- It never writes to any git config file.
- An identity that IS configured (repo, global, or env) is used unchanged.
Verified by: a test in a repo with HOME pointed at an empty dir, no git
identity and no `GIT_*` identity variables. `commit_all` produces a commit
on the branch, authored by the fallback. The same test with an identity
configured shows that identity as the author.

**CI-R2 — a failed commit is reported, never swallowed.** If `commit_all`'s
`git commit` fails for any other reason, the failure reaches the caller as an
error carrying git's stderr. It is not a silent no-op. The run that owned
the commit reports it:
- its result/summary names the failure;
- the node's events record it.
"Nothing to commit" is not a failure.
Verified by: a unit test on `commit_all` with a commit made to fail (e.g. a
failing `pre-commit` hook in the test repo): it raises or returns an error
naming the stderr. Also a test that a clean tree is not an error.

## Decisions, 2026-09-26 (orchestrator, after tester ag-862edc)

- **How `commit_all` learns the role and agent id:** the implementer's
  choice, for example optional keyword arguments. The runner-level test is
  what binds the naming.
- **Partial identity:** the fallback fills only the missing half (name or
  email). A configured half is kept.
- **CI-R2 reporting:** it covers the runner's end-of-run commit. The CLI
  callers already check `result.ok` and are unchanged.
- **Node status:** a failed commit does not by itself change the run's
  status. It is reported in the result text and in an event.

**CI-R3 — every commit multiagents makes has the same fallback.** Added
2026-09-27, after researcher ag-bb371e. CI-R1's fallback applies to every
`git commit` / `commit-tree` that multiagents itself runs, not only
`commit_all`. That includes `gitops.merge`'s squash commit and any
revert-then-commit step of the merge gate. For a merge, the identity is
named after the merging side, `multiagents` / `orchestrator@multiagents.invalid`,
unless one is configured.
Verified by: the existing `tests/test_core.py` merge-gate tests and
`tests/test_c3_lifecycle_harness.py` merge tests pass with no git identity in
the environment (HOME pointed at an empty directory), plus a direct
`gitops.merge` test in a new file.

**CI-R4 — the reported failure is bounded.** Added 2026-09-27, after the
advisor's catch-up review (ag-25c350). When CI-R2 appends the commit failure
to the result text, the git output it carries is truncated to at most 500
characters, with a marker saying it was truncated. The event already
truncates its copy. A hook that prints a thousand lines must not bury the
agent's own answer.
Verified by: a runner-level test where the commit fails with more than
500 characters of stderr. The result text keeps the agent's answer and
carries at most 500 characters of the git output.

## Decisions, 2026-09-27 (orchestrator, after advisor ag-25c350)

- **Fallback identity on orchestrator merges run in a container:** kept.
  CI-R3 says so on purpose, and failing the merge is worse. A user who wants
  their own name on merges configures git or `GIT_AUTHOR_*`.
- **`EMAIL` precedence (`gitops.py`):** correct, kept.
- **Hooks and signing on agent commits:** decided by the user on 2026-09-27.
  Signing is skipped on agent commits (CI-R6). Hooks are kept, and a hook
  failure is fed back to the agent (CI-R5). `--no-verify` is never used.

**CI-R5 — a hook failure is fed back to the agent, a bounded number of
times.** Added 2026-09-27 at the user's request. This covers the runner's
end-of-run commit, when it fails because a git hook refused it
(`pre-commit`, `commit-msg`, ...).
- **Resume:** the runner resumes the same agent session in the same worktree,
  as `steer_agent` does. The message says that the end-of-run commit was
  refused by a hook and carries the hook's output: its tail, at least the last
  4000 characters, not the CI-R4-truncated copy.
- **Retry:** after that turn, the runner tries the end-of-run commit again.
  If the agent committed during the turn and nothing is left, that counts as
  success.
- **Bound:** at most `limits.commit_fix_attempts` resumes. The default is 2,
  and 0 disables CI-R5. When they are used up, or the session cannot be
  resumed (the provider has no resume, or quota), the failure is reported as
  in CI-R2 and CI-R4.
- **Only hook failures:** a failure that is not caused by a hook (identity,
  lock, disk, ...) is never fed back, and goes straight to CI-R2. How a hook
  failure is recognised is the implementer's choice, but a repository with no
  active hook must never trigger a resume.
- **Recorded:** each attempt emits a `commit_fix_attempt` event, with the
  attempt number and a truncated detail. The final outcome is either
  success, or `commit_failed` as in CI-R2.
- **Result text:** it keeps the agent's original answer. It says how many fix
  attempts were made, and whether the commit finally succeeded.
- **Accounting:** the fix turns count toward the run's usage, its budget tag
  and its watchdogs like any other turn. The node stays `running` during
  them.
- **Hooks still apply:** the fix turns are ordinary turns. Read-only paths
  stay read-only, and `--no-verify` is never used.

Verified by: runner-level tests with a real failing `pre-commit` hook and a
scripted or fake agent:
- the hook is satisfied on the first fix turn: commit succeeds, one
  `commit_fix_attempt` event;
- the hook is never satisfied: exactly `commit_fix_attempts` resumes, then
  `commit_failed`;
- `commit_fix_attempts: 0`: no resume;
- a non-hook failure: no resume;
- the resume message carries the hook's output.

**CI-R6 — agent commits are never GPG-signed.** Added 2026-09-27 by the
user's decision. The key never enters the container: `~/.gnupg` is a
credential store and is never mounted.
- **What is unsigned:** every commit made on an agent's branch, both the
  runner's end-of-run commit and the commits an agent makes itself inside its
  executor. It is as if `commit.gpgsign=false` were set for them. The
  mechanism is the implementer's choice, for example an option on the commit,
  or `GIT_CONFIG_*` in the agent's environment. The user's config is never
  written.
- **What is unchanged:** the orchestrator-side commits, meaning `gitops.merge`
  (squash and `--no-ff`) and `initial_commit`. They keep the user's signing
  configuration. With a squash merge, the default, only that merge commit
  reaches the base branch.

Verified by: with `commit.gpgsign=true` and an unusable `gpg.program`
configured, the end-of-run commit and an agent-side `git commit` both
succeed, while `gitops.merge` still tries to sign (and so fails) in the same
setup.
