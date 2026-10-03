# C3 — prompts reach the provider CLI through a run file: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requirement:** `context/specs/phase6-closing.md`, item C3. This finishes H8; the interim fix was 21c5816.
- **Ids:** `PF-R*`. They are never renumbered.
- **Process:** tester, then implementer-deep, then reviewer. No adversary, because the prompt file lives in the run directory, inside the container domain.

## Why

Today every adapter receives the prompt as one argv element. An argv element is bounded by the kernel (`MAX_ARG_STRLEN`, 128 KiB on Linux), and the whole argv and environment together by `ARG_MAX`. A long prompt then fails at `exec` with E2BIG, or forces a truncation.

The interim fix (21c5816) gives a clear error past the limit. This contract removes the limit for every shipped provider.

## Behaviours

**PF-R1: a declared prompt transport.**
- A provider declares in its config how its native CLI receives the prompt: argv as today, stdin, or a file path.
- The core writes the prompt to a per-turn file in the run directory before building the command. The core then hands it to the provider by the declared transport, so the prompt text is never placed in argv.
- The config key names and the placeholder (e.g. `{prompt_file}`) are the implementer's choice, documented in `defaults/providers.yaml`.
- Generic: no provider name in `src/multiagents/*.py` (P0-R8).
- **Verified by:** a fake provider declaring each transport receives the exact prompt bytes, and the prompt does not appear in its argv.

**PF-R2: every shipped provider is converted, end to end.**
- `claude`, `codex`, `agy` and `opencode` (and their `extends` variants) no longer carry the prompt in argv on any launch path. That covers:
  - a fresh start;
  - a resume (steer, consult, NEED_INFO answer);
  - the free retry;
  - the commit-fix resume;
  - both executors (local and docker).
- This includes codex's adapter, which today takes the prompt from its own argv (`defaults/providers/codex.py` ~332, ~943) and feeds the native CLI through stdin. The adapter now reads the prompt from the run file.
- Each native CLI is driven through a transport it actually supports. The implementer verifies this against each CLI's `--help` or documentation, and says how, in the commit message.
- **Verified by:** for each shipped provider and each launch path, a multibyte prompt over 128 KiB (e.g. 200 KiB of mixed ASCII, accented and 4-byte UTF-8 characters) reaches a fake native binary byte-for-byte. Fresh start and resume are both covered.

**PF-R3: the prompt file is turn-specific and safe.**
- **Naming.** Each turn writes its own file, named by turn. A later turn never reads an earlier turn's file, and a steer never overwrites the file that a still-draining predecessor is reading (SR).
- **Writing and opening.** The file is written with the existing descriptor-safe run-file helpers: no following of symlinks, created with owner-only permissions, and written by atomic replace.
- **Reading.** Reads are bounded, and a file over the bound is a clear error, not a silent truncation. The bound is configurable, with a default comfortably above any realistic prompt (e.g. 16 MiB).
- **Retention.** Prompt files are kept for the run's life, like `prompt.md` today, and removed with the run dir. The implementer says whether old turns' files are pruned.
- **Verified by:**
  - a symlink planted at the prompt file's path is not followed;
  - an oversize file gives the documented error;
  - two consecutive turns use distinct files.

**PF-R4: argv remains bounded with a clear error.**
- A provider still declaring the argv transport, such as a user's custom provider, keeps today's behaviour: a prompt over the argv limit is refused before spawning, with an error that names the transport option to switch to.
- **Verified by:** a fake argv-transport provider with a 200 KiB prompt gets that error, and no process is spawned.

**PF-R5: ordering.**
- The command is built after the prompt file exists. Today it is built before (`runner.py` ~3541, ~3579).
- A failure to write the prompt file fails the launch cleanly, before anything is spawned, and the error says why.
- **Verified by:** a write failure (e.g. a read-only run dir) gives a launch error and no spawned process.

**PF-R6: no regression.**
- Short prompts behave exactly as before, visibly to the agent.
- Session ids, stream parsing, the `prompt.md` diagnostics and resume all keep working.
- **Verified by:** the existing provider, adapter, launch, steer, consult, codex and H8 suites stay green.

## Out of scope

- Changing any native CLI.
- Compression or deduplication of prompts.
