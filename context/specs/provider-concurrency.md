# Per-provider concurrency limit — the contract

**Status:** contract, written by the orchestrator on 2026-10-01 at the
user's request: "Il faudra un limiteur (desactive par default) de requetes
simultanees par fournisseur."
- **Ids:** `PC-R*`, never renumbered. A behaviour is retired by marking it
  withdrawn.
- **Related:**
  - `limits.max_concurrent` is the existing tree-wide limit. It is unchanged
    and still applies on top.
  - `context/specs/spend-caps.md` (SC) uses the same admission points and
    the same deferral machinery. Where both refuse, both causes are
    reported.

## Interpretation (orchestrator's decision)

A "simultaneous request" is a **running agent process on that provider**.
That covers:
- `start_agent` runs;
- steered or resumed runs while their turn executes;
- `consult` turns (the advisor and dev-advisor) while they run;
- nested agents started by other agents.

Agents parked `awaiting_user`, `idle` conversational agents between turns,
finished runs and deferred tasks hold no slot. Requests *inside* one agent
process (the CLI's own parallel tool calls) are not counted, because
multiagents cannot see or gate them.

## Behaviours

**PC-R1: configuration, off by default.**
- **Where it lives.** Under a provider, in `.multiagents/config/providers.yaml`:

  ```yaml
  opencode-deepinfra:
    max_concurrent: 2     # absent or null: no limit (the default)
  ```

- **Defaults.** No shipped provider sets it. With no `max_concurrent` on any
  provider, behaviour is exactly as today.
- **Valid values.** An integer ≥ 1, or null. Anything else is a config error
  at load time, naming the key:
  - 0 or a negative number;
  - a float, a bool or a string.
- **Not inherited through `extends`.** The limit belongs to the instance
  that declares it. `claude` and a provider that extends it have separate
  limits.
- **Re-read without restart.** A changed value takes effect at the next
  admission decision.
- **Verified by:**
  - the validation matrix;
  - no shipped provider has the key;
  - the `extends` rule.

**PC-R2: counting, across the whole project.**
- **What is counted.** A provider's count is its runs holding a slot, as
  defined above, across every process of this project: the root MCP server,
  nested servers and the driver.
- **Where the slot is counted.** A slot belongs to the provider the run
  actually launched on. A run that falls back to another provider counts
  against that other provider.
- **Atomicity.** Two admissions that race for the last slot never both
  succeed: the check and the reservation form one atomic step across
  processes.
- **Release.** A slot is released when the process exits, whatever the
  reason: done, failed, stopped, cancelled, limited, crashed or killed.
- **Crash recovery.** A slot held by a process that died without releasing
  it is reclaimed. It is not leaked for longer than the existing liveness
  checks take to notice the death.
- **Verified by:**
  - two concurrent admissions for one free slot, with exactly one admitted;
  - a run killed with SIGKILL frees its slot;
  - runs in a nested server count against the root's view.

**PC-R3: admission when the provider is full.**
- **When it applies.** On every start, steer, resume, consult turn,
  automatic retry, wrap-up turn and deferred restart that would launch on a
  full provider.
- **A fresh start or a deferred restart.** The next provider in the agent's
  route that has a free slot (and passes the existing checks) is used,
  exactly like a fallback for an exhausted provider. When none has one, the
  task is **queued**:
  - deferred with the cause `provider_concurrency`;
  - restarted automatically as soon as a slot frees, at the next
    `wait_for_agents` or slot release;
  - in FIFO order per provider.
- **A steer, resume or consult turn.** These need their own session and
  provider, so they never move to a fallback.
  - A steer or resume is queued like a fresh start, but on its own provider
    only.
  - A `consult`, which blocks the caller, waits for a slot up to its own
    timeout and then fails with a clear error naming the provider, its
    limit and the slot holders.
- **No timer-only wait.** A queued task never polls for its slot: a release
  wakes it.
- **Wall clock.** Queue time counts against no watchdog of the queued run.
  The run's wall clock starts at launch.
- **Verified by:**
  - with `max_concurrent: 1`, a second start goes to the fallback when
    there is one, and otherwise is queued with `provider_concurrency`;
  - the queued start launches once the first run finishes;
  - FIFO order with three queued starts;
  - a steer on a full provider is queued and never moved;
  - a consult on a full provider times out with the error described;
  - with no limit configured, nothing is queued.

**PC-R4: visibility.**
- **`budget_status`.** For each limited provider it shows the limit, the
  slots in use with their agent ids, and the queue length.
- **`wait_for_agents`.** Its `capacity` names the providers that are full.
- **Events.** A `provider_concurrency` event is recorded when a task is
  queued and when it is released from the queue.
- **Verified by:** the output fields under a full provider, and the events.

**PC-R5: no regression.**
- With no limit configured, the behaviour is exactly as today, including
  under `limits.max_concurrent`.
- **Verified by:** the full suite. The only reds are the known ones.

## Out of scope

- Limiting requests inside a single agent process.
- Rate limits per minute, as opposed to concurrency.
- Per-model concurrency limits. This can be added later as a `models:` map,
  as SC does.

## Amendments after the advisor's code check (2026-10-01, before tests)

These override any earlier wording they contradict.

**PC-R2a: reservation and release.**
- **Reservation.** A provider slot and the tree-wide `limits.max_concurrent`
  slot are reserved in **one** atomic, cross-process transaction, using
  the same mechanism as today's tree admission. Queued work holds neither.
- **Release.** A slot is released only when the agent process is
  **confirmed** to have exited. A server or Runner dying does not release
  its agents' slots: detached agents and cleanup holds keep their slots
  until confirmed dead. Adoption transfers a slot to the adopter, never
  adds one.
- **Lowering a limit.** New admissions are blocked until the count is under
  the limit. Running agents are never stopped for it.
- **Raising or removing a limit.** The queue is woken.
- **Isolation.** One provider being full never pauses or delays admission
  to another provider.

**PC-R3a: the queue.**
- **Identity.** Each queued entry is durable and carries:
  - its operation (start, steer/resume, retry, wrap-up);
  - the node and session ids;
  - the provider and the model;
  - the message;
  - every runtime pin.
  A resume entry is dispatched through the steer/resume path, never as a
  fresh start, and keeps its pins.
- **FIFO.** Each entry gets a durable, increasing sequence number per
  provider. A new arrival never takes a slot on a provider that has
  eligible queued entries ahead of it.
- **Where a fresh start queues.** It tries each provider in its route, in
  order, and takes the first free slot whose queue is empty. When there is
  none, it queues on the **first provider of its route** that passes the
  other checks.
- **A blocked head.** A head refused for another reason (SC cap, auth,
  cooldown) is reported and skipped, without losing its place, until that
  reason clears. It never blocks the entries behind it.
- **Draining.** The queue is drained:
  - by the process that releases a slot, at release;
  - at every `wait_for_agents`;
  - by the existing liveness reconciliation, which also recovers slots from
    unannounced deaths.
  "A queued task never polls" means no per-task timer. Reconciliation is
  allowed and required.
- **Claiming.** Claiming a queue entry and reserving its slot form one
  atomic step.

**PC-R3b: steer, and consult.**
- **Steering a live run.** The run **keeps its own slot** across the
  handoff. The predecessor is stopped only after the replacement is secured,
  and is never sent through the queue. With a limit of 1, a run never waits
  for its own slot.
- **Resuming a run that holds no slot.** It is queued on its own provider,
  as PC-R3 says.
- **One consult deadline.** A single deadline covers the conversation-lock
  wait, the slot wait and the execution. When it runs out:
  - the waiter is removed from the queue and any reservation released;
  - the error names the provider, its limit and the slot holders;
  - when the caller itself holds a slot on that provider, the error says
    so explicitly. This self-dependency is the likely cause of a deadlock
    with a limit of 1.
  Consultations are **not** exempt from the count.

**Implementation order (orchestrator's decision).** PC is built after CW
merges, by the same implementer who will later build SC, in a separate
reviewed pass. PC's shared admission, reservation and typed queue come
first; SC's ledger and enforcement build on them.
