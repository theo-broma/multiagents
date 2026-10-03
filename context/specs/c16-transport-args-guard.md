# C16 — a prompt transport and its spawn args must agree: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requirement:** `context/specs/phase6-closing.md`, item C16.
- **Ids:** `TG-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer.

## Why

C3 moved the shipped providers to `prompt_transport: stdin` or `file`. A config layer that still sets a pre-C3 `spawn.args` list, with `{prompt}` in argv, silently replaces the shipped list, because lists do not merge. Today the core fills `{prompt}` with an empty string. The result is an agent launched with an empty prompt, as happened to agy (an "empty prompt" error) and codex (`--prompt ''`).

## Behaviours

**TG-R1: a mismatch is refused at admission.**
- **The rule.** A launch is refused before any process is spawned when:
  - the provider's effective transport is `stdin` or `file`, and its resolved `spawn.args` or `spawn.resume` still contains `{prompt}`; or
  - the transport is `file` and the args contain no `{prompt_file}`.
- **The error says three things:**
  - which provider is affected;
  - the config layer and file that set the conflicting key, with its line where available;
  - the fix: drop the stale `args` override, or set `prompt_transport: argv` explicitly.
- **Verified by:** a provider whose layered config sets transport `stdin` with args containing `{prompt}` is refused with that error, and nothing is spawned. The same holds for a `file` transport with no `{prompt_file}`.

**TG-R2: `doctor` reports it.**
- `multiagents doctor` lists each such provider as a problem, with the same layer, file and fix information, and without launching anything.
- **Verified by:** the same configs as TG-R1 produce a doctor problem line, and a correct config produces none.

**TG-R3: `{prompt}` is never silently emptied.**
- No launch path substitutes an empty string for `{prompt}` because the transport is not `argv`. Either the transport is `argv` and `{prompt}` carries the prompt (bounded, PF-R4), or the launch is refused (TG-R1).
- **Verified by:** no test configuration reaches a spawn with an empty argv element where `{prompt}` was.

**TG-R4: an explicit argv opt-in still works.**
- A provider that sets `prompt_transport: argv` together with `{prompt}` in its args behaves as before C3, including PF-R4's bounded refusal.
- **Verified by:** a short prompt is delivered in argv; one over the limit is refused with PF-R4's message.

**TG-R5: no regression.**
- The shipped providers, which have matching transport and args, launch as today.
- The C3 suites stay green.
