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
  - Evaluate a per-run clone or worktree that reads the base objects through
    a read-only alternate, and whose result the **host** fetches.
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
- **PAC-R8 — cleanup.** Discard removes the container and the worktree. An
  orphaned container from a crashed host is found and reported. It is never
  deleted blindly.
