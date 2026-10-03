# Phase 6, closing round

**Status: requirements, not a contract.** Written by the initializer on
2026-10-03, with the user's agreement. These are the last items of phase 6
plus the defects found after the switch to `executor.kind: docker` that
morning. Ids are `C1`… The orchestrator writes a contract where an item
needs one; several items need none.

The user's standing rules all apply (see memory and BRIEF): the reviewer
runs after **every** implementer, one implementer per item, work still
rejected at review round 3 moves to opus, and tickets are fixed in-house and
never submitted.

## Items

- **C1 — a live compaction check (H10).**
  - Exercise a real compaction of a running orchestrator or agent, end to
    end, under docker.
  - Record the outcome in `phase0-context-and-team.md` (R8).
  - If it fails, the failure becomes an item.

- **C2 — branch triage (the rest of H12).** There are 11 unmerged branches
  on 2026-10-03.
  - `agents/reviewer/{98e037,e4919f,e8565d,37f81c,fdbf30,e6b702,ea1d60}`
    and `agents/robustness-tester/6e70df` each hold one "work in progress"
    commit, which is most likely the run's report or patch files.
    - Read each one.
    - If it holds a finding that was never acted on, record it as an item or
      a ticket.
    - Then discard the branch.
  - `agents/adversary/18bf9a` holds T1 amendments (DQ-R8/R8a, DQ-R2a,
    DQ-R4a/R3c).
    - Check them against `t1-deferred-queue.md` on main.
    - Merge whatever is missing; otherwise discard.
  - `agents/tester/b689bf` (R18) and `agents/tester/cf02a1` (R20) hold phase
    3 tests, and phase 3 is paused. **Keep them**, and record them in BRIEF
    as belonging to phase 3.
  - Before deleting anything, read the branch. Record what each one was and
    what was done with it.

- **C3 — prompt transport over a file (the rest of H8).**
  - Today adapters receive the prompt as one argv element, and only the
    interim fix is merged (21c5816).
  - Add a bounded run-file transport to the provider contract: the prompt
    is written to the run's directory and the adapter reads it. This is
    generic, with no provider name in core code.
  - Keep a clear error for any path that still uses argv past the limit.
  - All four shipped providers are converted.

- **C4 — close H5 (the claude budget under docker), with evidence.**
  - DK (`docker-claude-auth.md`) and AB probably cover it: `budget_status`
    now reports claude per vault account (`default/…`, `b/…`).
  - Verify that the reading is for the account the container's agents
    actually spend against. If it is, close H5 with that evidence;
    otherwise fix it.

- **C5 — `doctor` false positive: "binary not found in the container".**
  - The CLI dependency probe runs the bare binary name against the
    container's system PATH (`manifest.py` ~998). The CLIs are mounted at
    their host paths.
  - The probe must resolve them the way spawns do.

- **C6 — DK-R3a deviation.**
  - `auth_status` for a pinned provider (claude-b, `container_account: b`)
    lists every vault account (`b`, `default`), not only `b`.

- **C7 — SR leftover (reviewer ag-f9def3, non-blocking).**
  - When a run is adopted, `max_steps` comes from the current config
    instead of the host-owned limits ledger.
  - Make it consistent with D1 finding 8 and SR: limits are taken from
    host-owned state captured at launch.

- **C8 — reset times shown as raw UTC ISO in the monitor.** This is a ticket
  that was never filed, kept in `context/tickets/2026-09-30-unfiled.md`.
  - Check whether it is still true. If it is, fix it the general way:
    post-process ISO timestamps in script `usage` lines through
    `reset_label`.
  - Then trim that file: its other two tickets are fixed by T1 (the
    deferred task that vanished) and T2 (several tickets in one run). Note
    the commits.

- **C9 — config warnings.** This one is the user's decision, with defaults.
  `.multiagents/config/agents.yaml` is gitignored, so a change is proposed,
  never made silently.
  - `advisor`, `dev-advisor` and `adversary`: `models.agy.variant` is
    ignored, because agy consumes no variant.
    - DEFAULT: remove the key.
    - Alternative: map it to the effort agy does support, if it was meant
      as one.
  - `bug-reporter`: `opencode-go/kimi-k2.6` is no longer listed, and
    opencode Go is at 99 % of its monthly quota anyway.
    - DEFAULT: codex `gpt-5.6-terra`, the same tier as the researcher.

- **C10 — finish.** Move wt-main to main, then the user runs `/mcp`. Run a
  full suite on the host (`scripts/test-chunk.sh`, see AGENTS.md). Expect
  only the 72 by-design reds in `test_phase2_*`.

## Order and pipelines

- **C2, C4 and C8** first: they are mostly reading, and cheap.
- Then **C5, C6, C7**, small fixes. Each gets a tester, an implementer and
  the reviewer.
- Then **C3**. It changes the contract of every provider, so it needs a
  contract, a tester, implementer-deep and the reviewer. No adversary: the
  prompt file lives in the run directory, which is already within the
  container domain.
- **C1** whenever nothing else is running. It is a live check.
- **C9** as soon as the user answers.
- **C10** last.
