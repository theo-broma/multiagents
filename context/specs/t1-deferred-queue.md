# T1: the deferred queue never loses a task silently, the contract

**Status:** contract, written by the orchestrator on 2026-09-30.
- **Source:** ticket "a deferred task can be removed from the durable deferred queue with no event and no report", kept in context/tickets/2026-09-30-unfiled.md.
- **What happened:** on 2026-09-30, the deferred task df-e34d37 left `tree.json` `deferred` during a `wait_for_agents`. No `created`, `deferred`, `dropped` or error event was recorded, and the result said only "no active agents".
- **Scope:** the drain (`resume_deferred`), the `wait_for_agents` result, and two new MCP tools.
- **Ids:** `DQ-R*`. They are never renumbered.

## Behaviours

**DQ-R1: every exit from the queue is an event.**
- **When.** Each time an entry leaves the `deferred` list, one event is appended to `events.jsonl`.
- **Fields.** The event has `kind: "deferred_exit"`, `deferred_id`, `agent` and `outcome`, where `outcome` is one of:
  - `restarted`, with the new `agent_id`;
  - `re_deferred`, with the new deferred id;
  - `refused`, with `reason`;
  - `dropped`, with `reason`;
  - `cancelled`.
- **No silent exit.** No code path removes an entry without writing this event.
- Verified by: each outcome, driven through the drain or `cancel_deferred`, writes exactly one matching event, and the entry is gone from the queue afterwards (except under DQ-R3).

**DQ-R2: the `wait_for_agents` result reports the drain.**
- **The field.** When the drain did anything, the result carries `deferred: {"restarted": [...], "refused": [...], "dropped": [...], "still_deferred": n}`. It is always present in that case, **including** on the "no active agents" return and on a timeout.
- **A restarted run.** A run started by the drain is an active agent from then on. It appears in `still_running`, or in `changed` if it finishes, like any other run.
- Verified by:
  - a due entry drained while no agent is active, where the result lists the restart and the new agent id;
  - a due entry whose start is refused, where the result lists it under `refused` with the reason.

**DQ-R3: a refused restart stays visible.**
- **What counts as refused.** `start()` returns an error, or raises something that is not a quota or transient condition. Examples:
  - the pinned model is no longer configured for any provider of that agent;
  - the agent is no longer in the roster;
  - a budget tag is spent.
- **What happens to the entry.** It stays in the queue with `status: "refused"` and the `reason`. Two things follow:
  - it is never retried automatically, and it no longer holds the pause (DQ-R6);
  - it leaves the queue only through `cancel_deferred`.
- **Transient errors.** Behaviour is unchanged: the entry stays queued with `status: "waiting"`, and the drain stops.
- Verified by: a deferred entry pinned to a model later removed from `agents.yaml`. After the drain, `list_deferred` shows it as refused, with a reason naming the agent and the model.

**DQ-R4: the MCP tool `list_deferred()`.**
- **What it returns.** Every queued entry, each with:
  - `id` and `agent`;
  - `task`, the first 200 characters;
  - `model`, or empty when there is none;
  - `retry_after`, as ISO 8601 UTC;
  - `status`, which is `waiting` or `refused`;
  - `reason`;
  - `deferred_by`, the caller's agent id, or `orchestrator`.
- **Read-only.**
- Verified by: after two deferrals and one refusal, the tool lists the three entries with the right statuses.

**DQ-R5: the MCP tool `cancel_deferred(deferred_id)`.**
- **What it does.** It removes the entry and writes a `deferred_exit` event with `outcome: "cancelled"`.
- **Unknown ids.** An unknown id returns `{"error": ...}` and changes nothing.
- **Authorisation.** The caller must be the one that deferred the entry, or one of its ancestors. The orchestrator may cancel any entry. This is the same rule as `stop_agent`'s `_may_act_on`.
- Verified by:
  - cancelling removes the entry and writes the event;
  - an unknown id errors;
  - a sibling's attempt to cancel is refused.

**DQ-R6: the pause follows the waiting entries.**
- **When it holds.** The tree-wide pause set by a deferral is held only while at least one entry is `waiting`.
- **When it lifts.** When the last waiting entry leaves, whether it was restarted, refused, dropped or cancelled, the pause is lifted in the same step.
- Verified by: one deferral followed by a cancel leaves `pause` empty, and `start_agent` works again at once.

**DQ-R7: nothing else changes.**
- A deferral still returns `deferred: true` with `retry_after`, and a due entry still restarts at the next `wait_for_agents`.
- The existing suite stays green, apart from the known reds.

## Amendments of 2026-09-30, after the test suite (ag-b7bb12)

The tester's assumptions 1, 2 and 4 are accepted:
- `list_deferred` returns either a bare list or a one-list dict;
- the new deferred id may be any field of a `re_deferred` event;
- a `restarted` item carries the new agent id.

**DQ-R3a: an agent removed from the roster is `dropped`, not `refused`.**
- **What happens.** The entry leaves the queue, and the `deferred_exit` event records `outcome: dropped` with a reason. The `wait_for_agents` result lists it under `deferred.dropped`, which keeps `test_resume_deferred_reports_tasks_whose_agent_is_gone`.
- **What `refused` now covers.** A pinned model that is no longer configured, a spent budget tag, and any other refusal returned as a value.

**DQ-R3b: which raised errors are refusals.**
- **Refusals:** a `ValueError` or `PermissionError` raised by `start()`, and any error dict returned by `start()`. These count as `refused`.
- **Transient:** any other exception, for example `RuntimeError`, `OSError` or `TimeoutError`. The entry stays `waiting`, and the drain stops, as it does today.

**DQ-R6a: pauses of other origins.** DQ-R6 concerns only the pause set by a deferral. A pause set another way (auth, provider_down, …) is unchanged by this contract.

## Amendments of 2026-09-30, after the advisor (ag-322b14)

**DQ-R8: claim before restart, so two drains never restart the same entry.**
- **The claim.** In one tree transaction, the drain moves a due entry from `waiting` to `status: "restarting"` and records a claim with the drain's pid and time. A concurrent drain skips any entry that is not `waiting`.
- **After the attempt.** The entry leaves the queue only in a later transaction, once its outcome is known. The started node records `deferred_id`.
- **Where the truth lives.** `tree.json` is authoritative; the event is appended after the tree commit.
- **Recovery.** A `restarting` entry whose claimer pid is dead is resolved at the next drain:
  - when a node carrying its `deferred_id` exists, it counts as `restarted` and the missing event is written then;
  - when there is no such node, the entry returns to `waiting`.
- Verified by:
  - two drains run concurrently on one due entry start exactly one run;
  - an entry left `restarting` by a dead pid is resolved as described.

**DQ-R4a: `deferred_by` comes from the trusted caller.** `deferred_by` is recorded when the entry is created, from the caller's identity, never inferred later from `spec.agent`. Entries created before this change have no `deferred_by`, and only the orchestrator may cancel them.

**DQ-R2a: refused entries stay visible.** The `wait_for_agents` result carries `deferred.refused_total`, the count of refused entries still queued, whenever it is non-zero, even when that drain did nothing else. Refused entries never expire by themselves.

**DQ-R3c: an error returned by start() leaves nothing behind.** When `start()` returns an error dict, it must have created no node and no worktree. If it did, the node is recorded in the refused entry's `node_id`, so that it can be found.

**DQ-R8a: the shape of the claim, fixed after the tests (ag-7cfc05).**
- **Where it lives.** The claim is stored in the entry as `claim: {"pid": <int>, "at": <epoch float>}`.
- **The pause.** A `restarting` entry holds the pause, as a `waiting` one does.
- **A re-deferral.** A re-deferred entry keeps its original `deferred_by`.

## Amendments of 2026-09-30, after the adversary (ag-5917f6)

**DQ-R9: a malformed entry is tolerated.**
- **What counts.** A malformed queue entry is any of these: not a dict, missing `id` or `retry_after`, a non-numeric `retry_after`, or a claim that is not a dict with an int `pid`.
- **What happens to it.** It is skipped by the drain and shown by `list_deferred` with `status: "malformed"`.
- **No crash.** It never makes `wait_for_agents`, `list_deferred` or `start_agent` raise.
- **Removal.** Only `cancel_deferred`, by the orchestrator, removes it.

**DQ-R10: a claim is released on every exit path.** This includes cancellation, i.e. `BaseException`/`CancelledError`. A cancelled drain returns the entry to `waiting`, and then re-raises.

**DQ-R11: the queue and the node agree after a crash.**
- **Where `deferred_id` is written.** The node's `deferred_id` is written in the same transaction that creates the node. A crash between `start()` returning and the drain's bookkeeping can then never lead to a second start.
- **Re-deferral.** A re-deferral names its own new entry explicitly, never by diffing the queue.
- **Owner.** A re-deferral keeps the original owner, including the orchestrator (`None`), and never takes the drainer's identity.

**DQ-R12: cancel versus restart.**
- **Refusal.** `cancel_deferred` on an entry in `restarting` is refused ("restart in progress"). The caller may `stop_agent` the resulting run once it exists.
- **Recovery.** Requeueing a dead claim is done inside one transaction that re-checks the entry is still `restarting` with the same claim.
