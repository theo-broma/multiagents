# H1: host authority over container-written state

**Status:** contract, written by the orchestrator on 2026-09-28. It covers
H1 in `phase6-hardening.md`.
- Ids are `HA-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.
- Research: ag-4da90f covered the branch-deletion lifecycle, and ag-d0cd7b
  covered the container mounts.
- The advisor reviewed the threat model (ag-3f9bba, turn 7) and found the
  forged-parent merge that HA-R3 closes.

## Threat model, which re-scopes H1

The research showed that inside the docker container these locations are
**all writable by every agent**:
- `tree.json` and the rest of `<project>/.multiagents/`, except `config/`;
- every worktree under `~/.multiagents/worktrees/<slug>/`;
- every per-agent HOME;
- `.git/objects`, `.git/logs`, `.git/worktrees`, `.git/refs` and
  `.git/refs/heads/agents` in the base repository.

Inter-agent isolation inside one shared container is therefore **not a
property the system has**. Mounts belong to the container, not to each
`docker exec`, so one agent can already delete another agent's loose ref,
reflog, worktree or objects without help from the host. That gap is recorded
as a known limitation, and per-agent containers are future work. H1 does not
fix it.

What H1 guarantees is narrower, and it holds:

> **The container is one trust domain. Container-written state may cause the
> host to act only inside that domain, never outside it.**

The **container domain `D`** is:
- `refs/heads/agents/*` refs that the host did **not** create;
- filesystem paths strictly inside the project's worktree root,
  `~/.multiagents/worktrees/<slug>/`, after symlinks are resolved;
- merges whose target is the host-recorded worktree of the node whose run
  just ended on the host.

**Outside `D`** is everything else. It includes:
- the base branch and the main checkout (`paths.root`);
- every other ref;
- every branch the host itself created;
- every path outside the worktree root.

A container agent must never be able to make the **host** process mutate
anything outside `D`. It may make the host mutate things inside `D`. The
container could damage those itself, so that grants it nothing new.

"Host" means the multiagents MCP server or CLI running outside the executor
container. A nested MCP server inside the container is not the host, and the
behaviours below constrain only what the host does.

## Behaviours

**HA-R1: host record.** The host keeps a durable record of the nodes it
spawned itself.
- Each entry holds:
  - the node id;
  - the agent name;
  - the exact branch;
  - the worktree path;
  - the parent id as the host knows it;
  - once it happens, the completion: `merged` or `discarded`, the time,
    and for a merge the resulting commit.
- Location: under `~/.multiagents/`, in a directory that no executor mounts
  into any container. The docker executor's mount list must never cover it.
  This is checked by test, not only by convention.
- The record survives a host restart. Nothing the container can write
  creates, alters or extends it.
- Verified by: a test that the docker mount list covers neither the record
  directory nor any parent of it; a test that editing `tree.json` does not
  change what the record returns; a test that a new `Runner` over the same
  project reads back a completed record.

**HA-R2: host mutations take their operands from the host record.**
- Scope: every host-side mutation, meaning each of these:
  - an explicit `merge_agent`;
  - an explicit `discard_agent`;
  - the auto-merge into a parent;
  - the merge of pending children;
  - `_cleanup` (worktree removal and branch deletion);
  - `_drop_if_empty`;
  - `reap_pending_branches`;
  - the reconciliation on `resume`.
- For host-created nodes, these operations take the branch name, the
  worktree path, the merge target and the parent link from the host record,
  **never** from `tree.json`.
- Where `tree.json` disagrees with the record for a host-created node, the
  host acts on the record, or refuses when the record does not permit the
  action. It then emits one `host_authority_mismatch` event. The event
  carries the node id, the action, and the names of the fields that
  disagreed.
- Verified by: tests that rewrite a host-created node's `branch`,
  `worktree` and `parent` in `tree.json` after spawn, then drive each
  operation above. The mutation either uses the recorded values or is
  refused, and the event is emitted.

**HA-R3: no auto-merge outside the domain.** On the host, the auto-merge
into a parent (`inside_tree: auto`) happens only when both the child and
the target parent are host-recorded, and the child's recorded parent is that
parent.
- A node whose `tree.json` parent is set, unknown to the host record, or
  different from the recorded one is **never** merged into:
  - `paths.root`;
  - the base branch;
  - any worktree other than its recorded parent's.
- Such a node stays `done`, with its branch kept, and the refusal is
  reported through HA-R2's event.
- Landing on the base branch still happens only through an explicit
  `merge_agent`.
- This closes the attack the advisor found: a top-level agent rewrites its
  own `parent` to a nonexistent id, and when its run ends the host merges its
  branch into the main checkout (`runner.py` ~2635–2666 currently falls back
  to `paths.root`).
- Verified by: a test that sets a host-spawned depth-1 node's `parent` to a
  nonexistent id before the run ends, and asserts that the base branch and
  `paths.root` are unchanged.

**HA-R4: branch deletion.** The host deletes a ref under `refs/heads/agents/`
in exactly two cases:
1. **Host completion.** The host record shows that the host completed that
   node (merged or discarded), and the record binds exactly that branch.
2. **Nested pending deletion, inside `D`.** The branch is not the recorded
   branch of **any** host-created node, whatever that node's state. It is
   under `agents/`, and its name passes `check-ref-format`. Its node is
   terminal in `tree.json` and carries `branch_pending_delete` equal to that
   branch. These are today's checks, which remain.

   This is the legitimate path for a completion that happened inside the
   container. A forger can only reach branches in `D`, which it can already
   delete, but a genuine nested merge or discard still gets its branch
   removed.

The host never deletes:
- a branch outside `agents/`;
- the base branch;
- a host-created branch the host has not itself completed.

This holds whatever `tree.json` says.

Verified by:
- **Case 1:** a forged terminal node with `branch_pending_delete` naming
  another host-created node's unmerged branch. The branch survives both the
  reap and a host restart.
- **Case 2:** the same forgery naming the base branch, and naming a branch
  outside `agents/`. Both survive.
- **Case 3:** a genuine nested completion, simulated as a
  container-written terminal nested node that carries a pending deletion of
  its own non-host branch. Its branch is deleted by the next reap.
- **Case 4:** a host-completed node. Its branch is deleted, and its record
  entry shows the completion.

**HA-R5: worktree removal.** The host removes a worktree only at one of two
paths:
- the host-recorded path of a host-created node;
- for a nested node, a path that resolves (realpath) strictly inside
  `~/.multiagents/worktrees/<slug>/` and is not the recorded worktree of any
  host-created node that is still unfinished.

Any other path is refused with `host_authority_mismatch`, and nothing is
deleted. The case that matters most is a path under `paths.root`, `$HOME`,
or reached through a symlink out of the root.

Verified by:
- a forged node whose `worktree` points at a directory outside the root,
  including through a symlink placed inside the root. The directory survives
  every host operation in HA-R2.
- a forged node whose `worktree` is a live host-created agent's worktree.
  That worktree survives.

**HA-R6: pending children are merged only inside the domain.** When a host
node's run ends, `_merge_pending_children` merges only into that node's
**recorded** worktree.
- It merges only branches that are under `agents/` and are not the
  recorded branch of any host-created node.
- A forged child naming the base branch, a non-`agents/` ref, or another
  host-created node's branch is skipped with `host_authority_mismatch`.
- Legitimate deferred nested merges keep working. The existing behaviour is
  that a child's merge is deferred while its parent is running and done when
  the parent's run ends.
- Verified by:
  - a forged child whose `branch` is `main`, or another top-level agent's
    branch. That branch is neither merged nor deleted.
  - a genuine nested child deferred while its parent ran. It is merged into
    the parent's branch when the parent ends, as today.

**HA-R7: migration.** On the first host start after this change, the host
seeds its record once from `tree.json`. It takes every node with a `branch`
or a `worktree`, and marks each entry with `seeded` provenance.
- Seeding never repeats. A node that appears in `tree.json` later is not
  added unless the host spawns it.
- The trade: the seeding trusts `tree.json` as it stands at upgrade time.
  That is accepted, and documented beside the code.
- Without seeding, the roughly 20 existing unmerged `agents/*` branches
  would fall under HA-R4 case 2 and become deletable by a forgery.
- Verified by:
  - a tree with pre-existing nodes becomes a seeded record after one host
    start;
  - a node added to `tree.json` after that start is not host-recorded;
  - a seeded branch is protected exactly as HA-R4 protects a host-created
    one.

**HA-R8: nothing else regresses.** These keep their current behaviour:
- nested merge, discard and pending deletion inside the container
  (sandbox-git SG-R2);
- explicit merges into the base branch;
- conflict handling;
- the local executor. It has no container, so the record is still kept, and
  the rules hold trivially.
- Verified by: the existing suite stays green, apart from the 72 known
  phase2 reds.

## Out of scope, and recorded

- Inter-agent isolation inside the container: the threat-model section
  above. **Known limitation**; per-agent containers are future work. It is
  recorded in `sandbox-git.md` and in BRIEF.
- Git executing configured programs during a host merge (hooks, filters,
  drivers, fsmonitor) is **H3**, not H1. The merges that HA-R6 still
  performs are in H3's audit.
- `events.jsonl` is output, not authority. No host mutation may start
  reading it as authority.

## Adversary brief (for the phase after green)

Work from inside the container, as an agent with write access to everything
listed in the threat model. Make the **host** process do one of these:
- change the base branch or `paths.root`;
- delete or move a ref outside `D`, or a host-created branch it did not
  complete;
- delete any path outside `~/.multiagents/worktrees/<slug>/`;
- merge anything into a worktree other than the ending node's recorded one.

Anything the container can do to `D` directly is not a finding.
