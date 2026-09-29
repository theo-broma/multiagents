# H3: no agent-controlled program runs on the host through git

**Status:** contract, written by the orchestrator on 2026-09-28. It covers
H3 in `phase6-hardening.md`.
- Ids are `HG-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.
- The audit comes from researcher ag-6a8eb5, and is summarised below.
- Companion to H1 (`h1-host-authority.md`): H1 decides *what* the host
  mutates, and H3 decides *what the host executes* while doing it.

## The audit, summarised

Host git calls go through `gitops` as `git -C <repo> …` with the host
environment (`gitops.py` ~48). Two families exist.
- **Pinned calls**, `_pinned` (`gitops.py` ~317). These are the refresh
  reads, `reset --keep`, `collect_agent`'s `commits_on` and `diff_stat`.
  - They use a private `GIT_DIR` and index, with `core.fsmonitor=false` and
    an empty `core.hooksPath`.
  - `reset` also empties every trusted filter.
  - This family is already safe.
- **Unpinned calls.** These are the dangerous ones:
  - `gitops.merge`: the `status` preflight, `merge --squash` or `--no-ff`,
    `commit`, `merge --abort` and `reset --hard`. Reached by `merge_agent`,
    the auto-merge into a parent, and the merge of pending children.
    - The SG-R5 `_base_hooks` mechanism *deliberately* runs the base
      branch's hooks on merged agent content.
    - An outside-tree `core.hooksPath` stays active.
  - `commit_all`, on CLI stop and resume (`cli.py` ~939, ~2547): `add -A`,
    then `commit`. The commit hooks run, and `add -A` applies the clean
    filters that the agent's `.gitattributes` selects.
  - Recovery on resume and steer: `worktree move`, `prune` and `add`.
    `worktree add` checks out, so smudge filters and `post-checkout` can run.
  - `push -u` (`push_branch`): transport helpers and `core.sshCommand`.
  - Cleanup: `worktree remove`, `prune` and `branch -D`. These execute no
    content programs, but they still resolve the git dir and config from the
    worktree.

Agent-writable sources of **git dir or config**:
- the worktree's `.git` file, which is worktree content and writable;
- `.git/worktrees/<id>/`, which is writable in the container and holds:
  - `commondir`, which can redirect the "common" config;
  - `gitdir`;
  - `config.worktree`, honoured when `extensions.worktreeConfig` is set.

An unpinned host call run *in* an agent worktree can therefore be handed a
config the agent wrote. That config can set hooks, fsmonitor, filters,
drivers, `core.sshCommand`, includes, or anything else.

The base repository's `.git/config` and `.git/hooks` are read-only to the
container. They and the user's global and system config are **trusted**.

| Host Git call family | Possible program or redirect | H3 boundary |
| --- | --- | --- |
| Trusted base `git config` lookup for content-program names | global and base config includes | Reads only the base checkout's trusted config before per-call overrides are built (HG-R1, HG-R4) |
| `merge` status, squash, no-ff, commit, abort, reset | hooks, fsmonitor, clean/smudge/process filters, merge drivers, diff programs, linked-worktree config | HG-R1, HG-R2, HG-R3, HG-R4 |
| Host `commit_all` on stop or resume | hooks, fsmonitor, clean filters, linked-worktree config | HG-R1, HG-R2, HG-R3; HG-R4 refuses filtered paths |
| `worktree add` and recovery attach | post-checkout, smudge/process filters, fsmonitor | HG-R2, HG-R3; HG-R4 reports unconverted paths |
| `worktree move`, remove and scoped prune | writable registry `gitdir`, linked-worktree config | HG-R1 registration validation; HG-R2 and HG-R3 |
| `branch -D` and push | hooks/config, credential or transport helpers, `core.sshCommand` | HG-R1, HG-R2, HG-R3, HG-R5 |
| Pinned refresh reads and `reset --keep` | fsmonitor, filters | HG-R1, HG-R2, HG-R3, HG-R4 via the existing pinned path |

## Behaviours

**HG-R1: no agent-written git dir or config is ever honoured by a host
call.**
- Every host git call that operates on an agent worktree, or on the
  `.git/worktrees/<id>` metadata, resolves its git dir, common dir, index
  and config from host-held operands. Those operands are H1's host record
  plus the base repository's own paths.
- It never resolves them from:
  - the worktree's `.git` file;
  - `commondir`;
  - `config.worktree`.
- **The worktree registry is the exception** (advisor, turn 10). The
  maintenance commands `worktree remove`, `move` and `prune` necessarily
  read `.git/worktrees/<id>/gitdir`.
  - Before each one, the host validates that registration against H1's
    record: the recorded path and the recorded branch. It refuses a
    mismatch with `host_authority_mismatch`.
  - There is no unscoped `git worktree prune`. Pruning touches only the
    registration of the node being cleaned, never entries beyond it.
  - Verified by: a forged `gitdir` in another node's registration is not
    acted on, and a cleanup prunes only its own entry.

  The existing `_pinned` mechanism is one way to meet this, not a mandated
  one.
- Scope: `gitops.merge` in all its steps, `commit_all`, worktree
  `add`/`move`/`prune`/`remove`, `branch -D`, `push`, and every helper those
  call.
- Verified by: one test per call family. Each plants an agent-writable
  redirection and asserts the planted program never runs, where the
  program writes a sentinel file. The redirections are:
  - a `.git` file pointing at a crafted dir with a hook and an fsmonitor
    program;
  - `commondir` pointing at a crafted config;
  - `config.worktree` with `extensions.worktreeConfig` set in trusted
    config.

**HG-R2: hooks are off for every host git call by default.**
- Host git calls run with hooks disabled. No hook from any source fires:
  - not the base hooks (SG-R5);
  - not `.git/hooks`;
  - not a trusted `core.hooksPath`.
- This covers: merges into the base checkout and into a parent worktree,
  squash commits, `commit_all` on stop and resume, `worktree add`
  (`post-checkout`), and rollback.
- The opt-in is `git.merge.host_hooks: true` in `project.yaml`, default
  `false`. It re-enables **only** the base-branch hooks, and **only** for
  `merge_agent` into the base branch, as SG-R5 chose them.
  - It never enables hooks for merges into agent worktrees, for
    `commit_all`, or for worktree operations.
  - The setting is documented beside it in the shipped
    `defaults/project.yaml`. The comment says why the default is off: a
    base hook that runs tests executes the agent's merged code on the host.
- When a merge skips a hook that would otherwise have run, the host says so
  **once per process**, as an event plus one line in the `merge_agent`
  result. The line names the setting.
- **Boundary for `commit_all`** (a tester question). Two kinds of commit
  are distinguished:
  - **Agent commits** keep CI-R5's hooks unchanged. These are commits the
    runner makes on an agent's behalf **through that agent's executor**,
    docker or local, meaning `commit_all` called with the agent's
    executor.
  - **Host bookkeeping** follows HG-R2, with hooks off. These are commits
    the host CLI or server makes itself, outside any executor: the stop and
    resume commit of an interrupted agent's worktree (`cli.py` ~939,
    ~2547), where `commit_all` is called without an executor, which means
    on the host.
  - Under the local executor the agent already runs on the host, so its
    agent commits are unaffected.
- This supersedes SG-R5's default. CI-R5's "hooks kept" is untouched: it
  concerns agent commits *inside the container*, not host calls.
- **User decision `merge-hooks`:** this default is proposed to the user,
  and the work proceeds on it.
- Verified by:
  - with a trusted base `pre-commit` or `post-merge` hook that writes a
    sentinel, `merge_agent` (squash and no-ff) leaves no sentinel by
    default, and the result carries the one-line notice;
  - with `host_hooks: true`, the base hook runs for `merge_agent` into
    base, and still does not run for a parent-worktree merge or for
    `commit_all`;
  - `commit_all` on stop fires no hook in either setting.

**HG-R3: fsmonitor is off for every host git call.** It is off whatever
trusted or untrusted config says, because there is no legitimate need for a
host bookkeeping call to spawn it.
- Verified by: a trusted `core.fsmonitor` program writing a sentinel never
  runs during the calls in HG-R1's scope.

**HG-R4: no content program runs over agent content on the host by
default.** A trusted config can still name a program that the agent
*selects* through `.gitattributes`, and whose script may live in the merged
tree. So "trusted config" is not enough (advisor, turn 10).

By default, every host git call runs with all external content programs
disabled:
- `filter.*.clean`, `smudge` and `process`;
- `merge.*.driver`;
- `diff.*.textconv`;
- `diff.external`.

The opt-in is `git.merge.host_content_programs: true`, default `false`.
- It re-enables the trusted-config programs **only** for `merge_agent` into
  the base branch. That is the LFS case.
- It is documented beside the setting. The comment warns that with the
  opt-in, an agent-written `.lfsconfig` (`lfs.url`) can steer where LFS
  makes network requests.
- It never applies to `commit_all`, to worktree operations, or to merges
  into agent worktrees.

Where disabling a program would silently change content semantics, the
host refuses visibly instead:
- **`commit_all` on stop or resume.** When any path it would stage has a
  `filter` attribute, the host does not commit. The worktree is left as it
  is, uncommitted and intact. It emits one event and one line naming the
  paths.
  - The reason: committing a raw file in place of its filtered form, such
    as an LFS binary, would corrupt the branch.
  - Where the executor provides a container, committing inside the
    container, where the agent's code already runs, is an acceptable way
    to satisfy the same behaviour.
- **`worktree add` on the host,** during recovery. It checks out with
  filters disabled. When the checked-out tree has `filter` attributes, it
  says once that those paths are unconverted.
- **Merges.** With the opt-in off, a merge whose paths select a filter or
  a merge driver runs without them. The `merge_agent` result says so in one
  line naming the setting.

Verified by:
- a trusted `filter.x.clean` that writes a sentinel, selected by an
  agent-written `.gitattributes`, never runs during `commit_all`, and
  `commit_all` refuses visibly;
- a trusted `merge.x.driver` whose command runs a script *from the merged
  tree*, selected by the branch's `.gitattributes`. It does not run during
  `merge_agent` by default. With `host_content_programs: true` it runs for
  a merge into base, and it never runs for a merge into a parent worktree.

**HG-R5: push and transport.** `push_branch` takes its remote from trusted
project config (`git.remote`), never from agent-writable config (HG-R1).
- Credential helpers, `core.sshCommand` and remote helpers come only from
  trusted config. It otherwise behaves as today.
- Verified by: a `config.worktree` or redirected commondir setting
  `core.sshCommand` to a sentinel program is not honoured by
  `push_branch`, tested against a local bare remote.

**HG-R6: the audit is recorded.** `context/specs/h3-host-git-execution.md`
keeps the table of host git calls, with what each can execute and which
behaviour closes it. It is updated when a new host git call is added.
- A test enumerates the `gitops` functions that invoke git without the
  hardened path, and fails when a new one appears without being listed in
  an allowlist inside the test.
- Verified by: that enumeration test.

**HG-R7: nothing else regresses.**
- Merges, conflict aborts, rollback, `commit_all` identity handling
  (CI-R*), sandbox-git behaviour inside the container, and refresh or reset
  keep their current results, apart from the hooks and filters switched off
  above.
- The existing suite stays green, apart from the 72 known phase2 reds.
  Tests that asserted SG-R5's hooks-on default are updated **by the
  tester**, deliberately, to the new default, and listed in its result.
- Verified by: the existing suite.

## Adversary brief

Work from inside the container, with write access to everything listed in
H1's threat model. Make the host execute a program you chose, or one you
selected, during:
- any host git call: merge, stop or resume `commit_all`, steer or resume
  recovery, cleanup, reap or push;
- anything the host does with an agent worktree or its metadata.

A trusted program the user configured, run over data only as HG-R4
permits, is not a finding.
