# H4 and H14: no empty-model route, and project limits that actually apply

**Status:** contract, written by the orchestrator on 2026-09-29. It covers
H4 and H14 in `phase6-hardening.md`.
- Ids are `RT-R*` (H4) and `LM-R*` (H14). They are never renumbered. A
  behaviour is retired by marking it withdrawn.
- Research: ag-7b7ee5.

## H4: an empty fallback model is never a valid route

What happens today:
- CX-C28's consult path already treats an empty fallback as "no route"
  (`_conversation_route`, `runner.py` ~3375).
- Two paths still launch with `model=""`:
  - **steer:** `_spec_of()` (`runner.py` ~2826, called at ~3137) replaces
    the model with `spec.fallback_for(node.provider)`, which is `""` when
    the agent's `models:` has no entry for that provider;
  - **start:** budget or family routing picks a same-family sibling
    provider and does the same (`runner.py` ~1515, ~1581).

**RT-R1: start never routes to a provider without a model.** Budget and
family routing in `start()` considers only providers for which the agent
has a **non-empty** model. That is its primary model, or a non-empty
`models:` entry for the provider.
- A sibling provider without one is excluded from the candidate set before
  routing chooses.
- When no candidate remains, the existing exhaustion behaviour applies
  unchanged: defer, or the current refusal. It is never a launch with an
  empty model.
- When an otherwise-eligible provider is excluded for this reason, one
  `route_skipped` event is emitted per `start`, carrying `provider` and
  `reason: "no model configured for this agent on <provider>"`.
- Verified by:
  - an agent whose primary provider is exhausted, and whose only same-family
    sibling has no `models:` entry, is never launched with an empty
    `--model` or equivalent. It is deferred or refused as today, and the
    event is emitted.
  - with a non-empty sibling entry, it routes to that sibling as today.

**RT-R2: steer never resumes on a provider without a model.** When
`steer_agent` rebuilds a run on the node's recorded provider and the agent
has no non-empty model for it:
- the steer is **refused**. Nothing is launched, and the node's status,
  session and worktree are unchanged;
- the error names the agent, the provider, and the missing
  `models.<provider>` entry;
- the steer is not silently resumed on a different provider, because that
  would lose the session, which is the point of steering.
- Verified by: a node recorded on provider P, whose agent config no longer
  has a model for P, is steered. The result is an error naming P and the
  missing key, no process is started, and the node is untouched.

**RT-R3: one check for all three paths.** The consult (CX-C28), start and
steer paths reach the "no model for this provider" conclusion the same way.
- A change to what counts as a usable model therefore cannot fix one path
  and miss another.
- CX-C28's observable consult behaviour is unchanged: `conversation_replaced`
  and the `[system]` reply prefix.
- Verified by: the existing CX-C28 tests stay green, and RT-R1 and RT-R2's
  tests pass.

## H14: built-in agent defaults must not bypass project `limits:`

What happens today:
- `AgentSpec` defaults are `timeout=900`, `max_children=2` and
  `silence_timeout=180` (`config.py` ~346).
- The runner prefers them over `limits.default_timeout`,
  `limits.max_children` and `limits.silence_timeout`, as follows:
  - `timeout`: `limits.default_timeout` is never read (`runner.py` ~1148,
    ~1184);
  - `max_children`: `spec.max_children or limits.max_children` is always
    the truthy 2 (`runner.py` ~804);
  - `silence_timeout`: goes straight to the supervisor (`runner.py` ~1070).
- `max_steps` already falls through correctly, and is the model to follow.

**LM-R1: precedence.** For each of `timeout`, `max_children` and
`silence_timeout`, the effective value is the first of:
1. an explicit per-call value, such as `start_agent(timeout=N)` with N > 0.
   This applies to `timeout` only;
2. the agent's own value, **when the agent config sets it**;
3. the project's `limits:` value: `default_timeout`, `max_children` or
   `silence_timeout`;
4. the built-in default. Today that is 900, 2 and 180.

An agent that omits the field must be distinguishable from one that sets
it to the built-in value. Setting `timeout: 900` explicitly still wins over
`limits.default_timeout: 1200`.

Verified by, for each of the three keys:
- a project with `limits.<key>` set, and an agent omitting the field, gets
  the project value;
- an agent setting the field gets its own value;
- neither set gives the built-in value;
- for `timeout`, an explicit `start_agent(timeout=…)` beats all three.

The observables are the wall-clock timeout the supervisor enforces, the
child-count refusal, and the silence watchdog. Short values keep the tests
fast.

**LM-R2: the effective value and its source are reportable.** For every
started agent, the runner can report each of the three effective values
together with its source, one of `call`, `agent`, `project` or `builtin`.
- D1 (limit notices) depends on this to say which limit was hit and where
  it was set.
- Minimum surface: the `start_agent` result carries `effective_limits`,
  shaped as `{timeout: {value, source}, max_children: {value, source},
  silence_timeout: {value, source}}`.
- Verified by: the `start_agent` result for each LM-R1 case shows the
  expected value and source.

**LM-R3: nothing else regresses.**
- `max_steps` resolution is unchanged.
- The live `project.yaml` sets `default_timeout: 900`, so this project's
  runs behave as before.
- Per-agent values already in `agents.yaml` keep winning.
- The existing suite stays green, apart from the known reds (the 72
  phase2 tests, and any tests for items not yet implemented).
- Verified by: the existing suite.
