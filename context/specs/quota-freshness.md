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

**QF-R1 — a window past its reset does not count.** A window is *past its
reset* when its `resets_at` carries a timezone and lies at least
`quota_reset_margin_seconds` before now. That is a new project limit, default
120 s, which absorbs clock skew. A `resets_at` with no timezone, or one that
cannot be parsed, is never past its reset. When any budget reading, from any
source or cache layer, contains a window that is past its reset, the reading
is not used as it stands. It is replaced by a fresh
read of that provider when one can be made (see QF-R2). If no fresh read can be
made, that window counts as 0 % used, and the provider's headroom and
`resets_at` are recomputed from the remaining windows.
A window that has no `resets_at` is unaffected.
Verified by: unit tests on `budget` with a cached reading (CLI cache, shared
file and in-process cache, each separately) holding a window at 99 % with
`resets_at` past the margin. `usable` is then true, or the fresh read is used.
Also:
- a window in the past next to another window at 99 % in the future stays
  unusable;
- a `resets_at` 60 s in the past (inside the margin) is still counted;
- a naive `resets_at` is still counted.

**QF-R2 — a fresh read after a reset is attempted, and bounded.** On the claude
path, a reading with a past `resets_at` triggers one fetch of the usage
endpoint, even inside `SHARED_TTL`, and the shared file is rewritten with the
result. The existing back-off still wins. After a 429 or a `Retry-After` that
has not elapsed, there is no fetch and QF-R1's fallback applies. At most one
such fetch is made per provider **per machine** per 60 s, whether it succeeds or
fails. The attempt is recorded where every process on the host sees it (the
shared usage file or a sibling of it, not process memory), because every agent
runs its own server process. A failed attempt therefore backs off every process
for 60 s, and the reset cannot become a fetch storm.
Verified by: tests with the fetch stubbed, counting calls. A past reset inside
TTL gives exactly 1 fetch. An unelapsed Retry-After gives 0 fetches and the
fallback is used. Repeated reads within 60 s give 1 fetch. Two independent reader instances
(simulating two processes sharing the state directory) give 1 fetch between
them, including when the first fetch failed.

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
  provider's **quota** cooldowns and any tree pause whose cause is that
  provider's quota. It never clears a cooldown with another cause: an auth
  failure (`needs_login`), a crash loop (`provider_down_cooldown_seconds`),
  or a correlated integration failure (`_maybe_cool_family`). A usage API
  answering proves the account has quota, not that the CLI works. If a
  cooldown or pause record does not carry its cause today, it must from now
  on. A record with no cause is treated as not a quota record, and is left
  alone;
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
- a non-quota cooldown (auth, crash loop) surviving a usable read;
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

**QF-R6 — a quota pause ends when fresh room is seen.** A tree pause whose
cause is a provider's quota is lifted as soon as a fresh reading (QF-R1/R2) of
that provider is known and usable, rather than at its `until`. This happens in
three places:
- at `run` startup, after `_orchestrator_hold` passes;
- when the `--wait` loop proceeds, because today it proceeds without clearing
  the pause, and the first spawn is then refused by the runner's preflight
  pause check;
- in `refresh-quota` (QF-R4).

The same rule as QF-R4 applies: pauses with another cause, or with none
recorded, are untouched, and so are pauses from `spend_limit_pause_hours` or
a user `stop`.
Verified by: a driver test in which `--wait` proceeds and the first
`start_agent` then succeeds, and a test in which a non-quota pause survives.

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

## Decisions, 2026-09-26 (orchestrator, answering tester ag-00375d)

- **Cause of a cooldown or pause:** `Tree.set_cooldown(..., cause=...)` and
  `Tree.pause(..., cause=...)`, a string. The values are `"quota"`, `"auth"`
  (today's `needs_login`), `"provider_down"` (crash loop) and `"family"`
  (correlated integration failure). A record written before this change has no
  cause, and is treated as not quota.
- **Clock:** every "now" in this feature comes from `time.time()`.
- **`quota_reset_margin_seconds`** lives under `limits:` in `project.yaml`
  (default 120).
- **`refresh-quota` and the 60 s bound:** the command is an explicit user
  request, so it is NOT held back by QF-R2's 60 s bound. It still honours a
  429 or an unelapsed `Retry-After`, and it records its attempt so that other
  processes' 60 s bound counts from it.
- **A pause naming several providers** is lifted only when every provider it
  names has a fresh, known, usable reading.
- **Exit code of `refresh-quota` when the orchestrator's provider is not
  named:** it still reflects that provider's usability afterwards, read the
  ordinary way (QF-R1 applies; no forced fetch).
- **A reading with a top-level `resets_at` and no `windows`** is one window
  for QF-R1.
- **Every window past its reset, no fresh read possible:** 0 % used, headroom
  1.0, `resets_at` none.
