# Session persistence — a finished agent stays resumable across a container recreate

Status: contract, 2026-09-26. Source: ticket `bug-4a0446`. Ids `SP-R1`… are
stable; never renumber.

## Why

On 2026-09-24 a change to `auth_proxy` altered the container's mount list.
`multiagents run` then refused to start and told the user to run `multiagents
docker rm && multiagents docker up`, adding only "that ends any agent still
inside". The user did so. Every claude transcript lived in the container's own
writable layer, not in a mounted path, so all of them were deleted.

Two days later, `steer_agent` on a finished node (ag-12951a) went wrong in
three ways:
- it reported `steered: true, status: running`;
- it created a new branch `…/12951a-2` off base instead of reattaching the
  node's branch;
- the resume then failed with "No conversation found with session ID".

The orchestrator protocol ("an interrupted agent is resumed, not replaced")
rests on transcripts surviving. Nothing guaranteed that they did.

The providers already declare where their transcripts live: `transcript:
{dir, glob}` in `providers.yaml`, read by `watchdog.transcript_source`.
`claude.sh` computes the same location in `claude_sessions_root`. Neither says
where that path is backed when the agent runs in the container.

## Behaviours

**SP-R1 — a docker agent's transcripts are written to host-backed storage.**
For every provider that runs under the docker executor and declares a
transcript location, the session files its agents write inside the container
land in a directory backed by the host, one that survives
`multiagents docker rm && multiagents docker up`.
- The location stays per project and per provider, and it is never the user's
  own `~/.claude` (or any other provider's user profile) on the host.
- The executor does this generically, from the provider's declaration: no
  provider is named in the executor.
- `transcript.dir` contains a per-agent placeholder (`{slug}`, the folded
  worktree path). What is backed is its **static prefix**: the path up to the
  first component that contains a placeholder (for claude,
  `~/.claude/projects`). One container-wide mount then covers every agent.
  The worktree path is identical inside and outside the container, so the
  slugs match.
- **Resolve a contradiction first.** `providers.yaml` already declares
  `.claude` as a container-private home that is host-backed
  (`container_private_home`), yet on 2026-09-26
  `~/.multiagents/container-state/shared/claude/.claude/` held no
  `projects/`. Find out where the CLI in the container actually writes
  (`HOME` and `CLAUDE_CONFIG_DIR` at exec time, a per-agent copy, and so
  on), and back THAT path. The docker variant below is what proves it.

Verified by:
- a unit test that, for a provider declaring a transcript dir, the docker run
  command or mounts back that dir with a host path under the project's or
  the user's multiagents state, and that it is absent for a provider with no
  declaration;
- a docker variant (opt-in, host only, like `SV_TEST_DOCKER`): write a file
  where the provider's transcript would go, recreate the container, and the
  file is still there.

**SP-R2 — host tooling reads a docker agent's transcript where it really is.**
Everything on the host that reads an agent's transcript reads the host-backed
path of SP-R1 when the agent ran under the docker executor. That covers the
watchdog's `transcript_source`, `transcripts.default_root` callers, and
`claude.sh` `compact` when it is run for a docker agent.
Verified by: unit tests resolving the path for a docker node and a local node.

**SP-R3 — `steer_agent` refuses a session it cannot resume, before changing
anything.** Before relaunching, `steer` checks that the node's session exists
where SP-R1/SP-R2 say it should.
- The check applies only when the provider declares a transcript location.
  Providers that declare none are unaffected.
- If the session is missing, `steer` returns `steered: false` with an error
  naming the session id and the path it looked in, and suggesting a fresh
  run. It starts no process, creates no branch or worktree, and leaves the
  node's status unchanged.
Verified by: a runner test with a declared transcript dir and no file, checking
the return value, that no process was launched, the node status, and the branch
list.

**SP-R4 — a steer never forks the node's branch.** When a steered node's
worktree is missing, the worktree is recreated on the node's existing
`branch`, with its commits intact. If that branch no longer exists, `steer`
returns `steered: false` with that reason. It never cuts a new, suffixed
branch off base for an existing node.
Verified by: a runner test where the worktree was removed and the branch has a
commit. After the steer, the worktree is on the same branch with that commit,
and no `-2` branch exists. Also a test where the branch was deleted.

**SP-R5 — recreating the container says what it will cost.** The mount-drift
refusal (`DockerExecutor` stale mounts, and `run`'s preflight) and
`multiagents docker rm` must both list, by id, every node that is `running`,
`detached` or `stuck` under the docker executor, since recreation ends them.
- `docker rm` with such nodes present refuses, with that list, unless
  `--force` is given.
- The drift refusal must not prescribe a command that would then refuse.
  With such nodes present, it says to run `multiagents stop` first, then
  `docker rm && docker up`. With none, it keeps today's prescription.
- The refusal message suggests `multiagents stop` first.
- With none present, both behave as today.
Verified by: CLI tests with seeded tree nodes, covering:
- the drift refusal's wording with and without such nodes; the refusal and its
exit code, `--force` proceeding, and the message listing the ids.

## Out of scope

- Recovering transcripts already lost. They are gone.
- Providers that keep sessions server-side.
- Moving existing host-side transcripts. The local executor is unchanged.

## Constraints

- No provider-name special-casing in the executor or the runner. The
  provider's declaration (`transcript:`) and its script are the only
  provider-specific inputs.
- Agent-survival (SV-R1..R11, `context/specs/agent-survival.md`) is merged or
  landing. It changed `runner.steer`, the executors and `cmd_stop`: build on
  the current code.
- Credentials must not become readable through the new mount. Persist the
  transcript directory only, not the whole CLI profile.

## Decision, 2026-09-26 (orchestrator, after tester ag-9c02be)

**SP-R1's premise was partly wrong.** Since `auth_proxy` was enabled (on
2026-09-24 at 22:16), the container's own claude profile is host-backed
through `container_private_home: [".claude"]`, at
`~/.multiagents/container-state/shared/claude/.claude/`. Its `projects/`
holds every transcript written since then. ag-12951a's transcript predates
that change: it was in the container layer, and that is why it was lost.

So SP-R1 is satisfied for a provider when **either**:
- its transcript prefix lies inside a host-backed container-private home.
  This is today's claude path. It is multiagents' own container profile,
  never the user's `~/.claude`, and keeping credentials there is the
  existing design, not a regression;
- **or** the executor backs the static prefix with its own mount.

The executor must guarantee one of the two for every provider that declares
a transcript location. It must not add a second mount where the first
already holds. The unit tests assert the guarantee, not the mechanism: the
container path of the transcript prefix resolves to a host path that is not
the user's HOME profile.

- **SP-R2's "docker node":** a node run while the project's executor is
  docker. Nodes do not record their executor. That is acceptable for now.
- **The SP-R2 API:** the implementer may give `transcript_source` an executor
  argument, or add a resolver beside it. Tests go through steer and the
  resolver, not through a private signature.

**2026-09-26, landed (b6610ca).** Accepted from ag-261e5d:
- SP-R3's pre-check is skipped for `running`/`pending` nodes, because a live
  CLI may not have written its session file yet;
- SP-R4 reuses the node's old worktree path, because the transcript slug is
  keyed on it;
- `claude.sh compact` stays host-only. Compacting docker agents is not a
  feature today.
Open: a provider declaring a `~/…` transcript without a matching `home_links`
entry writes into its per-agent home, which host tooling does not read.

## Decisions, 2026-09-26 (orchestrator, after adversary ag-18167b)

All eight findings are accepted. Their tests are in
`tests/test_session_persistence_adversary.py`.

- **SP-R1, the prefix is a proper subdirectory.** The static transcript
  prefix is resolved lexically: `~` is expanded and `..` is normalised.
  - It is refused (no mount, no host path) when it is:
    - the filesystem root;
    - the container HOME itself;
    - an ancestor of HOME;
    - a path that leaves a container-private home once normalised.
  - A refused prefix is logged, and the provider is treated as declaring no
    transcript location. It is never mounted.
  - A placeholder other than `{slug}` gets the same treatment, and never
    raises.
- **SP-R2 covers `transcripts.session_context`,** and every other
  transcript reader: they resolve through the executor like `steer`.
- **SP-R2, slugs:** the slug is computed from the worktree path with
  symlinks resolved. If that finds no transcript, the unresolved path is
  tried as well.
- **SP-R3, what counts as present:** a session file counts as present only
  if it holds at least one complete (newline-terminated) line that parses as
  JSON. A missing file, an empty one, or one with no complete line is
  refused.
  - A truncated LAST line after complete ones is normal mid-write and is
    accepted.
- **SP-R4, what "missing worktree" covers:** the worktree counts as missing
  when its path does not exist, or exists but is not a git worktree on
  `node.branch`.
  - A non-git directory, or a worktree on another branch, at that path is
    moved aside (renamed with a suffix, never deleted), then the worktree is
    reattached.
  - A node with an empty `branch` reattaches `agents/<role>/<id>` if that
    branch exists. Otherwise steer refuses with the reason. It never creates
    a suffixed branch.
