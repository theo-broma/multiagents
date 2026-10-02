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

## Out of scope

- A `variant` or model override on steer (the user has noted it for later).
- A `timeout` argument on `steer_agent`. Config is the single source; reconsider only if it proves insufficient.
