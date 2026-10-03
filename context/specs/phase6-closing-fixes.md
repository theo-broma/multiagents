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
