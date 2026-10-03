# Phase 7 part 1 — interface contract: plan nodes, composites, the scheduler

**Status:** contract, orchestrator, 2026-10-04. Draft for the advisor.
- **Implements:** `phase7-part1-nodes.md` (NS-D1..D7, NS-R1..R19, with the
  user's corrections of 2026-10-04). Where this file and that one disagree,
  this file wins; where this file is silent, that one applies.
- **Ids:** `NC-R*`. Never renumbered; withdraw as `NC-Rn — withdrawn: <why>`.
- **Milestones:** each requirement names its NS-R18 milestone `[M1]`..`[M6]`.
  Tests are named `test_nc_r<n>_*`. Adversary is mandatory on M1, M3, M5.

## 0. Gate, configuration, locations

**NC-R1 — the gate.** `scheduler.enabled` in `project.yaml` (shipped `false`;
global may set it, project overrides).
- **Off:** behaviour is exactly today's (NS-R19). No scheduler process, no
  socket, no extra container mount. Node tools exist but return
  `{"error": "scheduler_disabled"}` without side effects.
- **On:** the scheduler is the only launcher of work (NC-R20). If it is not
  running, every node tool and `start_agent` return
  `{"error": "scheduler_unavailable"}`; there is no direct-launch fallback.
  `multiagents run` starts the scheduler (NC-R16) and refuses to launch the
  orchestrator if it cannot get it ready.
- **Verified by:** with the gate off the full suite shows only the known
  reds, and a node tool call leaves no file under the scheduler state dir;
  with it on and the scheduler stopped, `start_agent` returns
  `scheduler_unavailable` and creates no tree node. `[M1]`

**NC-R2 — keys.** All under `scheduler:`, layered shipped → global → project,
documented in `defaults/project.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | NC-R1 |
| `timezone` | `Europe/Paris` | default zone of windows (NS-R14); IANA name |
| `starvation_after_seconds` | `7200` | NC-R24 |
| `window_tolerance_seconds` | `60` | NC-R40 |
| `admission_timeout_seconds` | `10` | NC-R21 (`start_agent`'s synchronous attempt) |
| `tick_seconds` | `5` | upper bound between two evaluations when nothing wakes the scheduler |

An invalid value (unknown zone, non-positive number) is a config error with
file and line; `doctor` reports it as one problem.
- **Verified by:** precedence tests; doctor line for `timezone: Mars/Base`. `[M1]`

**NC-R3 — host state.** `state_root()/scheduler/<project-slug>/`, mode 0700,
files 0600. It holds the plan store, the capability registry, the template
registry, the notification log, the attempt journal and the singleton lock.
- It is never mounted into any container. The docker executor refuses a mount
  (configured or ancestor) that would expose it, as it does for
  `host-authority` (`executor/docker.py` ~1204).
- **The transport directory** `state_root()/scheduler-rpc/<project-slug>/`
  contains only the socket `rpc.sock` (mode 0600 dir 0700, owned by the user).
  With the gate on, the docker executor bind-mounts this directory, and only
  it, at the same path; it is part of the mount set, so mount-drift detection
  applies (recreating the container is the user's step, as today).
- **Verified by:** a configured mount of `state_root()` or of the scheduler
  dir is refused; the container mount list with the gate on contains the
  transport dir and nothing under `scheduler/`. `[M1]`

## 1. The node model (NS-R2, NS-R3)

**NC-R4 — node record.** Fields (JSON; the storage engine is the developer's):

| Field | Type | Notes |
|---|---|---|
| `id` | `nd-<8 hex>` | host-generated |
| `parent` | node id or `null` | plan parent (composite or the node of a delegating run) |
| `kind` | `simple` \| `sequence` \| `loop` \| `group` | `group` = composite with explicit `depends_on` among children |
| `agent`, `task` | str | simple only; required |
| `pins` | `{model?, effort?, provider?}` | simple only |
| `session` | alias name or `null` | simple only, NC-R30 |
| `children` | ordered node ids | composites only |
| `loop` | `{verdict_child, max_rounds, rounds_rejected}` | loop only |
| `depends_on` | `[{node, require}]` | `require` ∈ `success` (default), `approved`, `finished` |
| `inputs` | `[{node, generation?}]` | commits to build on; absent `generation` = latest approved at launch |
| `urgent` | bool, default false | |
| `locks` | `[str]` | NC-R26 |
| `window` | window or `null` | NC-R38 |
| `state` | `open` \| `running` \| `suspended` \| `held` \| `done` \| `cancelled` | stored |
| `hold` | `{reason, detail, since}` or `null` | set iff `state == held` |
| `outcome` | `approved` \| `rejected` \| `completed` \| `exhausted` \| `failed` or `null` | set iff `done` |
| `revision` | int | +1 per accepted write |
| `created_at`, `created_by` | time, capability subject | deposit time = `created_at` |
| `runs` | `[{run_id, attempt_id, generation?}]` | |
| `generations` | `[{seq, commit, run_id, verdict}]` | NC-R33 |
| `template` | `{name, version, sha256, instance, bindings}` or `null` | on the top-level node of an instance |

- `success` means `approved` for a node that has a verdict (a loop, or a
  simple node that is a loop's verdict child) and `completed` otherwise.
  `finished` is any `done` outcome.
- **Eligibility and blocked reasons are derived**, never stored (NC-R23).
- **Verified by:** schema round-trip; a stored `eligible` field is rejected
  on write. `[M1]`

**NC-R5 — composite semantics.**
- `sequence`: child *i* implicitly depends on child *i−1* with `success`.
- `group`: children run as their own `depends_on` allow; independent
  children may run in parallel.
- `loop`: see NC-R35.
- A composite is `running` while any descendant is `open`/`running`/
  `suspended`; it is `done` when its children are, with outcome: `sequence`/
  `group` = `approved` if every child succeeded, else `failed`; `loop` per
  NC-R35. A child's `held` holds the composite (reason `child_held`).
- `depends_on` on a composite gates all its descendants.
- **Verified by:** a sequence of three simple nodes launches in order; a group
  of two independent children launches both in one tick when slots allow. `[M4]`

**NC-R6 — validation on every write (NS-R4).** A write is refused, whole and
with no partial effect, with `{"error": "invalid", "problems": [...]}` when:
unknown node referenced (parent, child, dependency, input, verdict child);
dependency cycle (including implicit sequence edges and ancestor edges);
`loop.verdict_child` not among its children; `max_rounds < 1`; a simple node
without agent or task, or an unknown agent; an invalid window (NC-R38); a
session alias used by two nodes whose effective provider family differs, or
outside a template instance (NC-R30); a child with two parents; a write to a
`done`/`cancelled` node other than through NC-R36/R37.
- **Verified by:** one test per refusal; the store is byte-identical after a
  refused write. `[M1]`

**NC-R7 — revisions.** Every write names the `revision` it was based on
(`plan_revision` for creations, node `revision` for edits). A mismatch is
refused with `{"error": "conflict", "current_revision": n}`; nothing is merged
silently. Every accepted write is atomic and durable before the reply.
- **Verified by:** two edits based on the same revision: the second is
  refused; kill -9 after the reply leaves the write present. `[M1]`

## 2. Authority: the scoped RPC (NS-R5)

**NC-R8 — transport and protocol.** The scheduler serves `rpc.sock` (NC-R3):
one JSON object per line, request `{"op", "token", "args", "request_id"}`,
reply `{"request_id", "ok": bool, "result" | "error"}`. Ops are listed in
NC-R13. The socket is recreated at scheduler start; clients reconnect and
retry. `request_id` makes creating ops idempotent: replaying a request id
already applied for the same subject returns the original result (lost-reply
reconciliation).
- **Verified by:** a replayed `create_node` with the same request id creates
  one node; a client survives a scheduler restart by reconnecting. `[M1]`

**NC-R9 — capabilities.** Identity comes only from the token.
- **Root capability:** generated by the scheduler at first start, stored in the
  scheduler state dir, readable only by host processes; the orchestrator's MCP
  server (host) presents it. It never enters a container's environment, mount
  or file.
- **Run capability:** a random ≥256-bit token issued by the scheduler when it
  launches a run whose agent may spawn; the registry stores only its hash,
  bound to `(run_id, node_id, scope_root=node_id)`. It reaches the run's MCP
  server as `MULTIAGENTS_RPC_TOKEN`. Revoked when the run terminates
  (confirmed) or is cancelled; a revoked token is refused.
- **No token, unknown token, revoked token** → `{"error": "unauthenticated"}`.
  A missing caller id never means root (replaces `_may_act_on`'s `None` rule
  for every node op).
- Any `parent`, `run_id` or `caller` in `args` is ignored for identity; a
  `parent` outside the caller's scope is refused `forbidden`.
- **Verified by:** forged/absent/revoked token; a run token with a supplied
  `parent` of a sibling's node → `forbidden`; root token absent from
  `docker inspect` env and from every mounted path. `[M1]`

**NC-R10 — scope.** Root may do every op on every node. A run capability may:
create nodes whose `parent` is its own node or a descendant created under it;
read, edit, cancel and wait on that subtree; give a verdict only per NC-R34.
It may not merge, relaunch or close loops, register templates, or touch the
root's notification cursor. Its created nodes' `created_by` is the run.
- **Verified by:** each forbidden op returns `forbidden` and leaves the store
  unchanged. `[M1]`

**NC-R11 — accepted residual (part 1 only).** All runs share one container and
uid, so a run can read a sibling's token from `/proc/<pid>/environ` and act
within that sibling's scope. This is the same class as the C15 residuals
accepted by the H1 isolation decision until one container per run (part 2,
PAC-R*). Part 1 guarantees scope against forged ids, supplied parents and
revoked tokens, and keeps the root capability out of every container; it does
not guarantee scope against token theft by a same-uid sibling. Part 2 must
close it. Recorded here so it is not mistaken for a part 1 bug.

**NC-R12 — trusted launch context.** The scheduler launches through Runner
with an explicit context `{caller, run_parent, depth, node_id, attempt_id}`;
Runner's admission, child limits, depth, budget attribution, notices and tree
parent use it instead of `self_id()`. The process environment is never
mutated to carry it.
- `run_parent` = the run that created the node (delegation) or root; the
  plan parent is recorded separately on the node. When the creating run has
  finished, its delegated nodes still run; their transitions go to the root
  log (NC-R14) and to the creator if it is still live.
- **Verified by:** two concurrent launches with different contexts get the
  right tree parents and depths; a delegated child of a finished run still
  launches. `[M2]`

**NC-R13 — operations.** Exposed over RPC, and to the orchestrator as MCP
tools of the same names (NS-R15). Each returns the node(s) with derived
status (NC-R23).

| Op | Who | Args (main) |
|---|---|---|
| `create_node` | root, run (scoped) | node fields, `plan_revision` |
| `update_node` | root, run (scoped) | `id`, `revision`, partial fields |
| `cancel_node` | root, run (scoped) | `id`, `revision` — stops active runs (confirmed), state `cancelled` |
| `get_node`, `list_nodes` | root, run (scoped) | `id` / filter (`state`, `parent`, `eligible`) |
| `instantiate_template` | root, run (scoped) | `name`, `params`, `parent?`, `urgent?`, `window?` |
| `register_template`, `list_templates` | root / root, run | YAML text / — |
| `wait_for_nodes` | root, run (scoped) | `cursor?`, `node_ids?`, `timeout` |
| `ack_nodes` | root | `cursor` |
| `give_verdict` | run only | NC-R34 |
| `relaunch_node` | root | NC-R36 |
| `close_node` | root | NC-R37 |
| `merge_node` | root | NC-R39 |
| `scheduler_status` | root, run | — |

## 3. Notifications (NS-R15, NS-R16)

**NC-R14 — durable transitions.** Every node transition is appended to the
notification log with a monotonically increasing `seq`, and mirrored to
`.multiagents/events.jsonl` (kind `node.<transition>`). Transitions:
`created, updated, cancelled, eligible, launched, run_finished, integrated,
integration_conflict, verdict, round_rejected, loop_max, loop_exited,
unresolved_round, suspended, resumed, held, released, starving, done,
session_lost, empty_window, scheduler_started, scheduler_stopped`.
- **Verified by:** each transition in the scenarios below appears once, in
  order, in both places. `[M1]`

**NC-R15 — `wait_for_nodes` / `ack_nodes`.** Root's cursor is durable.
`wait_for_nodes()` without `cursor` returns transitions after root's last
acknowledged seq; it blocks up to `timeout` if there are none; it returns
`{transitions, next_cursor, capacity, scheduler}`. `ack_nodes(cursor)`
advances the acknowledged seq (never backwards). Un-acked transitions are
redelivered, so a reconnecting orchestrator loses none. A run's waits are
scoped to its subtree and not durable. Waiting is optional: nothing waits for
an ack.
- **Verified by:** transitions produced while no orchestrator is connected are
  all returned after reconnect; after `ack_nodes`, they are not. `[M1]`

## 4. The scheduler process (NS-R6)

**NC-R16 — lifecycle.** `multiagents scheduler start|status|stop` and
`multiagents run` (start if absent, wait until ready ≤ 10 s).
- Singleton: exclusive non-blocking `flock` on a lock file in the state dir,
  held for the process lifetime; a second start reports the live pid and
  exits 0 without starting.
- Independent of the orchestrator: it survives the driver's and the
  orchestrator's exit, compaction and restart. While a driver runs, the driver
  restarts a dead scheduler.
- `stop`: stop admitting and launching, finish in-flight store writes, record
  `scheduler_stopped`, release the lock. **Running runs are left running**
  (they are supervised as today) and reconciled at next start.
- `status`: pid, since, gate, counts by node state, held nodes with reasons,
  locks held, aliases bound, current windows, last tick.
- **Verified by:** two `start`s → one process; `stop` with a live run leaves it
  running and the next start reconciles it as `launched`. `[M2]`

**NC-R17 — attempt journal and recovery.** Before any launch side effect the
scheduler persists an attempt `{attempt_id, node_id, state: claimed}`. Runner
records `attempt_id` on the tree node in the same write that creates it (as it
does for `deferred_id`). Then `launched` (with `run_id`), then `recorded`
(result captured, NC-R33).
- At start, every non-final attempt is reconciled: `claimed` with a tree node
  carrying its id → `launched`; `claimed` with none → `abandoned` and the node
  is eligible again; `launched` → read the run's status; finished runs go
  through result capture. **A node never has two live runs from one
  eligibility.**
- Verdicts, integrations, suspensions, resumptions and loop transitions each
  carry a durable id; replaying one already applied is a no-op.
- **Verified by:** kill -9 between `claimed` and the tree write, and between
  the tree write and `launched`: after restart exactly one run exists per
  node. `[M2]`

**NC-R18 — admission reuse.** For each eligible simple node, in order
(NC-R24), the scheduler asks Runner to admit and launch with the trusted
context. Runner applies today's checks — provider concurrency, spend caps,
quota reserve, routing and fallbacks, depth and child limits — and returns
`started(run_id)` or `blocked(reason, retry_after?)`. In this mode Runner
writes **no** deferred or PC queue entry and kicks no dispatcher; the blocker
becomes a derived reason on the node (`admission:<code>`).
- **Verified by:** with PC capacity full, a node stays open with reason
  `admission:provider_concurrency` and `tree.json["deferred"]` is unchanged;
  when the slot frees, it launches once. `[M2]`

**NC-R19 — migration.** When the scheduler starts with existing
`tree.json["deferred"]` entries (normal and PC), each becomes a simple node
(agent, task, pins and recorded provider from its spec; `created_at` =
`queued_at`, or the entry time) under root, and the entry is removed, in one
idempotent step keyed by the entry id. A `refused` entry is migrated as a
`held` node with reason `admission:refused`.
- **Verified by:** three entries before start → three nodes, empty queue;
  a second start creates nothing. `[M2]`

**NC-R20 — one launcher.** With the gate on, every path that launches or
restarts work goes through the scheduler: `start_agent`, deferred restarts in
`wait_for_agents`, PC drains at release and reconciliation, and the
quota-reset resume. `steer_agent` on a run keeps today's behaviour, except it
is refused (`node_suspended`) for a run whose node is `suspended`, and
`stop_agent` on such a run cancels the suspension (node `held`, reason
`stopped_by_orchestrator`).
- **Verified by:** with the gate on, `wait_for_agents` never calls Runner's
  restart path (spy); a steer on a suspended node's run is refused. `[M2]`

## 5. Compatibility (NS-R15)

**NC-R21 — `start_agent` with the gate on.** Creates a simple node under the
caller's scope (`urgent` param, default false; `verifies`, `budget_tag`,
`model`, `timeout` carried on the node), then waits up to
`admission_timeout_seconds` for the scheduler's first admission attempt.
Returns `{node_id, agent_id?: run_id, status, blocked?: [reasons]}`. The node
id is returned even if no run started. Retrying the same call with the same
MCP request does not create a second node (NC-R8).
- `check_agent`, `collect_agent`, `steer_agent`, `stop_agent`,
  `merge_agent`, `discard_agent` accept run ids as today; given a node id they
  return `{"error": "node_id", "hint": "use get_node"}`.
- `wait_for_agents(ids)` accepts run ids as today; a node id in the list waits
  for that node's **current** run, and returns `no_run_yet` for a node with
  none. It never follows successive loop runs; that is `wait_for_nodes`.
- `agent_tree` and `node_id` keep meaning runs (alias kept, no rename).
- **Verified by:** existing MCP tests pass with the gate on except where they
  assert direct launch; `start_agent` under PC saturation returns a node id and
  `blocked`. `[M6]`

## 6. Ordering, eligibility, locks (NS-R7, NS-R8)

**NC-R22 — eligibility (derived).** A simple node is eligible iff: state
`open`; every `depends_on` satisfied; every input has an approved (or, for a
non-verdict node, completed) generation; every ancestor's dependencies
satisfied; the effective window is open (NC-R38); its locks are free; its
session alias has no active turn and its frozen binding is available; no
ancestor is `held`/`cancelled`/`suspended`; and admission does not block it.
The first failing conditions are returned as `blocked: [{code, detail}]`, codes:
`dependency, input, ancestor, window, empty_window, lock, session_busy,
session_unavailable, held, admission:<code>`.
- **Verified by:** `get_node` on a node blocked by each condition returns that
  code, and nothing named `eligible`/`blocked` is persisted. `[M2]`

**NC-R23 — `get_node` view.** Stored fields plus `eligible: bool`,
`blocked: [...]`, `eligible_since`, `active_run`.

**NC-R24 — order and starvation.** Eligible nodes launch in order: urgent
first (a node is urgent if it or any ancestor is), then by top-level ancestor
`created_at`, then own `created_at`, then id. A node eligible for longer than
`starvation_after_seconds` without launching produces one `starving`
transition per eligibility episode, to the orchestrator only; its priority is
never changed automatically.
- **Verified by:** urgent deposited last launches first; a node held off by
  admission for the threshold yields exactly one `starving`. `[M2]`

**NC-R25 — legacy priority.** `start_agent` nodes are ordinary unless
`urgent=true`.

**NC-R26 — named locks.** A node naming lock *L* is not launched while any
run holds *L*. *L* is acquired atomically with the attempt claim and released
only when the holder's termination is **confirmed** (Runner's
`predecessor_death_confirmed`). An unconfirmed stop keeps the lock and holds
the node (`termination_unconfirmed`).
- **Verified by:** two nodes on lock `runner.py` never overlap; a stop whose
  termination is not confirmed keeps the second blocked with `lock`. `[M2]`

## 7. Sessions (NS-R9)

**NC-R30 — aliases.** `session` names an alias scoped to one template
instance (`template.instance` of the top-level node). Outside an instance,
`session` is refused (NC-R6). Two instances never share an alias.
- **One active turn per alias** (implicit lock, reason `session_busy`).
- **Frozen binding:** at the alias's first launch the scheduler records
  provider, account (vault account or instance) and model. Later activations
  use exactly that binding; if it is unusable (quota, disabled), the node is
  blocked `session_unavailable` and the orchestrator notified once. Never
  substituted silently.
- **Persistence:** bindings and the provider session id survive scheduler
  restarts.
- **Verified by:** two instances of the shipped template keep distinct
  reviewer sessions; a disabled bound provider blocks rather than reroutes. `[M2]`

**NC-R31 — activation.** Each activation is a new run with its own run id and
attempt, an explicit task (the node's task + injected findings, NC-R35), the
generation under review and its working directory stated in the prompt.
Because providers find sessions by working directory, **an alias owns one
stable worktree path**; before each activation the host re-seats it: confirms
the previous turn terminated, captures its result (NC-R33), then checks out
the activation's input commit at that path on a fresh run branch and removes
untracked files — by host git under H1/H3. It never steers the old process.
- **Verified by:** reviewer-B's second activation runs in the same provider
  session, at the same path, at the new input commit, with no file left from
  the first. `[M4]`

**NC-R32 — model change and loss.** A relaunch that changes the model of an
aliased child is accepted only with `new_session: true`, or when the bound
provider declares `session_model_change: true` in `providers.yaml` (shipped:
codex true, others false). A lost session (resume fails as unresumable)
holds the node `session_lost`; the orchestrator may `relaunch_node` with
`new_session: true`. Native compaction keeps the alias.
- **Verified by:** model change without `new_session` on claude → refused;
  with it → a new provider session bound. `[M4]`

## 8. Git (NS-R10, NS-D3)

**NC-R33 — branches, results, generations.** The top-level node of a tree of
nodes that produce code gets branch `nodes/<id>` created from main's tip at its
first launch. Each run's worktree is based on its input: the latest approved
generation of each `inputs` node, or the tip of `nodes/<id>`.
- At run end the host reads the run branch tip by H3 host git from the H1
  record (never an agent-written ref), checks it descends from the run's input
  commit, and integrates it into `nodes/<id>` serialised per branch with an
  expected-tip check: fast-forward when possible, else a merge commit by the
  host with hooks and content programs off. A conflict leaves `nodes/<id>`
  unchanged, holds the node (`integration_conflict`) and blocks only that
  subtree.
- Each integration records a **generation** `{seq, commit, run_id,
  verdict: null}` on the node. Integration is not approval: a generation is
  usable as an input only once its verdict is `approved` (or, for a
  non-verdict node, its run `done`).
- **Reopening** (`update_node` to `open` on a `done` node, or a relaunch)
  blocks dependents that have not launched and notifies about launched ones;
  nothing is cancelled automatically.
- main is never written by the scheduler.
- **Verified by:** two parallel children touching different files are both
  integrated; touching the same lines → `integration_conflict`, branch tip
  unchanged, sibling subtree keeps running; a forged ref in the worktree's
  `.git` is not followed; a rejected generation passed as input → `input`
  block. `[M3]`

## 9. Verdicts and loops (NS-R11, NS-R12)

**NC-R34 — `give_verdict`.** Run-only op, args `{generation_seq, verdict:
approved|rejected, findings: [{summary, severity, evidence?}]}`. Accepted only
when the caller's run is the current activation of the loop's `verdict_child`
and `generation_seq` is the generation under review given to it; otherwise
`forbidden`. One verdict per activation; a second is refused. The verdict is
recorded on the generation and node; it never authorises anything on main. A
verdict child that ends without one leaves the round **unresolved**: node
`held` (`unresolved_round`), orchestrator notified. Today's transcript
`VERDICT(...)` parsing stays for runs outside nodes.
- **Verified by:** verdict from a sibling run, for another generation, twice,
  or by root → refused; no verdict → `unresolved_round`. `[M4]`

**NC-R35 — loop rounds.** Children run in order each round. Approved by the
verdict child → loop `done/approved`, `loop_exited`. Rejected → if
`rounds_rejected + 1 < max_rounds`: increment, relaunch the first child with
the findings injected into its task, `round_rejected`; else increment and the
loop stops: `held` (`loop_max`), `loop_max` transition, waiting for the
orchestrator. Infrastructure failures (crash, quota, refused launch) and
suspensions never count; they are reported (`run_finished` with the failure)
and the child is retried by the scheduler as a new attempt, bounded by today's
retry rules, else the loop is held (`run_failed`).
- **Verified by:** max 2: reject, reject → `loop_max` with counter 2 and no
  third launch; a crashed reviewer does not increment. `[M4]`

**NC-R36 — `relaunch_node`.** Root only. `{id, revision, max_rounds?,
pins?: {child_id: {model?, provider?, effort?}}, new_session?: [child_id]}`.
Valid on a loop `held` at `loop_max`/`unresolved_round`, on a `done` loop, or
on a simple node `held`/`done`. Raises the maximum (must exceed the counter),
applies pins (NC-R32 for aliases), and reopens; the counter is kept. A done
node's dependents follow NC-R33's reopening rule.

**NC-R37 — `close_node`.** Root only. `{id, revision, outcome: exhausted |
failed | approved}` on a `held` or `open` node; cancels nothing still
running (refused with `active_runs` if any). `exhausted` never satisfies
`success`/`approved`. `approved` by close is the orchestrator's explicit
decision and is recorded as such (`closed_by: root`).

**NC-R49 — end notification.** A top-level composite reaching `done`, `held`
at `loop_max`, or `unresolved_round` always produces a transition to root
(NS-D2).

## 10. Windows (NS-R14, NS-D7)

**NC-R38 — window spec.** `{timezone?: IANA, days: [mon..sun], ranges:
["HH:MM-HH:MM", ...]}`. Ranges are half-open `[start, end)`; `end < start`
crosses midnight and belongs to the start day; `00:00-24:00` is all day.
Membership of an instant is decided on its local wall time in the zone
(`timezone` or `scheduler.timezone`), so DST gaps simply never occur and
repeated hours count normally. Invalid: unknown zone, malformed range,
`start == end`, empty days.
- **Inheritance:** the effective window is the intersection of the node's and
  every ancestor's. An empty intersection over the next 14 days blocks the
  node `empty_window` and notifies once.
- **Verified by:** table tests: crossing midnight, Sunday→Monday, both 2026
  Paris DST transitions, intersection, empty intersection. `[M5]`

**NC-R40 — suspension and resumption.** Within `window_tolerance_seconds`
after the effective window of a node with active runs closes, the scheduler
stops those runs (Runner stop with confirmation) and marks each node
`suspended` (`suspended` transition). Suspension is never a completion, a
verdict or a round. Slots and locks are released only once termination is
confirmed; the session binding and the alias worktree stay reserved.
Within the tolerance after it reopens, the scheduler re-acquires slot and
locks (subject to admission) and resumes the **same session** with an
explicit "resume your interrupted task" activation, **before** the loop
advances; `resumed` transition. A resume blocked by admission leaves the
node `suspended` with the reason.
- **Verified by:** a window closing mid-loop: the reviewer is stopped, its
  lock released only after confirmation, the counter unchanged; on reopen it
  resumes in the same session before any next round; a scheduler restart while
  suspended preserves this. `[M5]`

## 11. Templates (NS-R13)

**NC-R41 — format.** YAML: `{template: <name>, version: <int>, params: {name:
{type: string|text|agent|model|int|bool, default?}}, root: <node>}` where a
node is `{key, kind, agent?, task?, session?, children?, verdict_child?,
max_rounds?, depends_on?, locks?}`. A parameter is used only as a whole value
`{param: <name>}`; there is no string interpolation, expression or shell. Keys
are local ids; `session` letters are local aliases.
- Shipped in `defaults/node-templates/`: `implement` —
  `{{tester-A <-> reviewer-B} -> {implementer-C <-> reviewer-B}}`, params
  `spec_path, tests_task, implement_task, tester, reviewer, implementer,
  test_rounds (2), impl_rounds (3)`; and `review-loop` —
  `{worker-A <-> reviewer-B}`.
- Global/project files with the same name replace a shipped template whole.
  Project templates registered through `register_template` are stored in the
  host registry; files in the worktree or `.multiagents/` that an agent can
  write are never loaded.
- **Verified by:** a template with `"{param: x} suffix"` style interpolation
  is refused; a project file under `.multiagents/` is ignored. `[M4]`

**NC-R42 — instantiation.** `instantiate_template` validates the template, the
params and the fully expanded graph (NC-R6), then creates the nodes in one
write. The top-level node records the expanded definition, bindings, version
and sha256; a later template edit never changes it.
- **Verified by:** editing the registry after instantiation leaves the
  instance's recorded definition and behaviour unchanged. `[M4]`

## 12. Merge (NS-D2) and visibility (NS-R16)

**NC-R39 — `merge_node`.** Root only. `{id}` of a top-level node in `done`.
Uses today's merge path (squash by default, readonly revert, protections,
host programs off) from `nodes/<id>` into the base branch. Refused
(`not_approved`) when the outcome is not `approved`/`completed` unless
`force: true`. On success the branch and node worktrees are cleaned up as
`merge_agent` does.

**NC-R43 — monitor and doctor.** With the gate on, the monitor's state API
and page, and `doctor`, show: scheduler state (NC-R16 `status`), nodes by state
with derived blocked reasons, held nodes first, locks and holders, session
aliases and bindings, effective windows and next open/close, and starving
nodes. `doctor` reports a dead scheduler while the gate is on as a problem.
- **Verified by:** snapshot tests of the API payload and doctor lines. `[M6]`

**NC-R44 — orchestrator instructions.** The shipped `_orchestrator.md` gains a
section on planning with nodes (deposit ahead, templates, `wait_for_nodes` and
ack, loop_max decisions incl. "round 3 → opus" as an orchestrator choice,
`merge_node`); the live copy is patched as PN-R6a was. `[M6]`

## 13. Acceptance (NS-R17) `[M6]`

**NC-R45 — baseline.** Deposit three dependent simple nodes; disconnect the
orchestrator; fill PC slots, then release; restart the scheduler; each node
launches exactly once, with inputs bound to its predecessor's generation; all
transitions are delivered after reconnect.

**NC-R46 — the user's example.** `implement` template with fake agents: tests
rejected once then approved; implementation rejected until `impl_rounds`;
`loop_max` notified; root relaunches with another model and a raised maximum;
approved; reviewer-B used one provider session across both loops (the model
change applies to implementer-C, not the alias); `done` notified; `merge_node`
merges into main.

**NC-R47 — adversarial.** Forged token; supplied parent outside scope; run
creating a node outside its subtree; verdict from the wrong child, about the
wrong generation, twice; rejected generation as input; crash between claimed
and recorded (no double launch); lock released before confirmed termination;
window closing mid-loop.

**NC-R48 — no regression (NS-R19).** Gate off: full suite = known reds only.
