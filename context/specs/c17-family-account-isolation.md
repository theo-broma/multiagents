# C17 — two accounts of one provider family coexist under docker: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requirement:** `context/specs/phase6-closing.md`, items C14 (reopened) and C17.
- **Diagnosis:** researcher ag-e83d9b.
- **Ids:** `FA-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer-deep, then reviewer.

## Why

The user registered a second codex account as `codex-b: {extends: codex, family: codex, env: {MULTIAGENTS_CODEX_PROFILE: ...}}`, the shape C14's research recommended.

Under docker, both providers inherit `container_private_home: [".codex"]`. `DockerExecutor.private_state()` (`executor/docker.py` ~1800-1813) keys the mount map by container path, so `codex-b`'s backing silently replaced `codex`'s at `~/.codex`. Every plain codex agent would then have run without its login.

`multiagents docker login codex-b` also failed: it execs the native CLI with only `HOME`, `PATH` and `TERM` (`cli.py` ~3057), so it logs into whatever is at `~/.codex`. It then died with EACCES, inferred to come from the nested read-only `~/.codex/packages/standalone/releases` mount.

`agy-b` works today only because its config gives it a distinct private path by hand, through `container_private_home` and `HOME`.

## Behaviours

**FA-R1: no silent mount collision.**
- When two providers with different credential owners resolve a private-state entry to the same container path, the docker executor refuses. It does so at config load or at `docker up`/admission, before any mount is made.
- The error names both providers, the path, and the fix: give one a distinct private path, or share credentials with `auth_from`.
- Providers that share an owner through `auth_from` (PS-R2) are not a collision.
- **Verified by:** two providers with distinct owners and the same `container_private_home` entry are refused, with both names in the error. An `auth_from` pair is accepted.

**FA-R2: a second codex account is declarable without code.**
- There is a documented config shape for a second account of the codex family (a provider that `extends: codex`) that gives it:
  - its own container-private codex home, distinct from `codex`'s;
  - its own login;
  - its own runs and budget reading.
- Its runs use its own home and never `codex`'s. `codex` agents keep theirs.
- The shape is documented in `defaults/providers.yaml`, next to the existing commented `claude-b`/`agy-b` examples. The implementer chooses the mechanism: a per-provider `container_private_home`, which the adapter already reads through `MULTIAGENTS_PRIVATE_HOME`; or a vault `container_account` through the auth sidecar, as `claude-b` does. The implementer says why in the commit.
- **Verified by:** with `codex` and the documented `codex-b` both configured, the private-state map has two distinct container paths, each backed by its own owner directory. The environment of a `codex-b` run (`adapter_env`) points the codex profile at `codex-b`'s path, and a `codex` run at `codex`'s.

**FA-R3: `docker login` logs the right provider in.**
- `multiagents docker login <p>` gives the native CLI the same profile location that `<p>`'s runs will use, so a login for `codex-b` lands where `codex-b`'s runs read, and never in `codex`'s home.
- If the provider's private home cannot be written by the login, for example because a read-only nested mount blocks a write the CLI needs, the command fails with a message that says which path, not a bare EACCES.
- **Added 2026-10-03.** `docker login codex` fails the same way for plain `codex`. The command execs the bare `codex` binary, which is the TUI: it starts the app-server daemon and dies with EACCES. In the container, `codex login --device-auth` is the working form, and the container has no browser. `docker login` must run the provider's declared non-interactive login argv, for codex `login --device-auth`, rather than the bare binary.
- **Verified by:** the argv and environment built for `docker exec` for `codex-b` name `codex-b`'s profile location; for `codex` they name `codex`'s. This is tested at the argv-building seam, without a real docker.

**FA-R4: `doctor` reports it.**
- `multiagents doctor` lists an FA-R1 collision as a problem, without launching anything.
- **Verified by:** the FA-R1 collision config gives a doctor problem line, and the FA-R2 shape gives none.

**FA-R5: no regression.**
- A single `codex` provider, `claude`/`claude-b` (sidecar, `container_account`) and `agy`/`agy-b` keep their current mounts and environment byte for byte.
- The existing docker-executor, PS (private state), codex-adapter and auth suites stay green.

## Out of scope

- Migrating any existing login. The `shared/codex-b/.codex` left by the failed attempt may be reused or ignored.
- Local (non-docker) execution, where `MULTIAGENTS_CODEX_PROFILE` already separates the accounts.
