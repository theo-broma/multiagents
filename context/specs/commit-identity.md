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
- **Hooks and signing on agent commits (`commit.gpgsign`, pre-commit hooks):**
  bypassing them overrides the user's repository policy, so the user decides.
  Open. Until then, a failure from either is reported per CI-R2.
