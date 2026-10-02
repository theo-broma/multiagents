# A second agy account under the docker executor — the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requested by:** the user ("fais implementer les modifs pour agy second compte en docker").
- **Ids:** `AB-R*`, never renumbered.

## Background

Researcher ag-b643b9 established the following:
- On the host, agy keeps its OAuth token in the GNOME keyring. Relocating `HOME` does not relocate it, so a second account is impossible under the local executor.
- Inside the docker container, agy falls back to a token file under `.gemini` (`defaults/providers/agy.sh`, around lines 4-12 and 46-90). A provider whose `env:` sets `HOME` to its own profile, with its own `container_private_home`, could therefore hold a second login. Two things are missing:
  - `multiagents docker login <provider>` hardcodes `HOME=Path.home()` (`cli.py` around lines 3008-3012).
  - The agy `budget` action runs `/usage` with the local binary (`agy.sh` around lines 92-115), so it always reads the primary account.

The target configuration, which the user will write in the project's `providers.yaml`:

```yaml
agy-b:
  extends: agy
  family: agy
  env: {HOME: "~/.multiagents/profiles/agy-b"}
  container_private_home: [".multiagents/profiles/agy-b/.gemini"]
```

## Behaviours

**AB-R1: docker login honours the provider's HOME.**
- `multiagents docker login P` runs P's login inside the container with `HOME` set to P's own `env: HOME`, resolved exactly as a launch resolves it (`providers.expand_env_value`).
- A provider without `env: HOME` behaves exactly as today.
- Generic: no provider name is added to `src/multiagents/*.py` (P0-R8).
- **Verified by:** a test with a fake docker exec that captures the login command's environment. For P with `env: {HOME: "~/x"}`, HOME is the expanded path; for the base provider, HOME is unchanged.

**AB-R2: the agy budget reads the account of the provider it is asked about.**
- Under the docker executor, the `budget` action for a provider whose `env:` sets `HOME` reads `/usage` for THAT profile, by running inside the container with that HOME.
- The base `agy` and `agy-partner` read the primary account as today.
- The mechanism is the implementer's choice: in `agy.sh`, or a generic way for provider actions to run in the provider's profile.
- **Verified by:** a fake agy binary or container that answers `/usage` differently depending on HOME. `agy-b`'s budget reports the second profile's numbers, and `agy`'s reports the first's.

**AB-R3: never the wrong account's numbers.**
- If the second profile has no login yet, or its token is missing or expired, `agy-b`'s budget is **unknown**, with a note that says so (for example "not logged in: run `multiagents docker login agy-b`").
- It must never report the primary account's quota.
- Its quota cache does not collide with `agy`'s, in memory or on disk.
- **Verified by:** an empty second profile gives `known: false` and no primary numbers; both providers read in turn keep their own values.

**AB-R4: under the local executor, nothing pretends.**
- Under the local executor, a provider that `extends: agy` and sets `env: HOME` cannot be a distinct account, because the keyring is shared.
- `doctor` and the config load warn that, under the local executor, such a provider uses the primary agy account.
- Launches are not refused: the user may want the docker setup ready before switching.
- **Verified by:** loading such a config with `executor.kind: local` yields the warning; with `docker` it does not.

**AB-R5: no regression.**
- `agy`, `agy-partner`, `claude`, `claude-b`, codex and opencode login, launch and budget behave as before.
- **Verified by:** the existing agy/docker/budget/login suites stay green, and `tests/test_budget_extends_reader.py` stays green.

## Out of scope

- Switching this project to `executor.kind: docker`. That is the user's decision and needs a container recreate.
- Any change to `egress_allowlist`. agy's hosts are already listed.
- Writing the user's `agy-b` entry into the project config. The orchestrator does that after the merge.
