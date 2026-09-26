# Quota freshness — no refusal on a window that has already reset

Status: contract, 2026-09-26. Ids `QF-R1`… are stable; never renumber.

## Why

On 2026-09-24 `multiagents run` refused to start as out of credit about three
minutes AFTER the claude window had reset. Researcher ag-92d428 traced the
refusal: `cli.py` `cmd_run` → `driver._orchestrator_hold` →
`budget.read_all(..., use_cache=False)` → `Budget.usable`. It found that
`use_cache=False` bypasses only the in-process cache. Three readings can still
outlive the reset:

- the CLI's own `cachedUsageUtilization` in `~/.claude.json`, trusted without
  a fetch while `stale_seconds <= STALE_AFTER` (900 s);
- the shared usage file `usage-claude*.json`, trusted for `SHARED_TTL` (300 s)
  plus jitter, or longer after a `Retry-After`;
- cooldowns and quota pauses in `tree.json`, up to `blind_cooldown_seconds`.

No command clears them. `doctor --clear` clears the cooldowns only.

The principle: **a reading carries its own expiry.** A window whose
`resets_at` is in the past says nothing about the present, whatever the age of
the cache that holds it.

## Behaviours

**QF-R1 — a window past its reset does not count.** When any budget reading,
from any source or cache layer, contains a window whose `resets_at` is at or
before now, the reading is not used as it stands. It is replaced by a fresh
read of that provider when one can be made (see QF-R2). If no fresh read can be
made, that window counts as 0 % used, and the provider's headroom and
`resets_at` are recomputed from the remaining windows.
A window that has no `resets_at` is unaffected.
Verified by: unit tests on `budget` with a cached reading (CLI cache, shared
file and in-process cache, each separately) holding a window at 99 % with
`resets_at` in the past. `usable` is then true, or the fresh read is used.
Also: a window in the past next to another window at 99 % in the future stays
unusable.

**QF-R2 — a fresh read after a reset is attempted, and bounded.** On the claude
path, a reading with a past `resets_at` triggers one fetch of the usage
endpoint, even inside `SHARED_TTL`, and the shared file is rewritten with the
result. The existing back-off still wins. After a 429 or a `Retry-After` that
has not elapsed, there is no fetch and QF-R1's fallback applies. At most one
such fetch is made per provider per process per 60 s, so a reset cannot become
a fetch storm across agents.
Verified by: tests with the fetch stubbed, counting calls. A past reset inside
TTL gives exactly 1 fetch. An unelapsed Retry-After gives 0 fetches and the
fallback is used. Repeated reads within 60 s give 1 fetch.

**QF-R3 — the refusal says what it is based on.** When `run` refuses on quota
grounds, the message names the provider, the window that is full, that
window's `resets_at` (absolute and relative), the source of the reading and
its age in seconds. It also names the command in QF-R4.
Verified by: a CLI test on the refusal output.

**QF-R4 — `multiagents refresh-quota [provider…]`.** The command forces a
fresh read of the named providers, or of all of them when none are named:
- it bypasses and replaces every cache layer above, including the shared file,
  and it ignores the CLI's `cachedUsageUtilization`;
- a Retry-After or 429 back-off is still honoured: the provider is then
  reported as "not re-read: rate-limited until <time>", never hammered;
- for each provider whose fresh reading is known and usable, it clears that
  provider's cooldown in `tree.json` and any tree pause whose cause is that
  provider's quota;
- a provider whose fresh reading is unusable, or unknown, keeps its cooldown
  and pause.

It prints one line per provider: name, headroom, worst window, reset, source,
and what was cleared.
Exit codes:
- `0` when the orchestrator's provider is usable afterwards;
- `3` when it is not (the same code `run` uses for a quota refusal);
- `2` for an unknown provider name.

It changes no config and starts nothing.
Verified by: CLI tests with the providers stubbed, covering:
- a clear after a usable read;
- no clear after an unusable read;
- no clear after an unknown read;
- a rate-limited provider not fetched;
- all three exit codes.

**QF-R5 — `run --wait` recovers on its own.** The `--wait` polling loop gets
the QF-R1/R2 freshness on each poll, so it proceeds within one poll interval of
the provider's `resets_at` once the provider reports room. It must not wait for
a cache TTL to expire as well.
Verified by: a driver test with a stubbed clock and a reading that resets
between two polls.

## Out of scope

- The burn-rate projection (bug-c050b0).
- Changes to `blind_cooldown_seconds` or its default.
- Providers without a `resets_at`: `known: false` stays `known: false`.
- The CLI's `~/.claude.json` file: we never write to it.

## Constraints

- No claude special-casing outside the claude reader. QF-R1 applies to every
  provider's windows, including readings from a provider script.
- `item 8` (`context/specs/agent-survival.md`) is being implemented at the same
  time and holds `cli.py` (`cmd_stop`, `cmd_resume`, the parser near `stop`),
  `runner.py`, `server.py` and the executors. Keep the `cli.py` change to a new
  subcommand and its parser entry, plus the QF-R3 message.
