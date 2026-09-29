# H7: provider startup failures, binary resolution, model pins — the contract

**Status:** contract, written by the orchestrator on 2026-09-29.
- **Source:** phase6-hardening.md, item H7, including the user's
  binary-resolution note of 2026-09-29.
- **Research:** ag-aedab3.
  - 25 failed opencode runs were found. 16 of them failed at startup with
    `Unexpected server error`, exit 1 and zero steps.
  - None of them failed because the binary was missing.
  - The code has no resolver that works out the binary once.
  - The circuit breaker is reset by any success and re-admits the
    provider when the cooldown expires.
  - A model override is not a pin.
- **Ids:** `PS-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.

## Behaviours

**PS-R1: resolve the binary once, from a fixed order.**
- A new provider field, `bin_search`, is an optional list of extra
  directories. `~` is expanded.
- `Provider.resolve_bin() -> ResolvedBin{path, via, searched}` is the only
  place that looks a binary up.
  - `path` is absolute, or `None` when the binary was not found.
  - `via` is one of `bin`, `PATH` and `bin_search`.
  - `searched` is the ordered list of places looked at.
- **The order:**
  1. `bin` itself, when it contains a `/`;
  2. the server process's `PATH`;
  3. each `bin_search` directory in turn.

  An entry counts only when it is an executable regular file, or a
  symlink to one.
- **The shipped `opencode` provider** gets `bin_search: ["~/.opencode/bin"]`.
- Verified by:
  - a fake binary found through each of the three routes;
  - an empty `PATH` with the binary only under `bin_search`, which is
    found with `via: "bin_search"`;
  - a non-executable file at the right name, which is skipped.

**PS-R2: every consumer uses that one result.**

Each of the following takes the binary from `resolve_bin()`. None of them
calls `shutil.which` on the provider's `bin` independently:
- native launch;
- the docker mount and container binary derivation, including
  `bin_versions_depth`;
- the auth `check` and `login` scripts;
- the budget and usage scripts;
- `doctor`;
- `refresh-models`.

The details that follow:
- **Scripts.** `MULTIAGENTS_BIN` is always the absolute resolved path.
  - When the binary is not found, the script is **not run**. The action
    reports a `missing` state carrying the PS-R3 message.
  - The shipped scripts use `MULTIAGENTS_BIN` and never search `PATH`
    themselves. The `:-opencode` style fallbacks are removed.
- **`models_cmd`.** Its first element is replaced by the resolved path
  when it equals the provider's `bin` or that name's basename. So
  `["opencode", "models"]` runs the resolved binary.
- Verified by: with `PATH` lacking the binary and `bin_search` pointing at
  a fake, each of the following finds and runs that fake:
  - native launch;
  - `auth check`;
  - `budget`;
  - `doctor`;
  - `refresh-models`.

**PS-R3: a not-found error says where it looked and how to fix it.**
- **The message** names the provider and every place in `searched`, in
  order. It then says how to fix it: set `bin:` to an absolute path, or
  add the directory to `bin_search:` in `providers.yaml`.
- **Where it appears:** it is the error from `start_agent`, from
  `multiagents auth login <provider>`, and from `doctor`.
- **`doctor`** also shows, for a found binary, its path and its `via`.
- Verified by message tests on each of those three surfaces.

**PS-R4: a startup failure is a category of its own.**
- **Definition.** A run *failed at startup* when it ended in failure,
  meaning a non-zero exit or a failed status, and the provider's stream
  produced **no** step, tool, text or usage event. Two kinds of end are
  excluded:
  - a run multiagents itself refused or stopped;
  - a quota or limit end, which is already classified.
- **What is recorded.** The provider's consecutive startup failures are
  counted separately from the existing general health counter.
- **What resets the count.** A run on that provider that produced at
  least one such event. A success on a *different* provider never
  resets it.
- **The threshold.** At `limits.startup_failure_threshold` consecutive
  startup failures (default 2), the provider is marked
  `startup_down`:
  - it gets an event naming the provider, the count and the last run's
    first error line;
  - routing excludes it exactly as it excludes `provider_down`.
- Verified by:
  - a fake CLI that exits 1 with no events twice marks the provider;
  - a fake CLI that emits one event and then fails does not count;
  - an intervening success on another provider does not reset the
    count.

**PS-R5: recovery is demonstrated, not timed.**
- **After the cooldown expires**
  (`limits.provider_down_cooldown_seconds`), a `startup_down` provider is
  *half-open*.
  - Routing may send it at most **one** run at a time: the probe.
  - All other runs keep being routed as if it were down.
- **The outcome of the probe:**
  - **If the probe produces a PS-R4 event**, the mark is cleared and a
    `provider_recovered` event is emitted.
  - **If it fails at startup**, the provider is marked down again for
    another cooldown.
- **Nothing else clears the mark:**
  - not cooldown expiry alone;
  - not a success elsewhere;
  - not an auth check;
  - not a restart of the server.

  The mark lives in the durable state where the cooldowns already live.
- Verified by:
  - with the cooldown expired, two concurrent starts give one probe on
    the provider, and the other start goes elsewhere or defers;
  - a successful probe clears the mark;
  - a failed probe re-marks it;
  - a restarted Runner still sees the mark.

**PS-R6: an explicit `start_agent(model=…)` pins its provider.**
- **Which provider.** A call-level model override is resolved to a
  provider as it is today: the configured primary or fallback whose
  model equals it, or else the primary.
- **What the pin forbids.** The run is pinned to that provider:
  - no family sibling;
  - no fallback;
  - no other account.
- **When the pinned provider is unavailable** (down, `startup_down`,
  out of quota, or not authenticated), `start_agent` **refuses**:
  - It does not defer, and it does not pause the tree.
  - The message names the provider, why it is unavailable, and that
    omitting `model` lets the router choose.
- **Unchanged.** A model that only comes from `agents.yaml` is not a
  pin. Family failover for it is kept.
- Verified by:
  - with the pinned provider healthy and a healthy sibling available,
    the run goes to the pinned provider;
  - with the pinned provider down, the call refuses and no run starts
    on the sibling;
  - without `model`, the same agent still fails over.

**PS-R7: nothing else regresses.**
- The existing `provider_down`, auth cooldown, deferral and pause
  behaviour is unchanged for everything PS-R4 to PS-R6 do not cover.
- The existing suite stays green, apart from the known reds.

## Out of scope

- **The cause of opencode's `Unexpected server error`.** It is upstream
  (the provider's server). H7 makes it contained, not fixed.
- **The model-namespace parsing idea**, reading `opencode-go/` as naming a
  provider. It is rejected: those prefixes are opencode's namespaces, not
  multiagents providers.
