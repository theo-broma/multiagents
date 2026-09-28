# Codex provider — requirements

**Status: requirements, not a contract.** Written by the initializer on
2026-09-28, from a conversation with the user. The interface contract is the
orchestrator's to write (phase 2 of its pipeline), and it should cite the ids
below. `CX-D*` are decisions already taken. `CX-R*` are requirements. `CX-Q*`
are open questions.

## Starting point

The user wrote an integration proposal, which is kept verbatim in
`context/codex-proposal/`. The original is in `codex-plugin/`, which is
untracked and belongs to the user, so leave it alone. Read the proposal's
`README.md` first: it records how the Codex CLI behaves and which constraints
the engine imposes, and most of that still holds.

- The proposal targets **codex-cli 0.157.1**, which is the version installed
  on the host.
- Its adapter is `providers/codex.py`: Python 3.11+, standard library only,
  used both as the provider `bin` and as its action script.
- It was written on 2026-09-27, **before sandbox-git**. Its 21 offline tests
  (`tests/test_codex.py`, with a fake CLI and the real `Provider`) still pass
  on `main` at `b3f3910`.
- Every contract field it uses exists today: `family`, `extends`,
  `home_links`, `container_private_home`, `usage_mode: delta`,
  `models_parse: tsv`, `agent_guidance`, and the `MULTIAGENTS_*` launch
  variables.
- Nothing about it has been run live: no login, no agent, no container.

## Decisions (2026-09-28)

- **CX-D1 — ship it as a default provider.** The initializer's call; the user
  did not object. The block goes in `src/multiagents/defaults/providers.yaml`,
  the adapter in `src/multiagents/defaults/providers/codex.py`, and the tests
  in `tests/`. This keeps it maintained and tested with the other three,
  instead of living as a hand-installed copy. The invariant "providers are
  plugins" still holds: **no provider name in core code**. Where the engine
  has to change, it changes generically.
- **CX-D2 — egress.** The user approved `openai.com` (which covers `api.` and
  `auth.`) and `chatgpt.com`.
  - They are already in the project's `egress_allowlist`, with a comment.
  - Adding them to the shipped `defaults/project.yaml`, next to the other
    model endpoints, follows from CX-D1. Confirm it with the user when the
    contract is reviewed.
  - No other host may be added without asking.
- **CX-D3 — a dedicated profile, never the user's `~/.codex`.**
  - Locally, agents use `CODEX_HOME=~/.multiagents/profiles/codex`, with its
    own login through `multiagents auth login codex`.
  - The proposal's `home_links: [.codex]` is rejected. It exposes the user's
    `auth.json`, `history.jsonl` and every personal session to every agent.
  - In Docker, the private backing is used, as in the proposal.
- **CX-D4 — roles.** The user wants Codex for:
  - `adversary`, `reviewer`, `implementer-quick`, `researcher` and `advisor`;
  - a **fallback** (`models: codex: …`) for `tester`, `implementer` and
    `implementer-deep` when claude is exhausted.

  `orchestrator` and `initializer` stay on claude. Under Codex they would lose
  the R8 context tracking and the driver-led compaction (see CX-R9).
- **CX-D5 — the subscription is ChatGPT Plus.**
  - Raised with the user and accepted: Plus has tight 5 h and weekly windows,
    and CX-D4 puts five roles plus three fallbacks on it.
  - What makes this workable is CX-R2: the router has to see the real quota.
    Without it, an exhausted window stops agents rather than slowing them,
    exactly as happened with opencode.
  - Model pins are chosen from measurements (CX-R11), not guessed from model
    names.

## Requirements

- **CX-R1 — the provider runs agents through `Runner`.**
  - Stream normalisation, resume via `codex exec resume <thread_id>`, token
    deltas with cache reads separated, permission mapping, MCP injection via
    `-c mcp_servers=…` with inherited servers disabled, and
    `features.multi_agent=false`: all as in the proposal unless the contract
    says otherwise.
  - The proposal's offline tests are the starting suite.

- **CX-R2 — a real quota reading (`budget` action).**
  - Codex writes a `rate_limits` block into its session rollout files
    (`<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*.jsonl`, `token_count`
    events). It has a `primary` window (`window_minutes: 300`) and a
    `secondary` one (`window_minutes: 10080`), each with `used_percent` and
    `resets_at` as a Unix timestamp. It was observed on 2026-09-28 on the
    user's account.
  - The action must return `known: true`, one window per bucket, headroom
    taken as the worst of them, and `resets_at`.
  - Freshness: the reading is only as recent as the last session, so follow
    the conventions of `context/specs/quota-freshness.md`. With no session at
    all, return `known: false`.
  - Read the newest reading across the profiles actually in use: the
    dedicated host profile and the Docker private backing.
  - Read metadata only. **Never read or print message bodies.**
  - Also check whether `codex exec --json` itself emits rate limits. If it
    does, a live reading beats the file.
  - The proposal's `known: false` is superseded.

- **CX-R3 — the native binary inside the container.**
  - The engine mounts a provider's `bin`, which here is the adapter. The
    native `codex` that the adapter calls is not mounted.
  - On the host, `~/.local/bin/codex` is a symlink to
    `~/.codex/packages/standalone/releases/<version>-x86_64-unknown-linux-musl/bin/codex`,
    a static musl ELF with no runtime dependencies.
  - The contract must say how the engine learns about, and mounts, a
    provider's second binary. It must be generic, not codex-specific.
  - It must also say what happens when Codex updates itself. P0-R1's
    versioned-mount detection treats a `…/<version>/bin/<same name>` layout as
    out of scope, so the mount would point at a stale path after an update.
  - The mount must not expose the rest of the host's `~/.codex` (CX-D3).

- **CX-R4 — permissions inside Docker, settled by a live test.**
  - Codex maps `sandbox` to `workspace-write` and `readonly` to `read-only`,
    both through its own Linux sandbox. Whether that sandbox can initialise
    inside our container is unknown.
  - Test it live. If it cannot, map every profile to `danger-full-access`
    **inside Docker only**, as claude already does with `bypassPermissions`:
    the container is the boundary.
  - If `read-only` does work, keep it. It would be the first `readonly` in
    this project that is a real boundary.
  - Record the outcome in this file.

- **CX-R5 — compatibility with sandbox-git.**
  - In the container, the project root and `.git` are read-only, and
    multiagents does the commits (`context/specs/sandbox-git.md`).
  - Run a live smoke test like the sandbox-git one: a codex agent edits a
    file, the commit lands, and writes to the root, `.git/config` and `.git/`
    are refused.

- **CX-R6 — authentication.**
  - Device login into the dedicated profile.
  - `check` never prints the CLI's output.
  - Docker uses the private backing.
  - **Token refresh through the proxy must be verified live.** agy's refresh
    failed through the proxy while `auth_status` still said "authenticated"
    (BRIEF, "Awaiting the user", 2026-09-23). Do not repeat that blind spot:
    a refresh failure must surface as not authenticated.

- **CX-R7 — models.**
  - List them from the dedicated profile's `models_cache.json`, keeping
    `visibility: list` entries only.
  - The proposal found that `refresh_models()` does not apply `provider.env`
    to `models_cmd`. Fix that generically in the engine rather than working
    around it with a static list.

- **CX-R8 — cost.**
  - The stream reports none.
  - The monitor must not show `$0` as if Codex were free, because it is paid
    through the plan.

- **CX-R9 — no interactive launch in this phase.**
  - The `launch` action returns 64 (not implemented).
  - Reason: the driver's context tracking, auto-compaction and MCP cost
    attribution all expect transcripts Codex does not write, as the proposal
    itself documents.
  - The proposal's `launch` code stays in `context/codex-proposal/` for a
    later phase.

- **CX-R10 — `advisor` works through `consult()`.**
  - The advisor keeps its context across calls through resume.
  - Verify live that consecutive `consult()` calls on a codex advisor land in
    the same thread.

- **CX-R11 — measure before pinning models.**
  - Once the provider works, run the same small, fixed task once on each
    candidate model, and record how much of the 5 h window each run used
    (CX-R2 provides the reading).
  - The user then pins models per role (CX-Q1).
  - Keep it to one run per model. On Plus the measurement itself costs quota.

- **CX-R12 — the roster tests still hold.**
  - The test that guards family separation (tester vs. implementer-deep, and
    the adversary on a different family) must still pass with Codex in the
    roster and in fallbacks.
  - The opencode-tier test is not affected.

## Open questions

- **CX-Q1 — which model for which role.**
  - Visible on the account on 2026-09-28: `gpt-6-astra`, `gpt-6-sol`,
    `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` and
    `gpt-5.5`.
  - Nobody has measured their relative cost or strength on this plan. Answer
    after CX-R11.
  - DEFAULT: one model for every codex role, the one that uses the least of
    the 5 h window on the probe task with an acceptable result.
- **CX-Q2 — the shipped default egress list (CX-D2).**
  - DEFAULT: add `openai.com` and `chatgpt.com` to group 1 (model endpoints)
    of `defaults/project.yaml`.

## Pipeline

- It touches authentication, the sandbox and an untrusted stream, so it gets
  the **full pipeline with an adversary**: tester, then implementer-deep,
  then adversary, then reviewer.
- Live checks run on the host and in the container, **only with no agent
  running**. A container recreate kills everything inside it.
- Chicken and egg: until Codex works, the adversary and reviewer roles have
  no provider. opencode is broken, and agy is at 98.6 % of its weekly
  quota until 2026-09-30 05:11 UTC and excluded by the user. So:
  - the first adversary pass runs on agy after its reset, if the user lifts
    the exclusion, or on claude as a stand-in, as was done for sandbox-git;
  - log the choice in `context/advisor-catchup.md`.

---

# Interface contract (orchestrator, 2026-09-28)

Ids `CX-C*`. They cite the requirements above. Never renumber: retire an id
with `CX-Cn — withdrawn: <why>`. Research behind it: ag-da174a (mounts, bin,
profiles, auth) and ag-fff50e (budget, cost, permissions, roster, consult).
The proposal's 21 tests pass on `main` (ag-fff50e).

**Two halves, one contract.** The engine half (CX-C1 to CX-C6) is generic and
names no provider. The provider half (CX-C7 to CX-C14) lives entirely in
`src/multiagents/defaults/providers/codex.py` and the `codex:` block of
`src/multiagents/defaults/providers.yaml`. After this work, `rg -n codex
src/multiagents/*.py src/multiagents/executor/` returns nothing.

## Engine (generic)

- **CX-C1 — `adapter:`, a provider field.** (CX-R1, CX-R3)
  - Optional. It names an executable resolved like action scripts
    (project → global → defaults, `scripts.find_script`).
  - When set, an agent run execs the adapter as argv[0], with the arguments
    `build_command` renders from `spawn:`. `bin` keeps its meaning, which is
    the provider's native CLI. `available()` and the mounts use `bin`, and
    the adapter drives it.
  - When `adapter:` is set and `script:` is absent, the adapter is also the
    action script.
  - Absent `adapter:` means today's behaviour, byte for byte.
  - In Docker, the adapter runs at the same path it has on the host. The
    executor makes it visible read-only if it is not already.
  - Verified by: unit tests on `Provider` parsing and `build_command`; a
    runner test with a fake adapter and a fake native CLI; a docker-mount
    test that the adapter path is in the mount list.

- **CX-C2 — the adapter knows its native binary and executor.** (CX-R3, CX-R4)
  - Every agent run of a provider with `adapter:` gets two variables, the
    same ones action scripts already get from `scripts.build_env`:
    - `MULTIAGENTS_BIN`: the absolute path of `bin`, resolved **at this
      exec**, not at container creation;
    - `MULTIAGENTS_EXECUTOR`: `local` or `docker`.
  - In Docker, `MULTIAGENTS_BIN` is a path that exists inside the container.
  - Verified by: a runner test (local), a docker argv/env unit test, and the
    live check L4.

- **CX-C3 — `bin_versions_depth:`, a versioned native binary.** (CX-R3)
  - Optional integer N ≥ 1. It says the resolved target of `bin` sits N
    directories below the directory that holds every installed version. For
    codex, the target is
    `…/releases/<version>-<triple>/bin/codex`, so N = 3 and the root is
    `…/releases`.
  - When set, the docker executor mounts that root read-only, instead of
    only the resolved file. It mounts nothing else above it. For codex that
    means the rest of the host's `~/.codex` is never mounted (CX-D3).
  - At each exec, the launcher is re-resolved on the host. If the new target
    lies under the mounted root, `MULTIAGENTS_BIN` names it, so a self-update
    needs no container recreate. If it does not, the run fails with a
    message that names the key and says `multiagents docker rm && multiagents
    docker up`, the same refusal style as P0-R1.
  - Absent key: today's P0-R1 behaviour, unchanged, and its tests stay green.
  - Verified by: docker mount unit tests using a tmp tree that mimics the
    layout (root mounted and nothing above it; a new version picked up; a
    target outside the root refused); live check L4.

- **CX-C4 — `models_cmd` runs with the provider's environment.** (CX-R7)
  - `refresh_models()` runs `models_cmd` with the same environment
    `scripts.build_env` gives actions, including `provider.env` with `~` and
    `$VAR` expanded.
  - A `models_cmd` whose first element is the provider's adapter or script
    name resolves it the same way as CX-C1.
  - Verified by: a unit test with a fake `models_cmd` that echoes an env var
    set in `provider.env`.

- **CX-C5 — a cost the provider does not report is not shown as `$0`.**
  (CX-R8)
  - A run with tokens > 0 and no reported cost (`cost_usd` absent or 0) is
    rendered as `plan`, not `$0.00`, in `agent_tree`'s rendered text, the
    monitor and `budget_status.by_model`.
  - Totals say what they exclude: `…, $X (+ plan-billed runs)`.
  - A run with zero tokens and no cost renders as today.
  - This is generic, and fixes agy's `$0.0` too.
  - Verified by: rendering unit tests on the tree and the monitor, and a
    `budget_status` shape test.

- **CX-C6 — the roster invariants hold with a fourth family.** (CX-R12)
  - Adding `family: codex` and `models: codex:` fallbacks keeps these green:
    `test_the_coding_tiers_get_their_own_advisor_on_another_family`,
    `test_every_working_agent_names_a_cross_provider_fallback`,
    `test_a_checking_pair_never_collapses_onto_one_model` and
    `test_every_opencode_pin_is_on_the_sixty_dollar_tier`.
  - The roster change itself waits for CX-R11 (the user pins the models).
    This id only requires that the tests accept a codex family. If one of
    them hardcodes the list of families, it is generalised.
  - Verified by: those tests, run against a roster fixture that includes
    codex fallbacks.

## Provider (codex.py and its block)

- **CX-C7 — layout.** (CX-D1)
  - The adapter is `src/multiagents/defaults/providers/codex.py`: Python
    3.11+, standard library only, executable, starting from the proposal's
    `codex.py`.
  - The block is in `defaults/providers.yaml`: `bin: codex`,
    `adapter: codex.py`, `family: codex`, `bin_versions_depth: 3`,
    `usage_mode: delta`, `models_parse: tsv`, `agent_guidance`, and a
    `notes:` for humans.
  - There is **no `home_links`**, and no `transcript:`.
  - The proposal's tests move to `tests/test_codex_provider.py`, adapted to
    the shipped paths. They stay offline, using a fake CLI.

- **CX-C8 — a dedicated profile.** (CX-D3, CX-R6)
  - On the host, every adapter invocation runs with
    `CODEX_HOME=~/.multiagents/profiles/codex`, set through `env:` and
    expanded by the engine. The adapter creates that directory (mode 0700)
    if it is missing.
  - In Docker, `CODEX_HOME` points at the private backing, via
    `container_private_home` or an equivalent. The host profile directory is
    **not visible** inside the container.
  - Nothing reads or writes the user's `~/.codex`, apart from the read-only
    versions root of CX-C3.
  - Verified by:
    - adapter unit tests (the env the native CLI gets, and the directory
      created);
    - a docker mount test (no mount of `~/.codex` or of the host profile);
    - the live check L1.

- **CX-C9 — auth actions.** (CX-R6)
  - **`check`** runs `codex login status` under `CODEX_HOME`, with a 15 s
    timeout. It exits 0 when logged in, 10 when not, and 20 on error. It
    never prints the CLI's output or any credential.
  - **`login`** runs device login into the profile the executor implies:
    - the private backing when `MULTIAGENTS_EXECUTOR=docker` and
      `MULTIAGENTS_PROFILE` is not `host`;
    - the host profile otherwise.
  - **A refresh failure is not "logged in".** When the stored token has
    expired and cannot be refreshed, both of these report not authenticated:
    `check` exits 10, and an agent run's result carries the engine's
    unauthenticated status, not a generic failure.
  - Verified by: unit tests with a fake CLI (status exit codes; an expired
    token scenario in which the fake refresh fails); live check L2.

- **CX-C10 — agent runs.** (CX-R1)
  - Behaviour is as the proposal documents and its tests assert:
    - `codex exec --json`, with the prompt on stdin and an explicit cwd;
    - resume via `codex exec resume <thread_id>`, where the session id is the
      `thread.started` id;
    - token deltas per `turn.completed`, with cached input separated and not
      double-counted;
    - one tool event per call id;
    - MCP injected with `-c mcp_servers.*`, inherited servers disabled, and
      `features.multi_agent=false`;
    - `--ignore-user-config`;
    - quota and auth failure text echoed on stderr for the engine's
      detectors.
  - **Permissions.** Under `MULTIAGENTS_EXECUTOR=local`: `readonly` →
    `read-only`, `sandbox` → `workspace-write`, `full` →
    `danger-full-access`. Under `docker`, the mapping is whatever live check
    L3 establishes, recorded in this file:
    - if Codex's own sandbox initialises in our container, keep the local
      mapping;
    - otherwise, every profile maps to `danger-full-access`.
    - Until L3 has run, docker maps to `danger-full-access`.
  - Verified by: the moved proposal tests, plus a permission test for each
    executor.

- **CX-C11 — `budget` action.** (CX-R2)
  - **Where it reads.** The newest `token_count` event carrying `rate_limits`
    across the rollout files of every profile in use:
    - `$CODEX_HOME/sessions/**/rollout-*.jsonl` on the host;
    - the same under the docker private backing
      (`$MULTIAGENTS_PRIVATE_BACKING`) when one exists.
    - Only the newest files are examined, and each from its tail.
  - **Output**, the shape `budget._from_script` parses:
    - `known: true`, `source: "rollout"`;
    - `windows: {"5h": {percent, resets_at}, "weekly": {percent,
      resets_at}}`, mapped from `primary` (300 min) and `secondary`
      (10080 min) by `window_minutes`, not by position;
    - `headroom = 1 - max(percent)/100`;
    - `resets_at` of the worst window, in ISO 8601 UTC;
    - `stale_seconds`, the age of the event.
  - **Freshness.** It follows `quota-freshness.md`. A window whose
    `resets_at` + 120 s has passed counts as 0 % used (QF-R1). When no event
    exists, it returns `known: false` with a note.
  - **Bounds.** It finishes in under 5 s on a profile holding 1000 rollout
    files of 5 MB each. It never loads a whole file into memory. It never
    emits or logs any field other than `rate_limits`, timestamps and window
    metadata: no message bodies, prompts or paths inside the user's home in
    `note`.
  - **Malformed input is not a crash.** Truncated lines, non-UTF-8 bytes,
    missing keys, `used_percent` as a string or out of 0..100, or a
    `resets_at` that is not a number: the event is skipped, and at worst the
    result is `known: false`.
  - **Optional, live-determined (L5).** If `codex exec --json` itself carries
    rate limits, the adapter writes the latest reading to
    `$CODEX_HOME/multiagents-rate-limits.json` (atomic replace, 0600), and
    `budget` uses whichever reading is newer.
  - Verified by: adapter unit tests on fixture rollout trees (fresh, stale,
    expired window, no sessions, malformed lines, a size-bound test);
    live check L5.

- **CX-C12 — `models` action.** (CX-R7)
  - It reads `$CODEX_HOME/models_cache.json`, keeps `visibility: list`
    entries only, and prints TSV `id<TAB>label`.
  - If the cache is missing or unreadable, it exits non-zero with a
    one-line reason and no traceback.
  - Verified by: unit tests with fixture caches.

- **CX-C13 — `launch` and `compact` exit 64.** (CX-R9)
  - Both say "not implemented for codex in this phase" on stderr. The
    proposal's launch code is not shipped.
  - Verified by: unit tests.

- **CX-C14 — egress in the shipped defaults.** (CX-D2, CX-Q2)
  - `openai.com` and `chatgpt.com` join group 1 (model endpoints) of
    `src/multiagents/defaults/project.yaml`, each with a one-line comment.
    Nothing else is added.
  - Verified by: a config test that both are present and that no wildcard
    or bare suffix was added.

## Live checks (after the offline work merges; no agent running)

These need the user once: `multiagents auth login codex`, a device login into
the dedicated profile, and the same for docker if it differs. Record each
outcome in this file under "Live results".

- **L1** — The dedicated profile is used. After a run, `~/.codex/sessions`
  has no new file and the profile does. In Docker, the backing has one.
- **L2** — Refresh through the proxy (CX-R6). A docker run after the access
  token's expiry either refreshes successfully, or reports unauthenticated.
  It never reports "authenticated" and then fails.
- **L3** — Codex's sandbox inside our container (CX-R4). Run `read-only` and
  `workspace-write` on a trivial task; record whether each initialises, and
  set CX-C10's docker mapping from the result.
- **L4** — The native binary and its update (CX-R3). The container runs codex
  via `MULTIAGENTS_BIN`. Simulate an update by adding a new release dir and
  repointing the symlink: the next run uses it without a recreate.
- **L5** — Quota. `budget_status` shows codex `known: true` after one run.
  Record whether `exec --json` carries rate limits.
- **L6** — sandbox-git (CX-R5). A codex agent edits a file, the commit lands,
  and writes to the root, `.git/config` and `.git/` are refused.
- **L7** — consult (CX-R10). Two consecutive `consult()` calls on a codex
  advisor land in the same thread.
- **L8** — CX-R11, the measurement: one fixed probe task per candidate model,
  recording the 5 h window used. Then the user pins the models (CX-Q1), and
  the roster change goes to the user (CX-D4).

## Amendments after the contract review (2026-09-28, ag-5b326a)

A claude opus stand-in reviewed the contract, because the advisor is on agy
and agy is excluded. Its findings were checked against the code. Where an
amendment conflicts with the text above, **the amendment wins**.

- **CX-C8, revised: the adapter chooses the profile, not `env:`.** `env:` is
  expanded on the host and applied under every executor. So
  `CODEX_HOME=~/.multiagents/profiles/codex` would reach the container as a
  host path that nothing mounts there.
  - The shipped block sets **no** `CODEX_HOME` in `env:`. It declares
    `container_private_home: [.codex]`, which mounts the private backing at
    `$HOME/.codex` inside the container and masks the user's own.
  - The adapter sets `CODEX_HOME` for the native CLI:
    - under `MULTIAGENTS_EXECUTOR=docker`, to `$HOME/.codex`, which is the
      backing;
    - otherwise, to `$MULTIAGENTS_CODEX_PROFILE` if set (so an `extends:`
      second account can override it through `env:`), else
      `~/.multiagents/profiles/codex`.
  - `MULTIAGENTS_CODEX_PROFILE` is provider vocabulary. It lives in
    `providers.yaml` and in the adapter, never in core.
  - A `CODEX_HOME` already present in the environment is ignored and
    overwritten, so the user's own shell setting cannot leak in.
  - **The collision with CX-C3 to watch in L4.** The versions root lives
    under the host's `~/.codex/packages/…`, and that path is masked by the
    backing inside the container. The versions-root mount therefore nests
    inside the backing mount. Docker creates the empty mount-point
    directories in the backing on the host, which is acceptable. The mount
    order must be the backing first, then the versions root. A docker
    mount-list test pins that order.

- **CX-C2, revised: the executor is the layer that resolves the binary.**
  - `MULTIAGENTS_BIN` is set by the executor inside `start()` (local and
    docker) and inside `_start_inside()`. The runner does not set it.
  - The existing `_versioned_argv` matches on `provider.bin == argv[0]`. It
    must keep working for providers without `adapter:`. With `adapter:`, the
    resolved `bin` goes into `MULTIAGENTS_BIN`, and argv[0] (the adapter) is
    left alone.
  - Agent runs also get `MULTIAGENTS_PRIVATE_HOME`, as actions do, whenever
    the executor has one.
  - **Spawns from inside the container (depth ≥ 2).** A spawn started from
    inside the container cannot see the host's launcher symlink. Its
    `MULTIAGENTS_BIN` is the launcher path as mounted, which is the version
    current when the container was created. It works, but it may be stale.
    This is a documented limitation, not a failure.

- **CX-C3, pinned.** The versions root is `resolved.parents[N-1]`, where
  `resolved` is the fully resolved target of `bin`. N = 1 is the target's
  own directory. Tests cover N = 1 and N = 3. `_versions_dir` is the hook to
  extend. The codex layout example belongs in the block's comment, not in
  code or in test names.

- **CX-C4, replaced.** Instead of running `models_cmd` under `build_env`:
  - a provider with **no `models_cmd`**, whose action script (or adapter)
    implements `models`, has `refresh_models()` run that action through
    `scripts.run_action`, which applies env and resolution;
  - exit 64 means "not implemented", and the provider is then skipped
    quietly;
  - a provider that still declares `models_cmd` behaves as today.
  - The codex block declares no `models_cmd`.
  - Any signature change to `refresh_models` is the implementer's.

- **CX-C5, replaced: `billing:`, a provider field.**
  - It takes `metered` (the default) or `plan`.
  - For a `plan` provider:
    - the monitor (`tui.py`) shows `plan` wherever it would show a dollar
      figure for that provider's runs;
    - `multiagents usage` labels it the same way;
    - each `budget_status.by_model` entry gains `"billing": "plan"`, and
      `cost_usd` stays a number;
    - the rendered tree keeps hiding a zero cost, as it does today.
  - Shipped: `codex` and `agy` are `billing: plan`, a change to YAML only.
  - Verified by: monitor rendering tests and `by_model` shape tests.

- **CX-C15 (new) — `stale_seconds` from a budget script is kept.**
  `budget._from_script` parses an optional numeric `stale_seconds` into
  `Budget`, and `budget_status` reports it. It follows the same convention
  as claude's `stale_seconds`. A malformed value is ignored.
  - Verified by: a `_from_script` unit test.

- **CX-C9, amended.**
  - **`check` follows the same profile rule as `login`**, the executor-implied
    one. So `auth_status` reports on the profile that agents actually use.
  - **The exact stderr line** for an auth failure in a run is
    `codex: not authenticated — run: multiagents auth login codex`. It
    matches the existing `_AUTH_MARKERS` ("not authenticated",
    "auth login"), so no marker is added.

- **CX-C10, amended.**
  - **Docker permissions.** The offline tests pin the interim mapping,
    everything to `danger-full-access`. That test is changed deliberately
    after L3, by `tester`.
  - **Self-update off.** Every invocation of the native CLI passes the
    option that disables its update check or self-update. The exact `-c`
    key is confirmed from `codex --help` or the config reference during
    implementation, and recorded here. A test asserts the flag is present.
  - **A resume whose thread is gone fails loudly.** It does not silently
    start fresh. The result is an error whose text starts
    `codex: resume failed:`, so an advisor never loses its context without
    anyone knowing.
  - **A run killed before `thread.started`** has no session id. Its retry
    is an ordinary fresh run, which is existing engine behaviour and needs
    nothing new.

- **CX-C11, made testable.**
  - It examines at most **8** rollout files per profile, the newest by
    mtime, and reads at most **1 MiB** from the tail of each.
  - If none of them carries `rate_limits`, it returns `known: false`.
    Tests assert the file count and byte count through a counting fixture;
    there is no time-based assertion.
  - "Newer", between a rollout event and the optional L5 file, means the
    **event timestamp**, never the file mtime.
  - `multiagents refresh-quota` for codex runs the `budget` action and never
    a model call.
  - **Extra read-only homes (the user decides the default).** The adapter
    reads, metadata only, the rollout files of each directory listed in
    `MULTIAGENTS_CODEX_QUOTA_HOMES` (colon-separated) in addition to the
    profiles in use. This exists because the 5 h and weekly windows are
    **per account**: the user's own Codex use in `~/.codex` spends the same
    quota, and without it the reading runs low. Whether the shipped block
    sets it to `~/.codex` is CX-Q3.

- **CX-C6, amended.**
  - A roster fallback that names a provider with `enabled: false` is
    skipped by routing, not crashed on. A test asserts it.
  - Disabling codex is `enabled: false` in the project's `providers.yaml`.
  - Removing it entirely also means deleting `~/.multiagents/profiles/codex`
    and the docker backing. This goes in the block's `notes:`.

- **Migration: nothing to migrate.** The proposal was never installed.
  There is no `codex` entry in the project or global `providers.yaml`, and
  no `codex.py` under `.multiagents/config/providers/` or
  `~/.config/multiagents/providers/` (checked on 2026-09-28). Note, though,
  that the global directory holds copies of `agy.sh`, `claude.sh` and
  `opencode.sh`, and those shadow the shipped defaults. That is an existing
  hazard, and it is out of scope here.

- **L2, extended.** Also run **two concurrent codex agents** across a token
  expiry. If one of them fails on a rotated refresh token, the provider must
  not sit in `needs_login` while the profile is actually logged in. Decide
  the fix from what is observed.

- **CX-Q3 (new, the user's decision).** Should the budget action also read
  rate-limit metadata from the user's own `~/.codex/sessions`? It would read
  only the `rate_limits` block, on the host only, and never mount anything
  in agents. DEFAULT, pending the answer: unset, so it reads only the
  dedicated profile and the backing.
