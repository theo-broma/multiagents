# Sandbox and git: agent-written code executed on the host

Status: **contract, 2026-09-27.** The user approved this work the same day. Ids `SG-R1`… are stable;
never renumber. It was found by the
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

## Facts (researcher stand-in ag-f8b8f7, 2026-09-27)

- **What the container writes under the root:**
  - a worktree commit writes `.git/objects`, `.git/refs`, `.git/logs`,
    `packed-refs` and `.git/worktrees/<id>/`;
  - nested orchestration, meaning the MCP server run inside the container
    (`docker.py:371-394`), also runs `git worktree add`/`prune`, creates and
    deletes branches, merges children, and writes `.multiagents/`
    (`tree.json`, `events.jsonl`, `runs/`).
- **`.multiagents/config` is already mounted read-only**
  (`docker.py:568-569`).
- **Worktrees and homes live outside the root,** under
  `~/.multiagents/{worktrees,homes}/<slug>`.
- **Host-side git on agent-controlled trees:**
  - `commit_all` (runner `_finalize`, the CI-R5 loop);
  - `restore_paths` and `merge` (`merge_agent`);
  - `is_dirty`, `uncommitted_entries`, `diff_stat`, `changed_paths` and
    `commits_on` (`git status`/`diff`/`rev-list`).

  All of them honour `core.fsmonitor` and hooks.
- **The mount check does not recreate the container.** `mount_drift()`
  only warns (before the first delegation); only `docker rm` followed by
  `up` recreates it.

## Why protecting `.git/config` is not enough

An agent controls its worktree's `.git` file, and `.git/worktrees/<id>/`
(`commondir`, `gitdir`) is writable from the container. So a host-side git
run in an agent's worktree can be redirected to a git directory the agent
wrote, with its own config (`core.fsmonitor`, hooks). In-tree hooks
(`core.hooksPath=.husky`) and hooks that run the project's tools (linters,
tests) execute agent-written content by design.

## Behaviours

This contract covers the docker executor. The local executor has no sandbox,
and its agents are trusted by construction. This contract does not change it.

**SG-R1 — nothing an agent can write is executed by a host-side process.**
The host-side processes are the runner, the MCP server on the host and the
orchestrator's tools. "Can write" covers:
- anything in its worktree (tracked or not), including its `.git` file;
- anything writable from the container.

That rules out git hooks, git config (`core.fsmonitor`, `core.hooksPath`,
filter and diff drivers, aliases, `include`), and a redirection of the
repository through `gitdir` or `commondir`.

Verified by, for each host-side git entry point listed under Facts: an agent
worktree prepared to execute a marker program through each of those vectors
(a hook, `core.fsmonitor` in a redirected gitdir, a `commondir` pointing to
an agent-written dir, a filter driver) never runs the marker on the host.

**SG-R2 — the container cannot change what the host trusts.**
- **Read-only from the container:** the main checkout's working files,
  `.git/config`, `.git/hooks`, `.git/info`, and `.multiagents/config`, as
  today.
- **Still writable, because worktrees and nested orchestration need it:**
  - the repository data: objects, refs (loose and packed), reflogs;
  - `.git/worktrees/`, for creating, pruning, locking and updating
    worktrees;
  - `.multiagents/` runtime state.
- **Missing paths:** a protected path that does not exist yet is created, as
  an empty directory or file, before the container starts, so it can be
  protected.

Verified by:
- from inside the container, writing each protected path fails, and replacing
  it by rename fails;
- a worktree commit, `git worktree add`/`prune`, a branch create and delete
  (packed-refs rewritten), and a nested child spawn and merge all still
  succeed.

**SG-R3 — an agent's own commits, and their hooks, run inside its sandbox.**
- **What moves:** the runner's end-of-run commit, the CI-R5 fix-loop commits,
  and `restore_paths`' commit on an agent branch all execute inside the
  agent's executor, not on the host.
- **Hooks:** they still run (CI-R5 is unchanged: no `--no-verify`), with the
  container's filesystem and network limits.
- **Unchanged contract:** CI-R1 to CI-R7 keep their observable behaviour,
  including hook detection and the fix loop.

Verified by: the commit-identity suites stay green under the local executor,
and a docker-executor test (host suite) shows the end-of-run commit's hook
running inside the container, for example by writing its hostname or
cgroup.

**SG-R4 — host-side git on an agent tree resolves the repository only from
what multiagents recorded.**
- **Resolution:** every host-side git command that reads an agent's worktree
  or branch uses the git directory, common directory and work tree
  multiagents recorded when it created the worktree. It never follows the
  worktree's `.git` file, nor `.git/worktrees/<id>/{gitdir,commondir}`.
- **Neutralised:** such commands run with `core.fsmonitor` off, and they
  execute no hooks, since they are reads or orchestrator-side bookkeeping.

Verified by: the SG-R1 vectors, applied to `is_dirty`, `diff_stat`,
`changed_paths`, `commits_on` and `uncommitted_entries`.

**SG-R5 — a host-side merge runs the base's hooks, never the branch's.**
- **The rule:** `gitops.merge` still runs the user's commit hooks on the
  merge commit, but it runs them as they are on the base branch before the
  merge. When `core.hooksPath` points inside the working tree, it uses that
  directory's content as of the base's HEAD, not the merged version.
- **Accepted:** a hook that runs the project's tools over the merged tree, a
  linter or the tests, does execute the merged content on the host. That is
  the orchestrator's deliberate act after review, the same as a human
  merging. It is documented, and it is not treated as a sandbox escape.

Verified by: a repo with `core.hooksPath=.hooks`, and an agent branch that
modifies `.hooks/pre-commit` to write a marker. The merge runs the base's
hook, and the marker is absent.

**SG-R6 — existing containers are brought in line safely.**
- **When:** a container created before SG-R2 has the old mounts. It is
  recreated only when no agent runs in it, in keeping with the provisioning
  rule.
- **Until then:** delegation to the docker executor is refused, with a
  message naming `multiagents docker rm && multiagents docker up`. This is a
  stricter form of today's mount-drift warning.

Verified by: the mount-drift check with an old-layout container refuses the
first delegation, and names the command.

## Decisions, 2026-09-27 (orchestrator, after tester ag-689232)

- **SG-R4, where the trusted paths come from:** derived, not recorded. The
  host knows the project root, so the common dir is `<root>/.git`. The git dir
  is `<root>/.git/worktrees/<basename of the worktree path>`, since
  multiagents names every worktree directory after its unique node id. It
  must exist, and the work tree is the path given. Nothing is read from
  `tree.json` or from any file in the worktree or `.git/worktrees/<id>/` to
  find them. The gitops read functions keep their signatures (a path in).
  With `GIT_DIR` and `GIT_COMMON_DIR` pinned, config comes only from the
  trusted `<root>/.git/config`, plus global and system.
- **SG-R4 covers every hook, not only commit hooks:** host-side reads run
  with hooks disabled. That includes `post-index-change`, which a plain
  `git status` fires, as well as `core.fsmonitor`.
- **SG-R4 on read results:** when the agent's files try to redirect the
  repository, a host-side read returns what the real repository says, not
  what the agent's files claim. Nothing is executed either way.
- **SG-R2 addition:** the main checkout's `.git/HEAD` and `.git/index` are also
  read-only from the container. Nothing the container does needs them, and
  an agent that moved the main HEAD would redirect the orchestrator's merges.
  `.git/` itself stays writable, because packed-refs is rewritten by
  lock-and-rename. The protected files are protected as file or directory
  bind mounts.
- **SG-R5, hook directory as a whole:** the base's version of the whole hooks
  directory is used, including helpers a hook calls next to itself. A hook
  that exists only on the branch being merged is not run.
- **Test placement:** SG-R1 and SG-R3 for `commit_all` and `restore_paths` need
  real docker, because the local executor still runs hooks by design (CI-R5).
  They are opt-in and run on the host. SG-R4 and SG-R5 are tested directly
  through gitops. SG-R6 uses the FakeDocker pattern, and SG-R2 is checked
  statically on `run_args()`, plus opt-in real-docker checks.
- **Vacuous controls:** where a vector does not fire even under plain git
  (for example `rev-list` or `diff --stat` between commits), no SG-R1 test is
  needed for it. Say so in the test file's docstring.
