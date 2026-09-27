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
- **SG-R4, how gitops finds the root** (tester's NEED_INFO sg-r4-root). The
  gitops read functions (`is_dirty`, `uncommitted_entries`, `commits_on`,
  `diff_stat`, `changed_paths`) gain a keyword argument, `repo: Path`, which is
  the project root. When it is given, resolution is pinned as above.
  Host-side callers in the runner and the CLI always pass it, because they
  know `paths.root`. Without it, the behaviour stays as today, for callers
  that are not about agent trees. This replaces "the signatures are
  unchanged" above.

## Decisions, 2026-09-27 (orchestrator, after tester ag-708d52 on SG-R5)

- **`--no-ff`:** git runs `pre-merge-commit` and `commit-msg` for a merge
  commit, not `pre-commit`. `gitops.merge` runs whatever git itself would
  run, taken from the base. It adds no extra hook.
- **Refusal status:** unchanged from today. A squash merge refused by a hook
  returns `failed`, and a `--no-ff` one returns `conflict`. Neither is ever
  `merged`.
- **Refusal cleanup:** a merge refused by a hook leaves the base checkout as
  it was before: HEAD unchanged, nothing staged, and the working tree
  restored. The branch's changes must not be left staged on the user's base.
  This is a new requirement, to be tested by the tester.
- **Documented limitation:** a base hook that calls a helper through a path
  relative to the repository (`.hooks/helper`), rather than one next to
  itself, reads the merged working tree and so runs the branch's helper.
  SG-R5 does not cover that. It falls under "a hook that runs the project's
  files over the merged tree is the orchestrator's deliberate act".

## Decisions, 2026-09-27 (orchestrator, after testers ag-2e9add on SG-R2/R6 and ag-4c3dd4 on SG-R4)

- **SG-R4 keyword renamed:** the five functions already name their first
  parameter `repo`. The new keyword is **`root`**, the project root; the first
  parameter keeps its name. This replaces "`repo: Path`" above.
- **SG-R4, path equal to root:** when the path given is the root itself (the
  main checkout), the pinned resolution is git dir = common dir =
  `<root>/.git`, and work tree = root, with the same neutralisation.
- **SG-R4, missing git dir:** if the derived git dir does not exist, the
  function raises `gitops.GitError` naming the path. It never falls back to
  unpinned resolution.
- **SG-R4, nothing written:** pinned reads write no git config.
- **SG-R2, index:** a missing `.git/index` is never created as a zero-byte
  file, which breaks git. It is created by git itself as a valid index. Host
  `git status` output must be unchanged by that step (the tester's test holds).
- **SG-R2, protected-path additions:**
  - `.git/config.worktree`;
  - `.git/modules` (submodule config);
  - `.git/refs/heads` and `.git/refs/tags`, while `.git/refs/heads/agents`,
    the agent-branch namespace, stays writable;
  - the base branch, meaning the branch checked out in the main checkout.
    It always has a loose ref file, unpacked by the host before the
    container starts if needed, so that a rewritten `packed-refs` cannot
    move it. A loose ref wins over a packed one.

  Otherwise an agent could move the base branch, and its commits would reach
  the user's branch without a merge.
- **Accepted limitations** (documented, not tested):
  - a user branch that is only in `packed-refs` and is not the base can
    still be rewritten through `packed-refs`;
  - `.git/objects/info/alternates` stays writable, which is data only and
    executes nothing;
  - a project whose `.git` is a file rather than a directory is out of scope
    for SG-R2, and the executor refuses to start with a clear message.
- **Mount paths:** every mount is at its own host path (source equals
  destination), as today.

## Decisions, 2026-09-27 (orchestrator, after tester ag-115a7c)

- **`GitError` "naming the path":** either the path given or the missing
  derived git dir is fine.
- **A `.git` file is refused** at `ensure_running()`, with an error that
  mentions `.git` and "file".
- **Detached main HEAD:** there is no base branch to unpack. `refs/heads` is
  still read-only apart from `agents/`, and HEAD stays read-only.
- **Paths:** the path and root are resolved (symlinks, relative paths) before
  they are compared.
- **Fix required alongside SG-R4:** `gitops.uncommitted_entries` drops the
  first entry when it starts with a space, because `run()` strips the output.
  That is a pre-existing bug, and it is fixed with SG-R4.
- **Redirected `commondir`:** git follows the `commondir` file even when
  `GIT_COMMON_DIR` is set. The pinned resolution must therefore not rely on
  environment variables alone. The mechanism is the implementer's choice.

## Decisions, 2026-09-27 (orchestrator, after implementer ag-9e10d8)

- **Pinned reads in the runner too:** the raw `status --porcelain` in
  `runner._refresh_conversation` and `holds_unmerged_commits` on an agent
  worktree go through pinned resolution, as SG-R4 requires. They are done
  with SG-R3.
- **`GitError` from a pinned read in the runner:** it never crashes the
  runner. The tree is treated as unreadable, a `git_unreadable` event is
  emitted with the path, and the operation takes its conservative branch:
  it does not merge, and it does not assume the tree is clean.
- **Documented limitation (SG-R5):** a hook in the in-tree copy that reaches
  outside its own directory by a relative path finds nothing during a merge.
  Husky v9 (`core.hooksPath=.husky/_`) is one such case, and its wrapper then
  skips the user's hook on merges. Users of such setups should keep hooks
  self-contained. Not fixed.
- **The server's `git_status` tool and `monitor/snapshot.py` are pinned
  too** (the implementer went beyond the task). Kept.

## Decisions, 2026-09-27 (orchestrator, after implementer ag-ce8b54 on SG-R3)

Host run: `tests/test_sandbox_git_docker_live.py` passes 7/7 at 4b9dbda.
The gaps below were found by the implementer and are in scope, because each
one breaks SG-R1 or SG-R4:

- **SG-R4 applies to every host-side git command that touches an agent
  worktree:**
  - `runner._worktree_state`, the `git status` polled about every 3 s;
  - every git call in `_refresh_conversation`: `head_sha`, `merge-base`,
    `untracked_in_the_way`, `symbolic-ref` and `reset --keep`.

  Reads are pinned. A command that WRITES an agent worktree, such as
  `reset --keep`, runs inside the agent's executor, like SG-R3, or pinned
  with filters and hooks disabled. The implementer chooses, but no agent
  config may execute on the host.
- **SG-R7 — files the host writes or reads for an agent never sit where the
  container can write.** Today `start()` writes the agent's env file under
  `.multiagents/env`, which the container can write, and it follows
  symlinks. An agent could then redirect a host write anywhere, or feed the
  host a FIFO. Such files are either:
  - created with no-follow semantics, never through a pre-existing symlink,
    and read bounded and non-blocking; or
  - moved to a host-only location that is not mounted writable.

  This applies to env files, and to any other host-written file under
  `.multiagents` that the container can write.
  Verified by: an agent that replaces its env file path, or the directory
  holding it, with a symlink to a host file cannot cause the host to write
  that file, and a FIFO in its place does not hang `start()`.
- **A `docker exec` timeout** kills the process inside the container too.
  It must not leave git running.

## Decisions, 2026-09-27 (orchestrator, after the stopped adversarial reading ag-044d7b)

- **Pinned-read copy cap:** `_pinned` copies a worktree's `index` that the
  agent can write, and today accepts up to 512 MiB, at every 3 s status poll.
  The cap drops to 128 MiB. Above it, the read raises `GitError`, which the
  runner handles as `git_unreadable`. `HEAD` stays capped small.
- **SG-R2 addition:** `.git/objects/info` is read-only from the container,
  so the container cannot write `alternates` or `http-alternates`. An
  `alternates` entry pointing to a FIFO or elsewhere would make every
  host-side git read hang or read foreign objects. A `commit-graph` write
  from the container then fails, which is harmless.
- **`protect_project` runs on the host before docker starts:** it must never
  write through a symlink, nor block on a FIFO, that an agent planted while
  the old layout was writable. That covers `.git/hooks`, `info`, `modules`,
  `config.worktree`, `index`, `refs/heads/<base>` and `.multiagents/config`.
  A protected path that is a symlink or a FIFO makes it refuse, with the
  path named, rather than create or unpack through it.
- **Not pursued:** `.gitattributes` on the merge path has no built-in
  behaviour that executes anything. Filters and drivers need config, which
  is protected, and `core.attributesFile` is config too.
- **A failed pinned read is never "clean"** (found by tester ag-080955).
  When git fails under a pinned read, because of a corrupt index or any
  other error, `is_dirty`, `uncommitted_entries` and `status` raise
  `GitError`. They never return "clean" or an empty list. Before the cap,
  an index that is a FIFO or a symlink also raises `GitError`, rather than
  being skipped.

## Decisions, 2026-09-27 (orchestrator, after implementers ag-3c75c1 and ag-b65538)

- **The rest of SG-R7:** every file or directory the HOST creates or writes
  under `.multiagents` is written with the same no-follow primitives
  (`gitops._write_beneath` or equivalent). That includes the run-dir
  `mkdir`s, the pid file (`_pid_file`), and `scratch()`.
- **`server.py` `git_status`:** a `GitError` from a pinned read is reported in
  the tool's result, as an error field, and never raised as a tool failure.
  It is never reported as `dirty: false`.
- **The image has no `pkill`,** yet `DockerHandle.stop` and `_KILL_SCRIPT` use
  it. Check the result: if stopping a docker agent is silently broken, fix
  it with the same `/proc` walk that `ContainerGit` uses.
- **Filters are off during `reset --keep`** (ag-b65538). With filters
  disabled, LFS-style filters do not run while a conversation's worktree is
  moved. Accepted: the move only happens when the files are identical to
  the base, and the worktree is kept, not moved, when that cannot be shown.
- **Accepted:** `gitops.is_repo(node.worktree)` and `head_sha` just after
  creation stay unpinned. They execute nothing, since config and hooks are
  protected and read-only.
- **After tester ag-6466bc:**
  - SG-R7 covers `Tree`'s files (`tree.json`, `.bak`, `.tmp`, `tree.lock`
    and `events.jsonl`) and every file in a run dir (`command.json`,
    `prompt.md`, `result.json`, `stream.jsonl`, `supervisor.lock`,
    `output.ndjson`, `exit_status`). All in scope.
  - **Stopping a docker agent ends its whole process tree,** including a
    child that left the group with `setsid`. Today it is broken: dash's
    builtin `kill` rejects `--`, the image has no `pkill`, and
    `pkill -P` comes too late. The fix must work with the image as it is,
    with no new package required, for example the same `/proc` walk as the
    `ContainerGit` watchdog.
- **Open, after ag-5fb684:** a few host writes under `.multiagents` still use
  plain paths:
  - the driver's `launch/` files;
  - the watchdog's `{role}-status.json`;
  - `monitor-launch.log`;
  - the runner's `consult-*.lock` (around `runner.py:3319`).

  They are in SG-R7's scope, and are the next small follow-up.
- **Closed, ag-2e2613:** the four remaining writers now use no-follow
  primitives. The consult lock, when planted, is replaced rather than
  refused: a refusal would let one symlink block the advisor forever. The
  consult never runs without the lock.

  Accepted:
  - `cli.py stop` calls `is_file` and `unlink` on a plain path; `unlink`
    removes a link itself and writes nothing;
  - the provider launch scripts write under `launch/` as shell, on the
    orchestrator's host-side launch path, not for agents.

## Decisions, 2026-09-27 (orchestrator, after the live smoke test)

**Defect found live: the protection on single files disappears.** In the
recreated container, `.git/config` and `.git/index` were writable. A smoke
agent appended a line to the host's `.git/config`; it has been removed. The
container's mountinfo no longer lists those two mounts, although
`docker inspect` still shows them.

The cause: `.git` is itself a writable bind of the host directory. When the
host replaces a file by rename (every `git config` write, VS Code's
`vscode-merge-base`, every host `git status` that refreshes the index), the
kernel drops the mount sitting on that file. So **protection by
single-file bind mount inside a writable `.git` is not a protection.**
`HEAD` survives only until the host next rewrites it.

**SG-R2 revised:**
- **`.git` itself is read-only** from the container.
- **Writable directory mounts beneath it,** only where worktrees and nested
  orchestration need them:
  - `.git/objects`, with `.git/objects/info` read-only;
  - `.git/refs`, with `refs/heads` and `refs/tags` read-only and
    `refs/heads/agents` writable, as today;
  - `.git/logs`;
  - `.git/worktrees`.
- **No single-file mounts at all.** `config`, `config.worktree`, `HEAD`,
  `index`, `packed-refs` and `hooks/*` are protected because their directory,
  `.git`, is read-only.
- **Accepted consequence:** the container can no longer rewrite
  `packed-refs`, so deleting or renaming a branch that exists only in
  `packed-refs` fails from the container. Branch deletion is normally done by
  the host (`merge_agent` and `discard_agent`). A nested orchestrator that
  discards a packed child branch gets an error, which it reports. The base
  branch still gets unpacked to a loose ref by `protect_project`.
- **Verified by:** the live tests must check the protection **after the host
  rewrites the file by rename** (`git config`, `git status`, and a checkout
  that rewrites `HEAD`), not only right after the container is created. Any
  test that relies on a single-file mount is wrong.
- **SG-R6** then refuses the current container, because the layout differs.
  It is recreated when idle.
