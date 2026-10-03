# Phase 6 closing — small fixes C5, C6, C7: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requirements:** `context/specs/phase6-closing.md`.
- **Ids:** `C5-R*`, `C6-R*` and `C7-R*`. They are never renumbered.
- **Process:** each item gets its own tester, implementer and reviewer.
- **Common rule:** no provider name is added to `src/multiagents/*.py` (P0-R8).

## C5 — `doctor`'s CLI dependency probe resolves binaries the way spawns do

**C5-R1: the docker probe finds a CLI that a docker spawn would run.**
- The probe runs under `executor.kind: docker`, inside the project container. For each provider, it resolves the binary through the same resolution a docker launch of that provider uses, and runs that binary. This includes host-path mounts and the launch's PATH and HOME.
- If such a launch would succeed, the probe does not report `missing` / "binary not found in the container".
- **Verified by:** a provider whose binary is reachable at its absolute (mounted) path, but not on the container's system PATH. The docker probe reports the version, not `missing`.

**C5-R2: a genuinely absent binary is still reported.**
- If the binary that a docker launch would use does not exist in the container, the probe still reports `missing`, with the path it tried.
- **Verified by:** a provider whose resolved path does not exist in the container yields `missing`, and the detail names that path.

**C5-R3: no regression.**
- The host (`local`) probe behaves as today.
- **Verified by:** the existing manifest and doctor suites stay green.

## C6 — a pinned provider's auth status reports only its own account

**C6-R1: pinned means one account.**
- A provider pinned with `container_account: X`, or a provider inheriting a pin through `extends`/`auth_from`, reports through `auth_status`, `doctor` and the provider `check` action under docker the status of account X only.
- The detail names X.
- It is authenticated if and only if X is usable.
- **Verified by:** a vault with `default` ok and `accounts/b` expired, and a provider pinned to `b`: the pinned provider is `not_authenticated`, its detail mentions only `b`, and the fix names the login for that provider.

**C6-R2: the unpinned view is unchanged.**
- An unpinned provider lists the pool, which is every account minus those pinned by other providers.
- It is authenticated if at least one of them is usable (DK-R3a).
- **Verified by:** the same vault, base provider: authenticated through `default`, and `b` is not listed.

**C6-R3: no regression.**
- The local executor and non-claude providers are unchanged.
- **Verified by:** the existing DK, auth and doctor suites stay green.

## C7 — an adopted run keeps the `max_steps` it was launched with

**C7-R1: max_steps survives a server restart.**
- At launch, the effective `max_steps` of a turn is recorded in host-owned launch state, next to the timeout and the silence limit (the same record SR uses).
- When a server adopts a running turn, it governs that turn with the recorded value, not with the value in the current config.
- The adopted supervisor's provenance for `max_steps` says it was restored from the launch record.
- **Verified by:** start a run with agent `max_steps: 100`, change the config to 50, and restart and adopt the run. The turn trips only after more than 50 steps, i.e. it is governed by 100. Then the converse: start with 50, change to 100, adopt; the turn is governed by 50.

**C7-R2: a steer still picks up fresh limits.**
- A turn launched by `steer` resolves `max_steps` from the current config (SR-R3).
- **Verified by:** after the config changes from 100 to 50, a steered turn is governed by 50.

**C7-R3: a missing record is handled explicitly.**
- If the launch record is missing, or has no `max_steps` (for example a run launched before this change), the adopted turn uses the current config's value.
- Provenance then marks it as a fallback, not as restored.
- Agent-writable files (`command.json`, `tree.json`) are never consulted for limits.
- The SR rule for a missing launch time is unchanged: such a turn is already expired.
- **Verified by:** a ledger entry without `max_steps` falls back to config and the provenance says so. A forged `max_steps` in `command.json` has no effect.

**C7-R4: no regression.**
- **Verified by:** the existing SR, SF, D1 and internal suites stay green.

## Revision after the advisor's check (2026-10-03, advisor ag-894250, before tests)

These override any earlier wording they contradict.

**C5-R1a: which resolution counts.**
- The probe uses the native-CLI resolution a docker launch uses for that provider: the direct-launch versioned argv (`executor/docker.py` ~2435) or the adapter's `native_bin` (~2465). This includes `bin_search` and versioned symlinks.
- With `mount_cli_from_host: false`, it still uses a container PATH lookup.
- The probe's HOME is the project container's default HOME (not a run HOME). The implementer states which value it is.
- The probe still never starts or seeds the container.
- **Tests** exercise real resolution against temporary launchers, and capture the argv and env that reach the container exec (`exec_in_running`). A fake that succeeds whatever the argv is is not acceptable coverage.

**C6-R1a: what counts as usable, and inheritance.**
- "Usable" means usable or renewable. An expired access token with valid refresh credentials is usable, as today. In C6-R1's test, `b` must be **not renewable**.
- A provider with `auth_from` (or `extends`) whose owner is pinned, and that has no pin of its own, inherits the owner's pin for status purposes.
- An explicit pin on the dependent provider wins.
- Refreshing still renews every account (DK-R3a).
- **Tests:** a missing pinned account (no `accounts/X` at all) gives `not_authenticated` with X named. The detail for the pool never lists pinned accounts.

**C7-R1a: exact boundaries and where provenance shows.**
- **Boundaries:** governed by 100 means no trip at step 100 and a trip at step 101.
- **Which config limit:** the tests cover a `max_steps` set at agent level and one set at global (project) level.
- **Where provenance shows:** the restored-or-fallback provenance appears in the observable outputs that report limits. These are `check_agent` / `effective_limits` and the runaway-steps notice (`runner.py` ~5278, which today regenerates provenance from the current config).
- **What provenance keeps:** the original launch source, file and line.

**C7-R3a: partial ledger.**
- If a ledger entry has a timeout but no `max_steps`, the timeout is restored and `max_steps` falls back to the current config.
- An eviction from the ledger (256 entries) counts as a missing record.

## C11 — a conversation that cannot be resumed is reported, not replaced silently

Root cause: `context/specs/phase6-closing.md`, Progress, C11.

**C11-R1: a lost session is its own outcome.**
- When a resumed turn (steer or consult) ends because the provider could not find the requested session, the run's status reason says so distinctly, as `session_lost`. Concretely, that is when the adapter's resume check reports that the requested id was not observed and no session was produced.
- It is visible in `check_agent` and `collect_agent`, and it names the requested session id.
- It is not a plain `failed` with only the stderr tail.
- Detection is generic: an adapter or provider config signals it. No provider name goes in core code.
- **Verified by:** a fake provider whose resume emits no session id. The turn ends with reason `session_lost`, and the requested id is reported.

**C11-R2: the next consult says the conversation was replaced.**
- When `consult(agent)` finds that the agent's last conversation ended `session_lost` (or is otherwise unresumable), it starts a new conversation.
- The reply then carries `conversation_replaced`, in the same shape as CX-C28, naming the previous agent id and the reason.
- A consult that resumes normally carries no such field.
- **Verified by:** consult, then force a lost session, then consult again. The second reply has `conversation_replaced` with the old id and the reason `session_lost`.

**C11-R3: steer on a lost session says what to do.**
- `steer_agent` on a run whose last turn ended `session_lost` does not pretend to resume.
- It either refuses with a message that says the session is lost and that a fresh start should be given the old run dir, or it does what C11-R2 does and says so.
- The implementer chooses which, and explains why.
- **Verified by:** steer after a forced lost session. The result names `session_lost`, and no turn is reported as resumed.

**C11-R4: no regression.**
- **Verified by:** the existing consult, CX-C28, steer and codex adapter suites stay green.

## C12 — transcript usage vocabulary comes from provider config

**C12-R1: provider config declares where a transcript record's usage lives.**
- Context-size reading for a launched role (`transcripts.py`, `_usage_of`) finds the usage object through a path declared in the provider's config.
- Claude's shipped config declares today's `message.usage`, so Claude's behaviour is unchanged.
- No provider vocabulary stays hard-coded in `src/multiagents/*.py` (P0-R8).
- **Verified by:** a fake provider that declares a different path (e.g. `payload.token_usage`) has its context size read correctly. Claude's transcript fixture still reads the same number as today.

**C12-R2: no declaration means unknown, never a misread.**
- A provider whose config declares no usage path yields an unknown context size (no reading), never a number parsed with another provider's vocabulary.
- **Verified by:** a transcript in Claude's shape, read for a provider with no declaration, gives no reading.

**C12-R3: no regression.**
- **Verified by:** the existing transcript, context and compaction (driver) suites stay green.
