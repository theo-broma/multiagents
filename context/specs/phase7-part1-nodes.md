# Phase 7, part 1: plan nodes, composite nodes and the scheduler

**Status: requirements, agreed with the user on 2026-10-04.**
- **Written by:** the initializer, with the advisor (ag-aed397, turns 5–6).
- **What it is:** the user's decisions and the requirements that follow
  from them. It is **not** the interface contract. The orchestrator writes
  that contract in its phase 2, citing these ids, then runs tester →
  implementer → reviewer as usual.
- **Ids:** `NS-R*` and `NS-D*`. They are never renumbered.
- **Background:**
  - BRIEF, "Ticket-driven agent scheduling (the user, 2026-10-02)";
  - `phase7-nodes-and-containers.md`, the phase 7 seed. Part 2, one
    container per run, builds on this part.

## The user's decisions (2026-10-04)

- **NS-D1 — the orchestrator plans, a script launches.**
  - The orchestrator keeps every decision. It **deposits nodes** and may add,
    remove and modify them at any time.
  - On each node it sets the dependencies, priority, concurrency rules, loop
    rules and time window.
  - A host-side script, **the scheduler**, reads the plan and launches runs
    when the rules allow it.
  - The orchestrator never waits for a free slot: it can plan all the work
    ahead.
- **NS-D2 — only the orchestrator merges into main.**
  - When a composite node's work is finished, the scheduler notifies the
    orchestrator, whether the loop exited or it reached its maximum.
  - The orchestrator then merges into main, relaunches the loop, or
    abandons the work.
- **NS-D3 — one branch per top-level composite node, one worktree per
  run.**
  - The branch is `nodes/<id>`, created from main.
  - Each run works in its own worktree, based on the tip of that branch.
  - Only the host advances the branch.
  - main is never touched while a node is running.
- **NS-D4 — composite nodes carry no rules imposed by the program.**
  - The engine supplies primitives only. The rules are what the
    orchestrator sets, or what a template from the **template library**
    supplies.
  - The user's example of an implementation template:
    `{{tester-A <-> reviewer-B} -> {implementer-C <-> reviewer-B}}`.
    This is a sequence of two loops, each with its own rules.
- **NS-D5 — a reused letter means the same session.** In the example,
  reviewer-B reviews the implementation in the **same conversation** in
  which it reviewed the tests.
- **NS-D6 — everything ships together.** Loops, structured verdicts and
  templates are all in part 1, not staged. The initializer recommended
  staging and the user chose otherwise. Internal milestones are allowed
  (NS-R18), but part 1 is done only when all of them are in.
- **NS-D7 — time windows are complete in part 1.** That includes pausing a
  running run when its window closes.

## Vocabulary (NS-R1)

- **node:** the planned unit, stored in the plan.
- **run:** one execution of a simple node. Today's tree "nodes"
  (`node_id`, `agent_tree`) are runs. Rename them or keep them as
  aliases; existing tools keep working.
- **attempt:** one launch try of a run. Its id is recorded **before**
  launch (NS-R6).
- **session alias:** a named conversation shared by every activation of
  one template letter inside one instance (NS-R9).
- **generation:** one recorded result of a node, as a commit plus a
  sequence number. Dependents bind to a generation (NS-R10).

## Requirements

**NS-R2 — primitives.** Validation is mandatory. "No imposed rules" never
means accepting an invalid graph.
- **Simple node:** an agent, a task, and optional model, effort and
  provider pins. It produces runs.
- **Composite node:** child nodes, with dependencies among them. Children
  with no dependency between them may run in parallel.
- **Sequence (`->`):** a composite in which each child depends on the
  previous one.
- **Loop (`<->`):**
  - the children run in order;
  - one designated child's **verdict** (NS-R11) decides whether another
    round runs;
  - a rejected round relaunches the first child, with the verdict's
    findings injected;
  - the loop has a counter and a maximum (NS-R12; `on_max` withdrawn
    2026-10-04).
- **On any node:** dependencies, priority, locks and a time window.

**NS-R3 — fields of a node.** The contract fixes the schema. It covers at
least:
- the id and the parent composite;
- the kind, and the agent, task and pins;
- `depends_on`: node ids, each with the outcome it requires (default:
  approved);
- `inputs`: the commits it builds on. These are separate from ordering;
  an ordering dependency need not bring in code;
- `urgent`: a boolean, default false;
- `locks`: named locks;
- the window;
- the loop spec;
- the status;
- the revision;
- the runs and their attempts;
- the generations.

Eligibility and the reason something is blocked are **derived**. They are
never stored as truth.

**NS-R4 — the plan store.**
- **Location.** It is owned by the host and never writable from a
  container. It is kept **separate** from `HostAuthority`'s `nodes.json`,
  which records branch and worktree operands.
- **Writes.** They go through transactions, with a revision number per
  node and per plan, so that a conflicting edit is refused rather than
  merged silently.
- **Validation on every write.** Refused:
  - a reference to a node that does not exist;
  - a dependency cycle;
  - a loop with no designated verdict child;
  - an invalid window;
  - a session alias used incompatibly (two providers, for example).

**NS-R5 — authority: a scoped host RPC, moved forward from PAC-R9.**
- **The interface.** Every operation on nodes goes through a host
  endpoint. The caller's identity and its permitted operations come from
  capabilities that the host issues.
- **Never trusted:** a parent id or a run id that the caller supplies.
  Today a missing caller id means root (`server.py` `_may_act_on`); that
  must not carry over.
- **The orchestrator** may act on any node.
- **A run that delegates** creates nodes only inside its own subtree.
- **The parent of a launched run** is the node's recorded parent. It is not
  derived from whoever happened to call (`Runner.start` uses `self_id()`
  today).

**NS-R6 — the scheduler process.**
- **One per project.** It runs on the host, behind a singleton lock, and
  is started by `multiagents run`.
- **It is independent of the orchestrator.** It keeps working through the
  orchestrator's compactions, restarts and absence.
- **Shutdown and restart** are specified: what happens to running runs,
  and what is reconciled at start.
- **Recovery.** An attempt id is persisted **before** launch. At restart,
  every attempt is reconciled (not launched, launched, or recorded), so a
  node never launches twice.
  - One recovery model covers launches, verdicts, integrations, pauses and
    loop transitions. Each of these transitions has a durable identity and
    is safe to replay.
- **Admission.** The scheduler reuses today's admission checks: provider
  concurrency (PC), spend caps (SC), quotas, routing and fallbacks. For
  planned work, the deferred queue and the PC queue become **reasons a node
  is not eligible**, not separate queues. Migrating existing entries is
  part of the contract.

**NS-R7 — ordering.**
- **The order.** Urgent nodes first, then FIFO by deposit time. This is
  the user's original rule.
- **Starvation.** A node that has been eligible for longer than a
  configurable threshold without launching is **reported** to the
  orchestrator, as a `wait_for_nodes` transition. It is not reported to the
  user, and it is never aged automatically (confirmed by the user,
  2026-10-04).
- **Legacy `start_agent`.** It creates a node with ordinary priority,
  unless `urgent` is passed.

**NS-R8 — concurrency rules: named locks.**
- **The rule.** A node that names a lock does not launch while another
  run holds that lock. An example is `runner.py`, for two implementers
  that would both edit it.
- **Release.** A lock is released only once the holder's termination is
  **confirmed**.
- **Not in v1:** a per-node `exclusive_with` list. A named lock covers it.

**NS-R9 — shared sessions (NS-D5).**
- **Scope.** A session alias is scoped to one instance of a top-level
  composite. Two instances of the same template never share a session.
- **One turn at a time.** An alias has at most one active turn. This is
  an implicit scheduler lock.
- **The provider and account are frozen** after the alias's first launch.
  If they are unavailable, the node is blocked and the orchestrator
  notified. Another account or session is never substituted silently.
  - Changing the model needs either a same-session resume the provider
    supports, or an explicit decision by the orchestrator.
- **Compaction and loss.**
  - Native compaction keeps the alias.
  - A lost session that cannot be resumed blocks the node and notifies the
    orchestrator, which may authorise a replacement session.
- **Each activation is a new run.** It gets its own worktree and an
  immutable input commit, and it keeps the conversation. Each activation is
  given its new task, the generation under review and its working
  directory explicitly. It is never done by blindly steering the old
  worktree.
- **Persistence.** Alias bindings persist across scheduler restarts.

**NS-R10 — Git (NS-D3).**
- **Where the work goes.** A run's result is a recorded commit. The host
  integrates it into `nodes/<id>` under H1/H3's protections. It never
  follows a ref that an agent wrote.
- **Parallel children.**
  - When their results do not conflict, the host integrates them.
  - On a conflict, the scheduler stops and notifies the orchestrator. It
    never resolves a conflict itself.
- **Integration is not approval.** A rejected implementation is integrated
  so that its reviewer and its next revision can see it. It does not
  become a dependency anyone can build on until a verdict approves its
  generation.
- **Dependents** bind to an approved **generation**, never to "node
  closed".
- **Reopening a predecessor** blocks dependents that have not started, and
  notifies the orchestrator about dependents that are running or done. It
  never cancels anything automatically.
- **Merge into main** is the orchestrator's alone (NS-D2), using today's
  merge path and its protections.

**NS-R11 — structured verdicts.**
- **How a verdict is given.** Through an explicit, scoped RPC tool, not by
  parsing the end of the transcript.
- **What it carries:** approved or rejected, plus findings.
- **What it is bound to:** the reviewed generation and the designated
  child. A verdict from any other caller, or about another generation, is
  refused.
- **No verdict.** A designated child that finishes without one leaves the
  round **unresolved**, and the orchestrator is notified.
- **What a verdict is.** It is evidence about the task. It is never
  authority to modify main.

**NS-R12 — loops.**
- **The counter.** It counts **rejected rounds**.
  - Infrastructure failures do not count: a crash, a quota, a refused
    launch. They are reported.
  - A pause for a window is not a round either (NS-R14).
- ~~`on_max`~~ — **withdrawn 2026-10-04 by the user** (plan
  `2026-10-04-phase7-part1-nodes.md`). There is no `on_max` setting, and
  neither `close` nor `escalate` exists.
- **At its maximum, a loop always calls on the orchestrator.** It stops,
  notifies the orchestrator, and waits. The orchestrator decides:
  relaunch (with a higher maximum, a different model, or both), close the
  loop as **exhausted**, or anything else the node tools allow.
  - Exhausted never means approved, and it never unlocks dependents that
    require approval.
  - "Round 3 → opus" is an orchestrator decision its instructions may
    describe; the engine does not automate it.
  - If a relaunch changes the model of a child that shares a session
    alias, NS-R9 applies: a same-session resume the provider supports, or
    an explicit new session decided by the orchestrator.
- **The end of a loop** (exit, maximum reached, or closed as exhausted) of a top-level
  composite always notifies the orchestrator (NS-D2).

**NS-R13 — the template library.**
- **Format.** Declarative YAML, with a version, typed parameters, and
  explicit references to nodes and sessions. No executable expressions, no
  shell interpolation, and no implicit substitution of a string into a
  structure.
- **Where templates live.**
  - Shipped templates are under `defaults/node-templates/`.
  - Global and project overrides replace a template by its whole name.
  - Project templates are registered by the orchestrator through the
    authenticated RPC, into a registry owned by the host.
  - A file that an agent can write is never loaded as scheduler
    instructions.
- **Validation.** Both the template and its fully expanded graph are
  validated, under NS-R4's rules, plus session compatibility.
- **The plan freezes the template.** It records the expanded definition,
  the bindings, and the template's version and hash. A later edit to the
  library never changes a plan that already exists.
- **Shipped first:** the user's implementation template (NS-D4) and a
  single review loop. Whether there are others is the contract's call.

**NS-R14 — time windows (NS-D7).**
- **What a window is.** Days of the week plus hour ranges, in a named time
  zone. Defaults:
  - the time zone is Europe/Paris by default, set in the general
    multiagents config (global, overridable per project; the contract
    names the key). A window may still name its own zone;
  - ranges may cross midnight;
  - intervals are half-open;
  - DST is handled.
- **Inheritance.** The effective window is the **intersection** of the
  node's own window and its ancestors'. An empty intersection is reported
  as a block.
- **When a window closes:**
  - the active runs of the descendants are stopped;
  - the stop is a **suspension**. It is never a completion, a verdict or a
    loop round;
  - execution slots and locks are released only once termination is
    confirmed. The session and the worktree stay reserved;
  - when the window reopens, slots and locks are re-acquired and the
    session is resumed, **before** the loop moves on.
- **Precision.** The scheduler's tolerance is stated. "Stopped exactly at
  the boundary" is not achievable.

**NS-R15 — the orchestrator's interface.** These are MCP tools, backed by
NS-R5's RPC:
- create, edit, cancel and list nodes;
- instantiate a template;
- read a node's status, with the derived reason it is blocked;
- `wait_for_nodes`, with a **durable cursor and acknowledgements**, so a
  reconnecting orchestrator loses no transition. Waiting is optional; the
  scheduler keeps working either way.
- **Compatibility.**
  - `start_agent` creates a node and returns **a node id immediately,
    plus a run id when there is one**. Its behaviour for existing callers
    is specified: `check_agent`, `wait_for_agents`, `collect_agent`.
  - Today's run tools keep working on runs.
- **The orchestrator's instructions** (`_orchestrator.md`) are updated to
  plan with nodes. The live copy is patched the way PN-R6a was.

**NS-R16 — visibility.**
- **The monitor and `doctor`** show:
  - the plan;
  - eligible and blocked nodes, with their reasons;
  - locks;
  - session aliases;
  - windows;
  - the scheduler's state.
- **Events.** Every transition is recorded in `events.jsonl`.

**NS-R17 — acceptance.** The contract turns these into black-box tests.
- **The advisor's example, as a baseline:**
  - deposit three dependent tasks;
  - disconnect the orchestrator;
  - fill the slots, then release them;
  - restart the scheduler;
  - check that each task launches exactly once, with the right inputs.
- **The user's example** runs end to end:
  - tests rejected once, then approved;
  - implementation rejected until it reaches its maximum; the orchestrator
    is notified and relaunches it with another model;
  - reviewer-B in the same session across both loops;
  - the orchestrator is notified at the end and merges.
- **Adversarial tests:**
  - forging an RPC identity;
  - a run creating a node outside its subtree;
  - a verdict from the wrong child or about the wrong generation;
  - a rejected generation used as an input;
  - a double launch after a crash between "claimed" and "recorded";
  - a lock released before termination;
  - a window closing mid-loop.

**NS-R18 — internal milestones.** Each is merged progressively behind a
feature gate. Part 1 is done only when the whole contract passes.
1. The protected plan store, the scoped RPC, revisions, durable
   notifications, and schema and expansion validation.
2. Scheduler admission and attempt recovery, simple runs, priorities,
   locks and session aliases.
3. Node branches, recorded results, conflict-safe integration, and
   dependencies bound to a generation.
4. Scoped verdicts, sequences and loops, the maximum and the
   orchestrator's relaunch or close, and
   template instantiation.
5. Window evaluation, stop and resume with recovery, and inherited
   windows.
6. The compatibility tools, the monitor and `doctor`, and the end-to-end
   tests for restarts and adversarial cases.

**NS-R19 — no regression.** With the feature gate off, the behaviour is
exactly as it is today. The full suite gives only the known reds.

## For the contract to settle, with defaults

- **Where the scheduler lives.** By default, a process that
  `multiagents run` launches. `multiagents scheduler status|stop` is
  available.
- **The starvation threshold.** By default, 2 hours.
- **Window tolerance.** By default, 60 s.
- **Today's tree nodes.** The default is to keep `node_id` and
  `agent_tree` as aliases of the runs rather than rename them.
