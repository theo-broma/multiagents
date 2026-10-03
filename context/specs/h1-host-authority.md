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

**Container-written state alone never authorises a host mutation outside
`D`.** An explicit host action may act outside `D`, because that is its job.
Examples are the orchestrator's `merge_agent`, `discard_agent` and
`push_branch`, or a user's CLI command. But it acts only with operands the
host holds (HA-R1), never with ones read from container-writable state.
Container-written state may make the host mutate things inside `D`. The
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
- Ordering: the host records a node's branch and worktree **before** any
  container-visible state exists that could request their deletion, whether
  in `tree.json` or in the worktree.

**HA-R2: host mutations take their operands from the host record.**
- Scope: every host-side mutation, meaning each of these:
  - an explicit `merge_agent`;
  - an explicit `discard_agent`;
  - the auto-merge into a parent;
  - the merge of pending children;
  - `_cleanup` (worktree removal and branch deletion);
  - `_drop_if_empty`;
  - `reap_pending_branches`;
  - the reconciliation on `resume`;
  - `push_branch` (its ref);
  - `multiagents clean --branches` (`cli.py` ~1736–1761);
  - every worktree move or reset done by steer and by conversation refresh
    (`runner.py` ~3149–3241, ~3489–3580).
- For host-created nodes, these operations take the branch name, the
  worktree path, the merge target and the parent link from the host record,
  **never** from `tree.json`.
- Where `tree.json` disagrees with the record for a host-created node, the
  host acts on the record, or refuses when the record does not permit the
  action. It then emits one `host_authority_mismatch` event.
  - The event carries `node` (the node id), `action`, and `fields`: a list
    of the names of the fields that disagreed, such as
    `["branch", "parent"]`. It may also carry `reason`.
  - HA-R5 and HA-R6 refusals use the same event and shape. For a path
    refusal, `fields` is `["worktree"]`.
- Verified by: tests that rewrite a host-created node's `branch`,
  `worktree` and `parent` in `tree.json` after spawn, then drive each
  operation above. The mutation either uses the recorded values or is
  refused, and the event is emitted.

**HA-R2a: operands of unrecorded nodes** (reviewer ag-84303b, 2026-09-29).
An unrecorded node, meaning one the host did not spawn and did not seed,
may supply a host operand only inside `D`.
- **Branch.** Its `branch` must be under `agents/`, pass
  `check-ref-format`, and not be the recorded branch of any host-recorded
  node. Otherwise `merge_agent`, `push_branch`, `discard_agent` and every
  other host action refuse, with `host_authority_mismatch` and
  `fields: ["branch"]`.
- **Worktree.** Its worktree must satisfy HA-R5's nested rule **at the
  moment of every use**, not only at a first check. That applies to steer's
  `move_aside`, to conversation refresh's reset, to resume's commit, and to
  removal.
  - A check followed by a later use of the same pathname is not compliant.
  - The use must go through a pinned or descriptor-based path, or must fail
    closed when the path changes.
- **Resolution.** Containment compares **resolved** paths on both sides:
  the root and the candidate. So a `~/.multiagents` that is a symlink to
  another disk does not refuse legitimate recorded paths.
- **Host-cleared operands stay cleared.** When the host itself legitimately
  clears a recorded node's branch or worktree, as `_drop_if_empty` does for
  a completed read-only node, the record is updated. Later host actions
  such as steer must not resurrect the deleted operand from the record.
- Verified by:
  - a forged unrecorded node with `branch: "main"`: `push_branch` and
    `merge_agent` refuse, and `main` is neither pushed nor merged;
  - the same node naming another host-recorded node's branch: refused;
  - a nested worktree swapped for a symlink to an outside directory
    between the check and the use: steer and refresh leave the outside
    directory untouched;
  - a root reached through a symlinked `~/.multiagents`: a recorded
    worktree is merged, committed and removed normally;
  - a completed read-only node cleared by `_drop_if_empty`, then steered:
    the steer takes the branchless-checkout recovery path, and no branch is
    restored.

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
- the host-recorded path of the exact host-created node whose
  host-authorised completion is being cleaned;
- for a nested node, a path that resolves (realpath) strictly inside
  `~/.multiagents/worktrees/<slug>/` and is not the recorded worktree of
  **any** host-recorded node, whatever its state.

Containment must still hold at the moment the removal runs. A realpath
check followed by a separate command that resolves the path again leaves a
symlink-swap race, and is not compliant.

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
or a `worktree`, and marks each entry with `seeded` provenance, meaning
unverified.
- A seeded entry **protects**:
  - its branch is never reaped under HA-R4 case 2;
  - its worktree is never removed under HA-R5's nested rule.
- A seeded entry **authorises nothing automatic**:
  - no auto-merge (HA-R3);
  - no merge of pending children into it (HA-R6);
  - no automatic cleanup.
- Only an explicit host action may act on a seeded entry: `merge_agent`,
  `discard_agent` or `clean`. That completion is then recorded normally.
- A seeded worktree path outside the worktree root, or reached through a
  symlink, is quarantined. It stays protected but is never used as an
  operand.
- Seeding runs before the constructor's reaper call (`runner.py` ~455),
  under a host-only lock. Its completion is marked atomically in the
  host-only directory.
- An interrupted seed fails closed: nothing is reaped until a seed
  completes. An interrupted seed leaves no completion marker. The next host
  start discards any partial entries and re-attempts the seed from scratch,
  and reaps only once it has completed.
  - Testable seam: inject a fault into the seed, for example by making the
    tree read raise part-way. The constructor must then not reap, and a
    forged pending branch survives.
- Seeding never repeats. A node that appears in `tree.json` later is not
  added unless the host spawns it.
- Without seeding, the roughly 20 existing unmerged `agents/*` branches
  would fall under HA-R4 case 2 and become deletable by a forgery.
- Verified by:
  - pre-existing nodes become seeded entries after one host start;
  - a node added after that start is not recorded;
  - a seeded branch survives a forged pending deletion;
  - a seeded node is not auto-merged;
  - after a simulated interrupted seed, reaping stays disabled on the next
    start until the seed completes.

**HA-R8: nothing else regresses.** These keep their current behaviour:
- nested merge, discard and pending deletion inside the container
  (sandbox-git SG-R2);
- explicit merges into the base branch;
- conflict handling;
- the local executor. It has no container, so the record is still kept, and
  the rules hold trivially.
- Verified by: the existing suite stays green, apart from the 72 known
  phase2 reds.

**HA-R9: node identity comes from the key, never from the entry**
(adversary ag-387314, 2026-09-29). A node's identity is the key it is stored
under in tree.json. The `id` field inside an entry is container-written data.
- Every host action on a node resolves its record, and everything derived
  from it, from the key the host addressed. That covers:
  - the authority record;
  - branch;
  - worktree;
  - completion;
  - merge target;
  - pending deletion;
  - `clean`'s liveness check.

  The inner `id` never picks a different record.
- An entry whose inner `id` differs from its key is a mismatch:
  - the host emits `host_authority_mismatch` with `fields` containing `"id"`;
  - it performs no host mutation on that node, or on any node the inner id
    names;
  - read-only reporting (tree, status) may still show the entry.
- `clean --branches` decides liveness per key. Another entry's inner `id`
  can neither mark a node done nor unprotect it.
- Verified by: `tests/test_h1_adversary_id_alias.py`, all 5 tests:
  - drop-if-empty aliasing a live node;
  - forged branch merged;
  - forged branch pushed;
  - clean deleting a live node;
  - auto-merge into an unrelated worktree.

  Plus a mismatch-event assertion added by the tester.

**HA-R10: stop and resume checkpoints take their operands from the host
record** (adversary ag-ab23f4, 2026-09-29, findings 1 and 4).
- `multiagents stop` and the resume path resolve each node's worktree and
  branch the same way other host actions do: from the host record when the
  node is recorded, and under HA-R2a when it is not. The worktree must be
  inside the domain and the branch must be an `agents/*` ref.
- A checkpoint never runs in the project root, the user's main checkout, and
  never commits onto the base branch or onto another node's branch. This
  holds whatever tree.json says, including an empty or missing branch.
- A node whose operands fail these checks is skipped under HG-R9:
  - an event is emitted;
  - the node's reason is updated;
  - the other nodes are still processed.
- Verified by: `tests/test_h1h3_round2_stop.py`.

**HA-R11: `merge_agent(into=…)` never follows a registration HEAD it did not
resolve itself** (ag-ab23f4, finding 2).
- The merge target branch is determined by the host:
  - the recorded branch of the recorded node whose worktree `into` resolves
    to, compared after resolving the path (symlinks, spelling);
  - or, for an unrecorded target, an `agents/*` branch validated under
    HA-R2a.
- A merge with no host-determined target branch is refused. It never
  advances whatever ref the target's HEAD names.
- The base branch moves only through the explicit merge into base.
- Verified by: `tests/test_h1h3_round2_merge_into.py`.

*HA-R11 resolution order, decided 2026-09-29 (ag-691e46, confirmed by the
orchestrator):* the host determines the target branch in this order:
1. The record whose worktree resolves to `into`.
2. For an unrecorded checkout inside the domain, the branch of its single
   unrecorded tree node.
3. Failing that, the single worktree registration the host finds by scanning
   `.git/worktrees/*/gitdir`, not through the checkout's own `.git`.

Options 2 and 3 must pass `safe_unrecorded_branch`, meaning an `agents/*`
branch that no other record holds. A target outside the domain, or on a
non-`agents/*` branch, is refused. Test fixtures that merged into arbitrary
checkouts are updated to use recorded or in-domain targets.

**HA-R12: malformed entries never abort host-wide passes** (ag-ab23f4,
findings 4 and 5). A tree entry can have a wrong type in any field (a
non-string `worktree` or `branch`, a list, a missing required field), or can
fail to build a Node. Such an entry is skipped with an event, and the node is
treated as mismatched (no host mutation) by every pass that iterates the
tree:
- stop and resume checkpoints;
- `reap_pending_branches`;
- `Tree.active()` and its callers;
- `clean`.

It never raises out of them.
- Verified by: the type cases in `tests/test_h1h3_round2_stop.py`, plus a
  reap and active-tree case added by the tester.

## Out of scope, and recorded

- Inter-agent isolation inside the container: the threat-model section
  above. **Known limitation**; per-agent containers are future work. It is
  recorded in `sandbox-git.md` and in BRIEF.
  - **The user decided on 2026-10-03:** accept the limitation for now, and
    remove it in phase 7 with one container per run.
  - The intermediate step of one Unix uid per agent was rejected.
  - See `phase7-nodes-and-containers.md`, part 2 (PAC-R1..R8).
- Git executing configured programs on the host (hooks, filters, drivers,
  fsmonitor) is **H3**, not H1. H3's audit covers two paths:
  - the merges that HA-R6 still performs;
  - the host CLI's `commit_all()` of an interrupted agent's worktree on
    stop or resume (`cli.py` ~940–965, ~2528–2560).

## Testing note

In production, `DockerExecutor.inside()` decides host versus nested
(`docker.py` ~590–611). It checks `/.dockerenv` and the container marker in
`/proc/1/environ`. Unit tests may inject or monkeypatch its result.
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
