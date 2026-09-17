# Phase 1 contract — the resume path

The interface contract for `BRIEF.md` phase 1 items 4, 5 and 6 — tickets
`bug-ad011c`, `bug-97a0c7` and `bug-8195f2`. Written by the orchestrator.

**These are three tickets and one function.** All three are defects in `steer()`
and the path it takes to respawn a run. They are contracted together because
splitting them would mean three agents touching the same twenty lines in
sequence, each merging before the next can start, and because two of the three
share a root cause worth naming once.

Read all three with `list_tickets`. Each carries the root cause with line
numbers, and between them they name the correct pattern to copy.

---

## The shape they share

`steer()` rebuilds a run's invocation from the agent's **static configuration**
rather than from what the run is **actually doing**. The static config knows
what the agent was configured to be; the live record knows what routing,
fallback and the passage of time made of it. Where those disagree, the resume is
built from the wrong one.

The codebase already does this correctly elsewhere. The silent-failure retry
path at `runner.py:1458` uses `run.spec` and `run.provider` — the mutated spec
created at spawn time, after routing. That is the pattern to follow, and the
ticket for R7 names it.

---

## R7 — a resumed run keeps the provider and model it was actually using

**Ticket:** `bug-ad011c`.

**Observable behaviour required.** Steering a run resumes it with the provider,
model and options the run is executing under.

Today it does not. `steer()` reads the provider from the live node
(`runner.py:2034`) and the spec from the static config (`runner.py:2033`), then
hands both to `_launch`. For a run that was routed to a fallback, those disagree,
and `_launch` builds an argv naming the *preferred* provider's model. Observed
verbatim:

```
error: invalid model selection (--model "opencode-go/qwen3.7-plus" --effort "high"):
--effort is not supported for model "opencode-go/qwen3.7-plus"
```

The run was executing on `agy` as `gemini-3.8-flash-high`. It died in 94 seconds
having produced nothing.

Note the second half of that message: the `effort` option travelled too, from
`options = {"effort": spec.effort, **spec.extra}` at `runner.py:825`, onto a
model that does not accept it. A model taken from one source and a flag from
another, with nothing checking the pair.

**What must be true afterwards.**

- A run routed to a fallback provider, then steered, resumes on that fallback
  with its model — not the configured preference.
- Options that belong to a provider namespace do not travel onto a model from a
  different one.
- A run that was **not** routed — where preference and actual agree — behaves
  exactly as it does today.

**Worth checking while you are there, and reporting either way:** the R7 ticket
observes that `consult()` at `runner.py:2120` appears to share this pattern. If
it does, say so; whether it is fixed here or scheduled separately is my call, not
a decision to take quietly inside this change.

---

## R8 — a truncated run can be resumed

**Ticket:** `bug-97a0c7`.

**Observable behaviour required.** `steer_agent` works on a run whose status is
`truncated`.

Today it fails before the model is reached:

```
OCI runtime exec failed: exec failed: Cwd must be an absolute path
```

Reproduced on two independent agent ids. `collect_agent` reports `branch: null`
for these runs, and `steer_agent` succeeds normally against a run that is still
going — so the fault is specific to resuming a truncated run, not to steering.

**Why this one is worse than it looks.** The `truncated` status string itself
tells the caller to do this:

> `RESUMABLE: steer_agent('ag-XXXXXX', ...) continues this session on its branch,
> which is far cheaper than reissuing the task.`

So the tool recommends a recovery that cannot work, and four runs during the
review spent roughly 400,000 tokens between them with nothing recoverable.

**What must be true afterwards.**

- Steering a truncated run resumes it rather than failing on argument
  construction.
- When a working directory genuinely cannot be resolved, the failure says so —
  `steer_agent` currently reports `"steered": false` alongside text claiming
  *"the steer was delivered"*, which contradicts itself and costs the caller a
  turn to disbelieve.

**Out of scope.** That ticket also describes a provider whose per-turn time limit
fires before the agent's configured timeout, so runs are cut off having written
nothing. That is real and it is not a defect in the resume path — it is a
reconciliation between two timeouts, with its own contract. Do not fold it in.

---

## R9 — a run being resumed is not reported as cancelled

**Ticket:** `bug-8195f2`.

**Observable behaviour required.** While a run is between its interruption and
its respawn, a concurrent reader does not see it as terminally ended.

Today `steer()` calls `stop()` (`runner.py:2032`), which is the same function
`stop_agent` uses for a genuine cancellation and which unconditionally writes
`cancelled` / `"stopped by parent"` (`runner.py:2011`). `steer()` overwrites that
back to `running` / `steered` only after `_launch` has respawned the process
(`runner.py:2044`). Between the two writes there is a real window, and
`wait_for_any` polls once per second and treats any status outside
`{pending, running}` as terminal — permanently, for that call.

This was observed **five times** during the review. Each time the reported status
was `cancelled: "stopped by parent"` for an agent that was alive, and the parent
had called nothing. The reason string is independently false: the parent stopped
nothing; the quota guard did.

**What must be true afterwards.**

- A reader polling during a steer does not receive a terminal status for that
  run.
- `stop_agent`'s own path is unchanged: a genuine parent-initiated cancellation
  still reports `cancelled` with a reason naming the parent.
- The two cases are distinguishable from outside, because acting on the wrong one
  costs a branch.

**The ticket proposes an approach** — an `internal=True` parameter on `stop()`
that skips the terminal write, with `steer()` passing it. It is a reasonable
shape and you may take it, but it is a proposal rather than a decision: if you
see a better one, take that and say why.

---

## What is NOT in scope

- **Do not change what `stop_agent` does.** Its behaviour is correct and other
  things depend on it.
- **Do not change the `truncated` reason string's advice** by deleting it. Once
  R8 lands the advice is true; removing it would be fixing the symptom.
- **Do not reconcile provider turn limits with agent timeouts.** Named above as
  out of scope for R8 and meant separately.
- **Do not touch `tests/test_core.py`.** It is read-only to the implementer
  tiers and the merge gate reverts it. A test that needs changing is
  `NEED_INFO(<test name>)` back to the test engineer.

---

## Why the existing tests missed all three

Worth knowing before writing new ones, because the gap is the same each time:
every existing steer test constructs a node whose provider and model **match**
the agent spec exactly, and none drives a concurrent reader.

- `test_a_steer_says_whether_anything_answered`,
  `test_steer_does_not_report_running_against_a_dead_run`,
  `test_steer_confirms_on_the_first_event_not_a_fixed_sleep` — all assert on
  `steer()`'s own return value, never on what another caller sees.
- `test_cancellation_reasons_are_distinguished` (`tests/test_core.py:1695`)
  covers `stop_requested` versus server shutdown inside `stop()`'s own handler,
  which is a different distinction from the one R9 needs.
- Nothing constructs a run whose actual provider differs from its configured
  preference, which is the entire content of R7.

---

## What "done" looks like

The confirmed baseline is **960 passed, 2 failed**. The two are
`tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated`
(finding F100, phase 1 item 8) and
`tests/test_core.py::test_the_claude_script_uses_the_container_profile_only_where_it_should`
(fails on a host with `docker` present, passes in a container — recorded in
`docs/open-questions.md`). Inside an agent, 17 runner tests fail with
`PermissionError: can_spawn is false` for reasons also recorded there.

Run the suite as `uv run --frozen python -m pytest`.

R7, R8 and R9 are done when their tests are green and that baseline is otherwise
unchanged.

---

# Amendment — 2026-09-17, after the first tester run

The first tester wrote and committed R7 and ran out of budget before R8 and R9.
Its findings are folded in here rather than left in a run summary. **R7's two
tests are merged and verified**: `test_r7_a_steered_fallback_run_keeps_the_model_it_is_actually_running`
fails on `- gemini-3.8-flash-high / + opencode-go/qwen3.7-plus`, and the
regression guard `test_r7_an_unrouted_run_resumes_exactly_as_it_does_today`
passes, as the contract requires.

## R8's root cause is not what the ticket guessed

The ticket hypothesised that the resume path derives its working directory from a
null branch field. The real mechanism is narrower and more interesting.

`_drop_if_empty` (`runner.py:1619-1635`) removes the worktree of a
`writes: false` agent whose branch has no commits, and writes `branch=""` and
`worktree=""` onto the node. `steer()` then does `workdir=Path(node.worktree)` —
`Path("")` is `.` — and `docker exec` rejects a relative `Cwd`.

That explains the observed population exactly: the four runs this cost during the
review were all read-only agents that produced nothing, which is precisely the
set `_drop_if_empty` acts on.

## A trap in R8's test, which would otherwise pass a fake fix

**Under the local executor a relative cwd silently resolves** to wherever the
server happens to be. So `is_absolute()` alone goes red locally, and a lazy fix —
`Path(node.worktree).resolve()` — turns `.` into the server's cwd and makes it
green while fixing nothing.

The test must also assert `Path(cwd).resolve() != Path.cwd().resolve()`.

And it must **not** assert the cwd is under the project root: `ProjectPaths.worktree()`
lives under `state_root()`, which is outside it.

## R9 is deterministic, and not through `wait_for_any`

A reader task looping `node = r.tree.get(id); await asyncio.sleep(0)` is
scheduled at every suspension point of `steer()`, and `_launch` →
`executor.start` → `create_subprocess_exec` is guaranteed to yield inside the
window between `stop()`'s write (`runner.py:2011`) and `steer()`'s corrective
write (`runner.py:2044`). The reader cannot miss the window; no timing luck is
involved.

`wait_for_any`'s own one-second sleep is what makes the realistic version flaky.
The tight reader is strictly stronger: if no terminal status is ever written,
no reader can see one.

Assert that no status outside `{pending, running}` appears in the recorded
sequence.

## Answer to `NEED_INFO(steer error wording)`

The tester asked whether a result carrying `"steered": false` may ever also claim
*"the steer was delivered"*, or whether that wording is wrong on every failure
path rather than only the unresolvable-cwd one.

**It is wrong on every failure path**, and R8's second bullet is hereby widened to
say so. `"steered": false` alongside "the steer was delivered" is
self-contradictory whatever caused it, and the caller cannot act on a result that
asserts both.

The tester was also right that the narrow version was untestable: once the
implementer adds any working fallback, no input from the public surface makes
resolution genuinely fail, so a test scoped to that one cause is unreachable by
construction. The widened version is testable through any failure path at all.

**Revised R8 second bullet:** a result reporting `"steered": false` does not
contain text claiming the steer was delivered.

## Decision on `consult()`

The R7 ticket flagged `consult()` (`runner.py:2120`) as possibly sharing R7's
pattern, and the contract reserved the decision. The tester confirmed it does:
`consult()` reads `self.config.agent(agent_name)` and never consults the live
node.

**It is in scope for R7.** It is the same defect, in the same shape, one function
away. Splitting it means a second contract, a second tester and a second
implementer for what is one line of reasoning, and leaving it means the next
fallback-routed consult dies exactly as the steer did.

R7's tests do not currently cover it. Whoever writes R8 and R9 should add the
`consult()` case alongside them.

## Read this before re-deriving anything

The first tester spent most of its budget establishing where a resume is
observable from outside, and that is worth inheriting rather than repeating:

- The argv is the only place a resumed run's provider, model, options **and**
  working directory are all observable together, which is what keeps these tests
  black box.
- `command.json` has no consumers and is therefore not a contract surface.
- Its probe helper writes to `<probe>.part` and moves it into place, so the file
  existing means the whole argv is in it.
- `_steer_recording(r, id, msg, probe)` starts `steer()`, waits for the probe
  file, then appends an event so the confirmation loop ends early instead of
  sitting out `STEER_CONFIRM_SECONDS`.

Both helpers are already merged in `tests/test_core.py` beside the R7 tests.
