# Phase 7: plan nodes, a scheduler script, and one container per run

**Status: seed.** Written by the initializer on 2026-10-03. **Nothing here is
to be built until it has been specified with the user and the advisor.** That
happens in the next `multiagents init-agent` session, after the closing round
(`phase6-closing.md`) is done.

## Part 1: plan nodes and the scheduler (the user's idea, 2026-10-02)

The full idea, the user's decisions and the orchestrator's design notes are
in BRIEF, under "Ticket-driven agent scheduling (the user, 2026-10-02)". In
short:
- every delegation is a **node** with dependencies;
- a **script, not an agent**, launches the nodes that are eligible;
- a reviewer can **reopen** a node, with its comments;
- a node may carry urgency and time windows;
- a node can be composite, or a loop with a counter, a maximum and an action
  taken when the maximum is reached.

Vocabulary, decided by the user: **node** is the planned unit and **run** is
one execution of it. Today's tree "nodes" are runs.

## Part 2: one container per run (decided 2026-10-03)

### The decision and why

- Inside today's single project container, agents are **not isolated from
  each other**. Any agent can modify another agent's worktree, HOME, refs,
  reflog and objects. This is recorded in `h1-host-authority.md`, "threat
  model".
- On 2026-10-03 the user decided:
  1. **Accept this limitation for now.**
  2. **Remove it in phase 7, with one container per run**, created by the
     same host-side scheduler that launches runs.
  3. **Skip the intermediate step** of one Unix uid per agent inside the
     shared container.
- Why the intermediate step was rejected:
  - Agents run as the host user's uid (`docker exec --user <uid>`,
    `executor/docker.py`, in six places). With per-agent uids, the host
    could no longer modify or delete the files an agent creates on bind
    mounts.
  - Making that work needs default ACLs on every worktree, HOME and the
    shared `.git`, a reserved uid range, and a migration. It also reopens
    sandbox-git.
  - That is one to two days of agent work, and all of it would be thrown
    away by per-run containers.
  - In the meantime the risk is accidental damage to another agent's
    unmerged work, through a mistake or a prompt injection. That has not
    happened so far, and H1 already keeps container-written state from
    acting on the host or on `main`.

### Requirements to carry into the contract (PAC-*)

- **PAC-R1 — the write map.** Before any design, list for each kind of run
  what it must be able to write, and what it must only read:
  - its worktree;
  - its private HOME, per provider;
  - its refs and objects;
  - caches (`uv` and the like);
  - the run directory;
  - the MCP server's state, for runs that may spawn.

  This map becomes the mount list for each container.
- **PAC-R2 — isolation tests, written first.** These are the acceptance
  criteria, as black-box tests that run under docker:
  - run A cannot modify, delete or read the private files of run B's
    worktree, HOME or run directory;
  - run A cannot move or delete run B's ref, nor make B's objects
    unreachable;
  - a `readonly` run cannot write to its worktree. This would make
    `readonly` a real boundary for the first time.
- **PAC-R3 — Git without shared write access.** Mounting the shared `.git`
  into every container would recreate today's sharing.
  - **First candidate (advisor, 2026-10-03):** an independent repository for
    each run.
    - It is seeded from a **bundle that the host produces**, holding the
      approved base and dependency commits only.
    - The host imports the run's designated result into a staging ref,
      under H3's protections.
  - **Rejected as the first design:** read-only alternates into today's
    shared object store. They prevent writes, but they expose every other
    run's committed content, and they depend on how the host prunes.
    Alternates may come later, as an optimisation against an immutable,
    authorised snapshot.
  - A plain linked worktree keeps Git's administration shared, so it is not
    an option either.
  - The design must keep the H1 invariant: container-written state never
    authorises a host mutation outside its domain. The domain is now the
    run, not the project container.
- **PAC-R4 — spawning goes through the host.** A run never gets the docker
  socket, under any circumstances. A run that delegates creates a node, and
  the host-side scheduler creates its container. This is the link with
  part 1.
- **PAC-R5 — credentials per container.**
  - claude already goes through the auth proxy.
  - For file-based tokens (codex, agy), decide between a copy per container
    and a shared mount. With copies, refreshes collide. With a shared mount,
    the sharing comes back.
- **PAC-R6 — resources per run.** Each container gets its own memory, CPU
  and pid limits. D1 reports an OOM kill per run, with evidence (LN-R6).
  - The total across concurrent runs must fit the host. `max_concurrent`
    and the per-run memory have to be reconciled.
- **PAC-R7 — lifecycle.** Adapt everything that assumes one long-lived
  container:
  - SV, survival and adoption;
  - SP, session persistence;
  - SF and SR, steer and death detection through pids and `/proc`;
  - docker stop walking the process tree;
  - mount-drift detection.

  Config changes then apply to the next run, and **no longer require
  killing every run**.
- **PAC-R9 — MCP authority goes through the host** (advisor).
  - Today a run that may spawn starts an MCP server inside the container,
    with access to the shared state (`executor/docker.py` ~906,
    `runner.py` ~3840).
  - Replace that with a scoped RPC endpoint on the host, optionally behind
    a stdio shim inside the container.
  - Bind the caller's identity and its permitted operations to
    capabilities the host issues. **Never trust a parent or run id that
    the run supplies.**
- **PAC-R10 — network isolation between runs** (advisor).
  - A run must not reach a sibling's MCP endpoint, its services or its
    credentials. Membership of the same internal network does not by itself
    isolate peers.
  - Shared auth and egress proxies are acceptable, with scoped
    authentication and routing. One proxy per run is optional.
- **PAC-R11 — the host owns credential refresh** (advisor).
  - Beyond PAC-R5's choice between copies and a shared mount, consider a
    host-side refresh broker that distributes access credentials into each
    run's private HOME. agy's renewal is already centralised
    (`executor/docker.py` ~1358).
  - Specify ongoing renewal and revocation.
- **PAC-R12 — durable identity for each run** (advisor).
  - Before launch, record the node, the execution attempt, the container
    id, the repository, the session and the capabilities.
  - Crash recovery and launch retries must be idempotent.
  - Retention after success is defined separately from discard, so that a
    reopened node can resume.
- **PAC-R13 — the acceptance tests go beyond PAC-R2** (advisor). Also test:
  - siblings' committed blobs are not readable;
  - a shared cache cannot be poisoned;
  - a `readonly` run cannot touch Git metadata;
  - an RPC identity cannot be forged;
  - a dependency's work is handed over when approved.

  Isolation must still allow a **deliberate** handover of work, without
  exposing work that is unrelated.

- **PAC-R8 — cleanup.** Discard removes the container and the worktree. An
  orphaned container from a crashed host is found and reported. It is never
  deleted blindly.

## Inputs from phase 6 (orchestrator, 2026-10-03)

**C15 residuals.** Advisor ag-b08808 verified them on C15's branch; they predate C15. The H1 isolation decision accepts both until one container per run exists.
- **Signals follow agent-writable pid files.** `kill_detached` and the liveness verdict trust `wrapper.pid` and `container.pid`, which the agent can write (`executor/docker.py` ~2476 and `_KILL_SCRIPT` ~154/160). A forged pid can make `stop_agent` signal another same-uid process, and a long-lived forged pid makes a verdict permanently "alive". Phase 7 must authorize signals and verdicts from a trusted, host-side launch anchor for both recorded pids.
- **Escaping descendants.** A descendant that calls `setsid()` leaves the wrapper's session and is not tracked. A per-run pid namespace or cgroup contains it.
