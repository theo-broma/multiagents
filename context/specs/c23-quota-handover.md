# C23 — Quota handover

Status: validated by the user 2026-10-04.
Source: user request 2026-10-04 (BRIEF.md C23) and the user's rules given the same day; research ag-0600c9; advisor ag-aed397, turn 9.

## Purpose

An instance whose quota is exhausted hands its work to the next available one. The policy is generic: it names no provider. Any provider can be the orchestrator's.

- A **running** agent on an exhausted instance moves on to the next available instance:
  - first another instance of its own family, which keeps its conversation;
  - then the other providers in its own list.
- The instance the orchestrator and initializer run on is protected for them: reserved entirely while other instances are available, and to a 25 % floor otherwise.
- That floor is opened to agents only as a last resort, while the orchestrator is idle for lack of work, one task at a time, under the orchestrator's veto.

## Terms

- **Instance:** a provider entry in `providers` (for example a base provider and its `-b` sibling).
- **Family:** `Provider.family`, as it exists today (`providers.py`, C22).
- **Sibling:** another instance of the same family.
- **Agent list:** the instances an agent may run on. These are its `provider` followed by its fallback and `models:` entries, in configured order.
- **Reserved instance:** the instance declared as the orchestrator's (QH-R2). The initializer runs on it too.
- **Floor:** the fraction `quota_handover.reserve_fraction` (default `0.25`) of the reserved instance's quota. It is measured as headroom on that instance's most constrained short window.
- **Usable:** an instance is usable when it is enabled, authenticated and admissible now. Admissible means it passes the concurrency, spend-cap and quota checks that a fresh launch would pass. Unknown quota counts as usable.
- **Quota stop:** a run attempt that ends on a provider-limit cause the runner already classifies: the `limited` outcome with a provider-limit cause, or the `quota` classification.
  - It is never a spend-cap stop, a budget-tag stop or an admission refusal.
  - Those keep their current behaviour.
- **Home:** the instance a run was launched on. It is recorded at launch and never changes.
- **Current:** the instance the run is on now.
- **Segment:** a maximal stretch of a run on one instance. A run has segments 1..n, and each switch opens a new one.
- **Orchestrator idle:** no agent is running in the tree, and all remaining work is deferred because of quota, or there is no remaining work (QH-R16).

## Configuration

- **QH-R1.** `quota_handover.enabled` in `project.yaml` (boolean, default `true`). With `false`, quota stops behave exactly as before C23 and no reservation applies. *Verified by:* the same quota-stop scenario run with each value.
- **QH-R2.** `quota_handover.reserved_instance` names the orchestrator's instance.
  - It must name an existing instance; anything else is refused when config loads, with an error.
  - When it is unset, nothing is reserved. QH-R6 and QH-R14 to R17 are then inert.
  - `quota_handover.reserve_fraction` is a float in (0, 1), default `0.25`.
  - *Verified by:* config-load tests.
- **QH-R3.** Each provider declares a `handover_mode` per execution context (`docker`, `local`). The values are:
  - `shared`: the sibling sees the same session store and only the credentials change;
  - `copy`: the session must be transferred into the sibling's store;
  - `none`: the conversation cannot be carried to a sibling.

  An undeclared mode counts as `none`. Shipped defaults are data, not policy, and come from research ag-0600c9:
  - claude: `shared` under docker, `none` locally;
  - codex: `copy`;
  - agy: `none`.

  *Verified by:* config tests, plus fixture-provider scenarios.
- **QH-R4.** An agent may set `handover: false` in `agents.yaml`. Its runs then never switch instance and a quota stop defers them as before C23. *Verified by:* a scenario test.

## Order of succession

- **QH-R5.** When a running agent hits a quota stop on its current instance C, the runner picks the next instance in this order. It takes the first tier that has a usable candidate:
  1. **Sibling of C, not reserved, with a mode other than `none`:** the session is resumed there (QH-R8).
  2. **The reserved instance, if it is a sibling of C, its mode is not `none`, and its headroom is above the floor:** the session is resumed there.
  3. **The other instances in the agent's list, in configured order:** a continuation run starts there (QH-R9). Reserved instances are skipped in this tier unless they are above the floor.
  4. **The floor of the reserved instance:** only under the conditions of QH-R14 to R17.
  5. **Otherwise:** the run defers as before C23.

  Within a tier, C22's existing instance strategy picks among candidates. Only instances that serve a model the agent is allowed to use on them count as candidates. Within tiers 1 and 2, the model must be exactly the run's model. *Verified by:* one fixture test per tier, plus one asserting that a better tier always wins.
- **QH-R6.** The same order governs **new** runs routed by C22. A new run may use the reserved instance only when no other instance in its agent list is usable, and only above the floor. Below the floor it follows QH-R14 to R17. This replaces today's per-project exclusion by pinning. *Verified by:* routing tests on C22's strategy.
- **QH-R7.** The orchestrator can request a switch explicitly with `steer_agent(agent_id, message, provider=<instance>)`.
  - The target must be in the agent's list and usable.
  - A sibling target is a session resume (QH-R8); any other target is a continuation run (QH-R9).
  - The reserved instance below its floor is refused.
  - A `provider` equal to the current instance is a plain steer.
  - Every refusal names the condition that failed.
  - *Verified by:* MCP tool tests.

## Switching

- **QH-R8. Session resume (siblings).** The run resumes the same session id on the target. Everything else carries over unchanged: branch, worktree and cwd, task, model, effort, limits, budget tag and readonly paths. The resume prompt is the one an interrupted-run resume already uses.
  - In `copy` mode, the transfer follows these rules:
    - only that conversation's session state is installed in the target's store; credentials, other sessions and unrelated history are never copied;
    - the source is left intact and installation is atomic;
    - if a session with that id already exists in the target's store and differs from the source, the transfer is refused;
    - after a failure, the run is still resumable where it was.
  - If the target rejects the session (unknown id, auth, or failed verification), the runner moves to the next tier of QH-R5. It never starts a fresh session silently under the old id.
  - *Verified by:* a fixture provider whose transcript on B contains the turns from A; a rejection fixture; and copy-store tests for concurrent reads, a divergent target and failure midway.
- **QH-R9. Continuation run (another family).** A new session starts on the target with the agent's model for that instance, on the same branch and worktree. Uncommitted changes are kept. Its prompt is the original task, followed by a continuation note. The note:
  - says the previous segment was cut by quota;
  - gives the path of the previous segment's run log (`.multiagents/runs/<id>/`);
  - gives the branch's commits since the run started;
  - says that the worktree may contain uncommitted work from that segment.

  The `agent_id` does not change; the switch is a new segment of the same run. *Verified by:* a fixture provider asserting the prompt contents, the worktree state and an unchanged agent_id.
- **QH-R10.** Each quota stop leads to at most one switch attempt per tier candidate. Attempts are identified by `(agent_id, segment, attempt)` and recorded durably *before* any transfer or launch.
  - After a server restart mid-switch, the attempt is reconciled rather than repeated.
  - Within one run, an instance that already gave a quota stop is not chosen again until its quota has reset. This prevents ping-pong.
  - *Verified by:* restart-injection tests and a test with two successive quota stops.

## Return home

- **QH-R11.** At each resume boundary of a run whose current instance is not home, the runner tries home first: on a steer, a continue, or the restart of a deferred run.
  - When home is a sibling of current, the session goes back with QH-R8 rules, in the opposite direction.
  - When home is in another family, the session goes back only if home's session can still be resumed. That is the case when home's mode is `shared` or `copy` and its stored session is unchanged. Otherwise the run stays where it is.
  - A live turn is never stopped to go home.
  - If going home fails, the run continues where it is and a `return_home_failed` event is recorded.
  - *Verified by:* fixture tests with home exhausted, then reset.
- **QH-R12.** A run that is on the reserved instance (tier 2 or 4) when it reaches a resume boundary leaves it if any better tier is usable. *Verified by:* a test that frees a tier-1 sibling between two steers.

## Protecting the reserved instance

- **QH-R13.** No agent run uses the reserved instance while another instance in its own list is usable. No agent run uses its floor except under QH-R14 to R17. The orchestrator's own session is not an agent run and is never limited by this spec. *Verified by:* routing and switch tests at headroom above and below the floor.
- **QH-R14.** Agents may use the floor only when all of the following hold:
  - the orchestrator is idle;
  - the run's agent list contains no usable instance other than the reserved one;
  - no other run is currently using the floor.
- **QH-R15.** **One task at a time.** At most one run is on the floor at any moment. Other candidates queue in FIFO order of their quota stop, or of their deferral for new runs. *Verified by:* a test with two candidates.
- **QH-R16.** **Veto.** Before a run is dispatched to the floor, the server records a `reserve_request` (agent, task, reason) and notifies the orchestrator.
  - The orchestrator answers with `veto_reserve(request_id, reason)` or `allow_reserve(request_id)`.
  - Without an answer within `quota_handover.veto_window_seconds` (default `120`), the dispatch proceeds.
  - A vetoed run defers as before C23 and is not proposed again until the next quota reset of any instance in its list.
  - `wait_for_agents` returns on a `reserve_request`, with the `awaiting_orchestrator` status, so an idle orchestrator is woken.
  - *Verified by:* MCP tool tests for allow, veto and timeout.
- **QH-R17.** A floor run that hits a quota stop defers. A floor run that reaches a resume boundary once another instance in its list is usable leaves the floor under QH-R11 and QH-R12. *Verified by:* fixture tests.

## Observability and accounting

- **QH-R18.** Every switch, attempt and outcome appends an event with these fields:
  - `kind` ∈ {`handover_started`, `handover_completed`, `handover_failed`, `return_home`, `return_home_failed`, `reserve_request`, `reserve_allowed`, `reserve_vetoed`};
  - `agent_id`, `session_id`, `from`, `to`, `tier`, `segment`, `attempt`, `reason`, `at`.

  *Verified by:* event assertions in every scenario.
- **QH-R19.** `check_agent`, `agent_tree` and `collect_agent` show `home_provider`, `current_provider` and the list of segments as `{provider, account, model, session_id, started_at, ended_at, end_reason}`. `account` is the vault account actually used where it is known. *Verified by:* tool output tests.
- **QH-R20.** Usage and spend are attributed per segment to the instance and account that served it. Earlier segments' spend is never relabelled. `budget_status` shows the reserved instance and its floor. *Verified by:* a two-segment fixture run.

## Scope

- **QH-R21.** C23 covers runs launched through `start_agent`/`steer_agent`, including subagents.
  - It does not cover nodes managed by the scheduler (Phase 7 part 1). NC-R56 freezes an alias's binding, so covering them requires an amendment to NC-R56 and launcher integration. That is follow-up work.
  - It does not switch the orchestrator's own session.
- **QH-R22.** With C23 off, or with no candidate in any tier, a quota stop behaves exactly as before C23. The existing quota and defer tests stay green unmodified. *Verified by:* the existing suite.
