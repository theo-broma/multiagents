# H7: provider startup failures, binary resolution, model pins — the contract

**Status:** contract, written by the orchestrator on 2026-09-29, and
revised the same day after the advisor's review (ag-8e7d87, turn 3).
- **Source:** phase6-hardening.md, item H7, including the user's note on
  binary resolution.
- **Research:** ag-aedab3.
  - 25 failed opencode runs were found. 16 of them failed at startup with
    `Unexpected server error`, exit 1 and zero steps.
  - None of them failed because the binary was missing.
  - Binaries are looked up again in each place that needs one, not
    resolved once.
  - The circuit breaker is reset by any success, and it re-admits a
    provider when its cooldown expires.
  - A model override is not a pin.
- **Ids:** `PS-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## Behaviours

**PS-R1: resolving the binary.**

A new provider field, `bin_search`, is an optional list of extra
directories. `~` is expanded in it, and relative entries are refused at
config load.

`Provider.resolve_bin(env=None) -> ResolvedBin{path, launcher, via,
searched}` is the one host-side lookup:
- `path` is absolute and has its symlinks resolved. It is `None` when
  nothing was found.
- `launcher` is the path as found, before symlinks are resolved. The
  docker version-root mount needs this path.
- `via` is one of `bin`, `PATH` and `bin_search`.
- `searched` is the ordered list of places looked at.

The lookup runs in this order:
1. **`bin` itself, when it contains a `/`.** After `~` expansion it must
   be absolute. When it is missing or not executable, resolution fails
   there. It does not fall through to the next steps.
2. **`PATH` from `env`,** or from the server process when `env` is
   omitted. An empty or unset `PATH` is skipped.
3. **Each `bin_search` directory**, in order.

An entry counts only when it is an executable regular file, or a symlink
to one.
- **Lifetime.** Resolution runs per operation, against the config
  snapshot that operation uses. It is never cached for the life of the
  process.
- **Shipped config.** The shipped `opencode` provider gets
  `bin_search: ["~/.opencode/bin"]`.

Verified by:
- a fake binary found through each of the three routes;
- an empty `PATH` with the binary only under `bin_search`, found with
  `via: "bin_search"`;
- a non-executable file at the right name, which is skipped;
- an explicit missing `bin: /x/y`, which fails without searching `PATH`;
- a symlinked launcher, which reports `launcher` and `path` distinctly.

**PS-R2: every host-side consumer uses that result.**

These consumers take the binary from `resolve_bin()`:
- native launch;
- the docker host-mount derivation when `mount_cli_from_host` is true,
  which uses `launcher` for the `bin_versions_depth` root;
- the auth `check` and `login` scripts;
- the budget and usage scripts;
- `doctor`;
- `refresh-models`.

None of them calls `shutil.which` on the provider's `bin` independently.

The details:
- **When the CLI lives in the container.** With `mount_cli_from_host:
  false`, the container binary is resolved in the container, as it is
  today. The binary's absence on the host neither blocks nor warns for
  container launches.
- **Scripts, when the binary was found.** `MULTIAGENTS_BIN` is the
  absolute resolved path.
- **Scripts, when it was not found.** The script still runs, because
  file-based actions such as reading a budget from `auth.json` do not
  need the binary. `MULTIAGENTS_BIN` is empty, and
  `MULTIAGENTS_BIN_ERROR` carries the PS-R3 message.
  - A shipped script action that needs the binary prints that message
    and exits 20 (unknown).
  - The shipped scripts never search `PATH` themselves: the
    `${MULTIAGENTS_BIN:-opencode}`-style fallbacks are removed.
- **`models_cmd`.** Its first element is replaced by the resolved path
  when it equals the provider's `bin` or that name's basename.

Verified by: with `PATH` lacking the binary and `bin_search` pointing at
a fake, each of these finds and runs the fake:
- native launch;
- `auth check`;
- `refresh-models`;
- `doctor`.

A budget action that is file-based still runs when the binary is
missing.

**PS-R3: a not-found error says where it looked and how to fix it.**
- **The message** names the provider and every entry of `searched`, in
  order. It then says how to fix it: set `bin:` to an absolute path, or
  add the directory to `bin_search:` in `providers.yaml`.
- **Where it appears:**
  - in the `start_agent` error;
  - in `multiagents auth login <provider>`;
  - in `doctor`.
- **`doctor` for a found binary** shows the path and its `via`.

Verified by: message tests on those three surfaces.

**PS-R4: a startup failure is its own category, and "progress" is
defined per provider.**

- **Startup progress.** Evidence that the model actually ran, meaning at
  least one of:
  - non-empty assistant text;
  - a tool call;
  - usage with output tokens greater than zero.

  These do **not** count:
  - initialization or session events, such as claude `system` init,
    codex `thread.started` or `turn.started`, and opencode session ids;
  - echoed input;
  - error events.

  Each provider declares its startup-progress signal in its stream rules
  or adapter. It is distinct from the generic `step` used by the
  watchdogs, and the watchdogs are unchanged.
- **Failed at startup.** A run *failed at startup* when both hold:
  - it ended in failure, through a non-zero exit or a failed status;
  - it produced no startup progress.

  Excluded, because they are already classified: a run multiagents
  refused or stopped, and a quota or limit end.
- **The count and its threshold.**
  - Consecutive startup failures are counted per provider, separately
    from the general health counter.
  - The count resets only on startup progress from a run launched on
    that provider.
  - At `limits.startup_failure_threshold` (default 2) the provider is
    marked `startup_down`, with an event naming the provider, the count
    and the last run's first error line.
  - Routing excludes a `startup_down` provider, as it excludes
    `provider_down`.

Verified by:
- for claude and codex fake streams, init events followed by a failure
  count as a startup failure;
- init plus one assistant text followed by a failure does not count;
- two startup failures mark the provider;
- a success on another provider does not reset the count.

**PS-R5: recovery is demonstrated, and its state is host-owned.**

- **Where the state lives.** The `startup_down` mark, the startup
  failure count and the probe claim live in the protected per-project
  host-state directory (`authority.py` ~20), under an atomic lock.
  - They are not in `tree.json`, which agents can write. `tree.json` may
    mirror them for display only, and nothing reads the mirror back for
    a decision.
  - A runner that cannot reach the host-state directory treats a
    `startup_down` provider as down. It fails closed.
- **Half-open.** After `limits.provider_down_cooldown_seconds`, the
  provider is *half-open*: routing may give it **one** probe run at a
  time.
  - The claim is taken atomically, before launch, and records the run id
    and a generation token.
  - All other routing treats the provider as down.
- **What happens to the probe:**

  | Probe outcome | Mark | Claim |
  |---|---|---|
  | Startup progress | Cleared at once, with a `provider_recovered` event | Released |
  | Startup failure | Re-marked for another cooldown | Released |
  | Exits without progress and without failure | Stays; a new probe may follow | Released |
  | Refused or stopped by multiagents, launch failure, or cancellation | Stays | Released |
  | Quota or limit end | Stays | Released |
  | Hangs | Stays | Held until the run ends. The existing watchdogs apply. |

- **After a restart.** A claim whose run is no longer alive is released
  at reconciliation. A completion carrying a stale generation token is
  ignored.
- **Independence from the other health state.** The general health path
  never clears `startup_down`. That includes a success removing a
  provider's cooldown (`tree.py` ~903). Clearing `startup_down` never
  lifts an independent auth or quota restriction.
- **Runs already in flight.** A run launched before the mark, or any run
  other than the probe, does not clear it.

Verified by:
- with the cooldown expired, two concurrent starts give exactly one
  probe;
- the probe succeeds and the mark clears;
- the probe fails and the provider is re-marked;
- a probe with no outcome leaves the mark and releases the claim;
- a restarted Runner still sees the mark and releases a dead claim;
- a forged `startup_down` entry, or its absence, in `tree.json` changes
  nothing.

**PS-R6: an explicit `start_agent(model=…)` pins its provider.**
- **Which provider.** The call-level model is resolved to a provider
  exactly as it is today, with unchanged match precedence, including the
  catalog lookup (`runner.py` ~1690).
- **What the pin forbids.** The run is pinned to that provider:
  - no family sibling;
  - no fallback;
  - no other account.

  The pin carries over to steer and resume of that run.
- **When the pinned provider cannot take the run**, `start_agent`
  **refuses**. That covers a provider that is:
  - disabled or has a missing binary;
  - down or `startup_down` and not claimable as a probe;
  - out of quota, or held by the reserve or wind-down;
  - known to be unauthenticated.

  The refusal is structured:
  - `reason`, and `retry_after` when known;
  - a message saying that omitting `model` lets the router choose.
  - It never enqueues, defers or pauses the tree.
- **Unknown auth** does not block the pin.
- **A pinned call may take an available half-open probe claim.**
  Otherwise a pinned-only workload could never recover.
- **Unchanged:** a model that only comes from `agents.yaml` is not a
  pin, and family failover for it stays.

Verified by:
- pinned healthy with a healthy sibling: the run goes to the pinned
  provider;
- pinned down: refused, and no run starts on the sibling;
- pinned half-open: the call becomes the probe;
- without `model`, the same agent fails over.

**PS-R7: nothing else regresses.**
- The existing `provider_down`, auth cooldown, deferral and pause
  behaviours are unchanged outside what PS-R4 to PS-R6 cover.
- No existing health state is migrated.
- The existing suite stays green, apart from the known reds.

## Out of scope

- **The root cause of opencode's `Unexpected server error`.** H7 contains
  the failure; it does not diagnose it.
- **Reading an opencode model namespace (`opencode-go/…`) as a
  multiagents provider.** Rejected.
