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
