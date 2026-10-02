# A steered turn is judged by its own exit — the contract

**Status:** contract, orchestrator, 2026-10-02.
- **Ticket:** bug-dc522a. It is fixed in-house and never submitted.
- **Ids:** `SR-R*`. They are never renumbered; a behaviour is retired by marking it withdrawn.
- **Order:** implementation starts after SF (`context/specs/sc-pc-followups.md`) has merged, because both touch the steer path in `runner.py`.

## The defect

A run was stuck on its wall clock and was then steered. The resumed turn was marked `failed, exited -15` within 3 s and had no events (ag-ba1672).

The sequence:
1. `steer` → `stop(internal=True)` sends SIGTERM to the old wrapper.
2. The old `agentwrap` writes `-15` to `run_dir/exit_status`.
3. That write lands after `_start_wrapped` has unlinked the file for the new turn.
4. The new turn's `FollowHandle` reads it and finalizes.

Separately, a steered turn reuses the original `run.spec`. A limit raised in config since the start, such as `timeout`, therefore never reaches it. A run stopped by its wall clock and then steered gets the same budget that just ran out.

## Behaviours

**SR-R1: an exit status belongs to the turn that wrote it.**
- An exit status written by the wrapper of an earlier turn of the same run must never be read as the outcome of a later turn. This holds whatever the timing.
- The mechanism is the implementer's choice: a per-turn file, a generation token, or draining the old wrapper before launch.
- **Verified by:** a test that has a previous turn's wrapper write `-15` after the new turn has launched. The new turn is not finalized by it, and its status reflects its own process.

**SR-R2: the old turn is settled before the new one launches.**
- `steer` on a running or stuck run does not launch the new turn until the old wrapper and agent process have exited, or a bounded grace has elapsed and they have been killed.
- If they cannot be confirmed dead, the steer is refused with a clear reason. It does not launch beside a live predecessor.
- **Verified by:** after `steer` returns, the predecessor's pid is not alive, or the steer was refused with that reason.

**SR-R3: a steered turn's limits come from the current config.**
- When a turn is launched by `steer`, `timeout`, `silence_timeout` and `max_steps` are re-resolved from the configuration in force at that moment.
  - The resolution follows the same layering as `start_agent` (agent → project → default).
  - The wall clock starts at the steer.
- Provider, model and options stay as the run was started; they are not re-resolved. A per-run timeout given explicitly to `start_agent` keeps priority over config.
- **Verified by:** a run started with agent `timeout: 900`, the config then changed to 2700, and the run steered. The new turn's effective timeout is 2700, measured from the steer. The steer result or the events report it, in the same `effective_limits` shape that `start_agent` returns.

**SR-R4: the reported duration is the turn's.**
- The "[no output] the run ended … after Ns" text and `elapsed_seconds` for a finished turn describe that turn, not the node's lifetime since its first start.
- The node's total age may be reported separately, under its own name.
- **Verified by:** a steered turn that fails immediately reports an elapsed time of seconds, not the node's age.

**SR-R5: no regression.**
- Steer on a `done` or `failed` run, steer for a NEED_INFO answer, and an unconfirmed-death refusal (SF) all behave as today, apart from SR-R3's limits.
- **Verified by:** the existing steer, SF and SC suites stay green.

## Revision after the advisor's check (2026-10-02, before tests)

These override any earlier wording they contradict.

**SR-R1 (precise).**
- **Turn identity.** The identity of a turn (whichever mechanism carries it) survives a server restart and the adopt/follow path (`runner.py` adoption rebuilds a `FollowHandle` from the run dir). An adopted handle accepts only its own turn's exit status.
- **Docker.** The docker executor unlinks the same shared status file (`executor/docker.py` ~2504), so it is covered the same way.
- **Verified by (replaces the original line).** A test forces the predecessor's termination and its status write to be delayed. It then checks that either the launch waits for the predecessor to settle (SR-R2), or the later turn ignores the earlier status. Either way, the new turn is never finalized by the old turn's status.

**SR-R2 (precise).**
- **Launch gate.** Predecessor death is confirmed before `_launch`. Today, a successful `stop` goes straight to `_launch` without confirming it.
- **On uncertainty.** If the predecessor is not confirmed dead, the steer is refused through SF's existing release/hold path (`_steer_release`, `_steer_predecessor_dead` on the SF branch). There is no second cleanup mechanism.
- **Docker.** Under the docker executor, death means the container-side wrapper and agent process, not merely the host `docker exec` client.
- **Unknown liveness means refusal.**

**SR-R3 (precise).**
- **Resolution.** Fresh resolution uses the same layering as `start_agent` for the run's frozen route, including any limit set in the `models.<route>` entry.
- **What stays frozen.** Model, provider and options stay frozen, and stay frozen across a server restart. An adopted run must not rebuild them from the current config.
- **Invalid config.** If the current config is missing or invalid, the steer is refused BEFORE the predecessor is stopped.
- **Clock.** The wall clock starts when the new turn actually launches. Queueing and cleanup before the launch do not count against it.

**SR-R4 (precise).**
- **`elapsed_seconds`** in `check_agent`, `collect_agent` and the result text describes the current turn, or the last one if the run has finished.
- **Node lifetime** is reported as `node_elapsed_seconds`, wherever `elapsed_seconds` previously meant lifetime: status, collect and the server listings (`runner.py` ~6055 and ~6135, `server.py` ~516).
- **A finished turn's duration is frozen.** Its end time is persisted, so the duration stops growing.

**Consult (new scope line).**
- Consult sessions also resume through `_launch` (`runner.py` ~7714), and SR-R1, SR-R2 and SR-R4 apply to them.
- SR-R3 does not change consult's own call timeout and deadline semantics.

## Out of scope

- A `variant` or model override on steer (the user has noted it for later).
- A `timeout` argument on `steer_agent`. Config is the single source; reconsider only if it proves insufficient.
