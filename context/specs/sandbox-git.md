# Sandbox and git: agent-written code executed on the host

Status: **problem statement, 2026-09-27. Not yet a contract.** It waits for
the user's go-ahead, because it extends the brief. It was found by the
adversarial tester ag-6ceb2b while attacking CI-R5.

## The problem

Under the docker executor, `executor/docker.py:mounts()` mounts the whole
project root **writable** inside the container. That includes the main
checkout and `.git/`: `.git/hooks`, `.git/config` and `.git/info`. An agent
can therefore write:

- a hook in `.git/hooks/`, or config such as `core.hooksPath`,
  `core.fsmonitor`, a filter driver or an alias, in `.git/config`.

  Host-side git then executes that code outside the container, with the
  user's filesystem, network and credentials. Host-side git includes the
  runner's end-of-run `commit_all`, `gitops.merge` (its squash commit runs
  `pre-commit` and `commit-msg`), and any `git status` run by the
  orchestrator or the user.
- files in the main checkout: `src/`, `BRIEF.md`, and
  `.multiagents/config/project.yaml`, which is the sandbox's own
  configuration.

This predates CI-R5: `commit_all` already ran hooks before it. CI-R5 does not
widen it. It makes it more visible, because it now invites agents to deal
with hooks.

A related case holds even with `.git` protected. A user whose hooks live in
the tree (for example `core.hooksPath=.husky`) runs hooks that the agent's
branch can change. On the host, the end-of-run commit then runs the agent's
version, and a squash merge brings the agent's version into the main checkout
before its commit runs the hooks.

## Directions, to decide when this becomes a contract

1. Mount the project root read-only. Mount writable only the parts of
   `.git` a worktree commit needs (`objects`, `refs`, `logs`,
   `worktrees/<id>`, packed-refs), and keep `.git/hooks`, `.git/config` and
   `.git/info` read-only.
2. Run the end-of-run commit inside the agent's executor rather than on the
   host, so the agent's hooks run sandboxed.
3. For host-side merges, run the hooks from the base branch, never from the
   branch being merged, or declare in-tree hooks unsupported for merges.

The advisor has not seen this yet; it is in context/advisor-catchup.md.
