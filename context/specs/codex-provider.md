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

## Decisions on the provider tester's questions (2026-09-28, ag-155ed5, merged 115b54d)

1. **How a failed refresh is worded.** Unknown until the live check L2. The
   fake uses Codex's "could not be refreshed … sign in again" text. If the
   real wording differs, the adapter's matching changes to follow it, and
   so does the fake. The contract does not change.
2. **A successful `exec resume`** announces the same thread id again. A
   thread that no longer exists produces `codex: resume failed:` in both
   cases: when the CLI returns an error, and when it silently starts a new
   thread (a different id).
3. **A models-cache entry with no `visibility`** is **not** listed. Only
   an explicit `visibility: list` counts.
4. **`check` and `login` under docker** act on
   `$MULTIAGENTS_PRIVATE_BACKING`, the host path of the backing, unless
   `MULTIAGENTS_PROFILE=host`. Docker runs see that same directory as
   `$HOME/.codex`. Consistent, as the tests assume.
5. **`resets_at` in the output** is ISO 8601 UTC, converted from the
   rollout's Unix timestamp.
6. **A non-zero `login status` with output the adapter does not recognise**
   exits 20 (unknown), never 10. Only a recognised "not logged in" or
   "refresh failed" gives 10.
7. **The device-login flag** is taken to be `--device-auth`, to confirm
   from `codex login --help` during implementation.
8. **A failed `mcp list`** still refuses the run, as in the proposal. Its
   stderr line starts `codex:`.
- **The update-check option** is asserted through one constant,
  `UPDATE_CHECK_OFF` in `tests/support/codex_harness.py`, currently
  `check_for_update_on_startup=false`. The implementer confirms the real key
  from the installed CLI. If it differs, `tester` changes that one line.
- **Confirmed on the host, 2026-09-28:**
  - The installed CLI is **0.158.0**. It updated itself from 0.157.1, which
    shows CX-R3 matters.
  - `codex login --device-auth` exists.
  - `check_for_update_on_startup` is a real config key; the binary contains
    it 18 times. `UPDATE_CHECK_OFF` stands as written.
  - The layout is `…/releases/0.158.0-x86_64-unknown-linux-musl/bin/codex`,
    so N = 3.

## Decisions on the engine tester's questions (2026-09-28, ag-4aa932, merged 425cafe)

The engine tests total 52: 29 red and 23 guards.

- **A negative `stale_seconds`** may be ignored or clamped to 0. Either is
  acceptable, and the tests accept both.
- **"Skipped quietly"** means no line for the provider in the
  `refresh-models` output, and no entry for it in `models.yaml`.
- **An invalid `billing:` value** is treated as `metered`. It is not
  tested.
- **The CX-C5 totals wording** (`+ plan-billed runs`) is **withdrawn**. It
  is not tested, and the per-row `plan` label is enough.
- **An adapter run is recognised by an absolute adapter path in argv[0]**:
  accepted.
- **Real defects the tester found under CX-C6**, to be fixed by the engine
  implementer:
  1. **Routing:** a provider with `enabled: false` in the same family was
     routed to and actually ran. `pick_instance` treats "no budget" as
     "room", and `read_all` omits disabled providers.
  2. **The roster test only simulates agy and opencode being down.**
     `test_a_checking_pair_never_collapses_onto_one_model` does not cover a
     fourth family. The new test covers it through a fixture roster, and
     `test_core.py` is not changed.

## CX-C11 revised: a live quota reading via the app-server (2026-09-28)

**Source.** The binary was inspected at the user's request, and the
app-server protocol generated offline with `codex app-server
generate-json-schema`. `/status` reads the quota **live**:
- Codex calls `GET https://chatgpt.com/backend-api/wham/usage` with the
  profile's ChatGPT token. `chatgpt.com` is already approved.
- It exposes the result through its app-server's JSON-RPC method
  **`account/rateLimits/read`**. There is also an `account/rateLimits/updated`
  notification.
- The response, `GetAccountRateLimitsResponse`, carries:
  - `rateLimits`, a single-bucket view;
  - `rateLimitsByLimitId`, keyed by `limit_id` (for example `codex`; there
    may be one bucket per model family).
- Each bucket is a `RateLimitSnapshot`: `primary` and `secondary`, each a
  `RateLimitWindow` with `usedPercent`, `windowDurationMins` and
  `resetsAt` (Unix seconds). It also carries `credits`, `planType` and
  `rateLimitReachedType`.
- It costs no model call, and so no quota.

**Why it replaces the rollout reading as the primary source:**
- **It is live.** The rollout reading is only as recent as the last session.
- **It is per account,** so the user's own Codex use is included
  automatically. **CX-Q3 is thereby withdrawn**, and
  `MULTIAGENTS_CODEX_QUOTA_HOMES` is kept only as an optional fallback
  input.
- **It uses Codex's own auth and refresh.** We never reimplement a private
  HTTP call or handle the token ourselves.

**The contract, which supersedes CX-C11's "where it reads":**
- **Primary source.** `budget` starts `codex app-server` (stdio) under the
  profile the executor implies (the CX-C9 rule), sends `initialize`, then
  `account/rateLimits/read`, and shuts it down.
  - The whole exchange is bounded to **7 s**, inside the engine's 10 s action
    timeout. On timeout the process group is killed.
  - Output: `source: "app-server"` and `stale_seconds: 0`.
  - Windows are named by `windowDurationMins`: 300 → `5h`, 10080 →
    `weekly`, anything else → `<n>m`.
  - With several `rateLimitsByLimitId` buckets, each window is prefixed with
    its `limitId` (`codex-5h`, …). Headroom is the worst across all of
    them.
  - `rateLimitReachedType` non-null means headroom 0.
- **Fallback, only if the app-server fails.** On a non-zero exit, a timeout,
  a JSON-RPC error, not being logged in, or an unparseable response, the
  rollout reading applies as specified above, with its bounds, and its
  `source: "rollout"`.
  - The `note` says why the live read failed, in one line, with no
    credentials and no response bodies.
- **Nothing else from the response is emitted.** No `accountId`, no
  `credits.balance`, no upsell text.
- Tests use a fake `codex` that speaks the JSON-RPC exchange over stdio.
  They cover success (single and multi-bucket), a hang (killed within the
  bound), an error, garbage output, and the fallback to rollout.
- **Live check L5 changes accordingly:** it confirms that
  `account/rateLimits/read` works from the dedicated profile, on the host and
  in the container through the proxy, and records the `limitId` buckets
  observed. Those feed CX-Q1, since a model may have its own bucket.

### Decisions on the app-server tester's questions (ag-fc872d, 37 tests `test_cx_c11r_*`, merged fa91940)

- **When `rateLimitsByLimitId` is present and non-empty,** every window is
  prefixed with its `limitId`, even when there is a single bucket. The
  unprefixed `rateLimits` view is then not also emitted. Only when
  `rateLimitsByLimitId` is absent or empty does `rateLimits` give
  unprefixed names.
- **Null fields.**
  - A window with a null `usedPercent` is skipped.
  - A null `windowDurationMins` gives the name `window`, prefixed when the
    bucket has one.
  - A null `resetsAt` means that window has no `resets_at`.
- **A valid response followed by a non-zero exit is used.** The exit status
  of a process we shut down ourselves is noise.
- **A live `usedPercent` out of 0..100, or not a number,** means that window
  is skipped. This is the same rule as the rollout reading.
- **7 s is the target.** The tests assert the observable bound, under
  9.5 s.
- **How "not logged in" is signalled** is to be confirmed at L5.

## Engine review of be92256 (ag-6f199c, a claude opus stand-in for the reviewer, 2026-09-28)

Verdict: approve with fixes. The engine half is merged. The fixes below are
new ids.

- **CX-C16 — the provider is named, not guessed.** The executor must learn
  which provider an agent run belongs to from the runner, by an explicit
  parameter or an env entry, and never from the file name in argv[0].
  - An `extends:` instance (`acme-2`, inheriting `adapter:`) gets its own
    `MULTIAGENTS_BIN` and private home, not those of its parent.
  - A provider without `adapter:` whose `bin` shares a file name with some
    adapter gets no `MULTIAGENTS_*` adapter variables.
  - The argv[0] match may stay only as a fallback. When two providers match,
    it is an error.
  - Verified by: executor unit tests with two instances sharing an adapter,
    and with a colliding file name.
- **CX-C17 — `bin_versions_depth` cannot widen the mount.** A computed root
  is refused as a config error naming the key, reported through
  `container_state`, and nothing is mounted, when it is:
  - `/`;
  - the user's home;
  - an ancestor of either;
  - a system prefix (`/usr`, `/bin`, `/lib*`, `/etc`, `/opt`, `/var`,
    `/nix`, `/snap`).
  - The same applies when the depth is larger than the path allows.
  - Verified by: unit tests with depth 7 on the codex-like layout (which
    reaches the home) and a `/usr/bin` layout.
- **CX-C18 — `mount_cli_from_host: false` is honoured.** When the flag is
  off:
  - `MULTIAGENTS_BIN` is the bare `bin` name, resolved inside the container
    by PATH;
  - no versions root is mounted;
  - no CX-C3 refusal runs.
  - Verified by: unit tests with the flag off.
- **CX-C19 — the stale-root refusal is worded neutrally.** It says "this
  container lacks the versions root <X>" and gives the recreate command.
  This covers both a moved target and a key added after the container was
  created.
  - Verified by: a unit test on the message.
- **CX-C20 — disabled providers add no mounts.** `enabled: false` providers
  contribute neither a versions root nor an adapter mount.
  - Verified by: a unit test.
- **Documented, not changed:**
  - **An adapter mounted as a single file** stays pinned to the old copy
    after an atomic replace on the host, until the container is recreated.
    This goes in the providers README.
  - **CX-C4 changes `refresh-models`** for a shipped provider with no
    `models_cmd` but an action script (claude). The spec allows it, but it
    is not byte-for-byte. The "no models_cmd" line disappears when the
    script exits 64. Accepted, and noted in the README.
  - **`load_providers` runs on every monitor snapshot.** A minor cost,
    deferred.

### Decisions on the CX-C16..C20 tester's questions (ag-96b5b0, merged 364904d)

- **CX-C17 and `container_state`.** No new return shape is required. A
  refusal on the start path, whose message names `bin_versions_depth`, is
  enough.
- **CX-C17, which system prefixes.** A root is refused when it **is** one of
  the listed prefixes, or an ancestor of one. A root strictly below a prefix
  (`/usr/lib/node_modules/x`, `/opt/tool/releases`) is allowed.
- **CX-C16, the ambiguous fallback.** Any refusal will do, or a start
  without adapter variables. No specific exception type is required.
- **CX-C18 and argv.** With the flag off, argv[0] of a provider without an
  adapter also stays the bare `bin` name. The P0-R1 host-path rewrite does
  not apply. With the flag off, the CX-C17 refusal does not run either,
  since there is no versions root at all. Neither is tested yet; the
  implementer adds no test, but implements both.
- **test_core, merged a2acd54.** The checking-pair invariant now takes down
  every family in the roster. An agent with **no** fallback is exempt when
  its own provider is down. That follows the standing decision (fc01d27):
  `implementer` and `implementer-deep` wait for claude rather than switch.
  Accepted.

### CX-C16..C20 implemented (ag-f0d8d7)

- Result: 25 of the 29 review tests pass.
- **The 4 that fail, `test_cx_c16_*[acme-2-*]`, are a test defect.** `acme`
  and `acme-2` share a family, so routing (`choose_provider`, which breaks
  ties by name) sends the agent to `acme`. The executor is then correctly
  told `provider="acme"`.
  - **Fix, for the tester:** make `acme-2` the instance the router picks,
    for instance by making `acme` unavailable or loaded, or assert against
    the node's recorded provider.
- `/sbin` was added to the refused system prefixes. Accepted.

## The attack on the adapter (ag-602b46, a tester standing in for the adversary; merged as tests/test_codex_provider_edges.py)

The results were 14 findings, which are red tests, and 78 probes that held.
Ranked:

1. **security**: a resume session id that starts with `-` reaches the native
   argv as a flag, for example `--dangerously-bypass-approvals-and-sandbox`.
   A session id must be validated (UUID-shaped) or placed after `--`. Refuse
   anything else.
2. **security**: a DEL (`\x7f`) or another control character in MCP env
   values, args or names, or in effort, produces invalid TOML. The whole
   `-c` value then falls back to a string, which means our server is not
   configured and inherited servers are not disabled. Every control
   character must be TOML-escaped.
3. **data**: malformed `turn.completed` usage (null, string, list, `1e999`)
   and `turn.failed` with a string error drop the result event, lose the
   tokens, or crash. Also `OverflowError` is not caught, and `_iso` is
   unprotected.
4. **data**: a live or rollout `resetsAt` in milliseconds or out of range,
   or `window_minutes: 1e999`, crashes `budget`. The event must be skipped,
   and the live read must fall back.
5. **data**: a live `usedPercent` outside 0..100 is emitted. It must be
   skipped, as already decided.
6. **data**: `sessions/**/*.jsonl` matches non-rollout files. Only
   `rollout-*.jsonl` counts.
7. **data**: a models entry without `visibility` is listed, contrary to
   decision 3. A slug containing a tab or newline forges a TSV row.
8. **annoyance**: a malformed `mcp list` gives an AttributeError traceback,
   and the stderr prefix is `Codex adapter:` instead of `codex:`.

Decisions on its questions:
- **A profile (`MULTIAGENTS_CODEX_PROFILE`) that resolves to the user's
  `~/.codex`** is refused with a `codex:` line (CX-D3). No test exists for it
  yet; the implementer adds one.
- **A non-numeric `window_minutes` or `windowDurationMins`** is treated as
  null, which gives the name `window` (see the CX-C11 decisions).
- **Engine, out of scope here:** the prompt goes to the adapter in argv
  (`--prompt`). The Linux limit of 128 KiB per argument (E2BIG) caps prompts
  for every provider that takes argv. Logged for later.

## Adapter review at 4f3f185 (ag-fae464, a claude opus stand-in for the reviewer)

The adapter's attack findings are fixed (4f3f185, 261 tests green). The
review verdict is approve with fixes. The fixes become new ids:

- **CX-C21 — window names never collide.**
  - Two live windows that map to the same name, because their durations are
    both null or equal, are kept apart, with suffixes `-primary` and
    `-secondary`.
  - Headroom is always the worst of all windows.
  - The live and rollout paths validate windows in one shared function.
  - Review findings 1 and 5.
- **CX-C22 — `login` prints its instructions before exec**, flushed, so they
  are visible when stdout is a pipe. Review finding 2.
- **CX-C23 — `budget` always exits 0 with JSON.** This holds even if a rollout
  file vanishes between the listing and the stat (race), or if any unexpected
  error occurs on the rollout path. Review finding 3.
- **CX-C24 — the live-read fallback says why.**
  - `note` names the failure class (`timeout`, `exit`, `jsonrpc-error`,
    `not-logged-in`, `unparseable`, `internal`). It is one line, with no
    bodies and no paths.
  - A programming error inside the live read is reported as `internal`, not
    silently treated like an unavailable server.
  - Review finding 4.
- **CX-C25 — no credential reaches argv.**
  - An inherited MCP server's `url` is never copied into the native argv.
    A placeholder is enough to disable it.
  - The only env values that may go on argv are an allowlist:
    `MULTIAGENTS_*` without secrets, `PATH` and locale.
  - Review finding 6.
- **CX-C26 — the event stream is trusted less.**
  - A non-string `thread_id` is ignored.
  - Mapped events no longer carry the whole raw Codex object.
  - An auth marker in a retryable `error` event does not fail the run. Only
    the final result, or the exit status, decides it.
  - Review finding 8.
- **Cleanup, not tested:**
  - `_shutdown` joins the reader thread before closing the pipe (finding 7);
  - signal handlers are installed before `Popen`, and a comment explains
    that agentwrap kills the process group (finding 9);
  - the argv rewrite in `main` is derived from the parser (finding 10).

### Decisions on the CX-C21..C26 tester's questions (ag-cc6173, merged; 27 tests, 24 red)

- **The env allowlist in CX-C25 is withdrawn.**
  - The multiagents entry's `env` is built by the engine for its own MCP
    server, and Codex only takes it through `-c`, which means argv.
  - The standing rule, recorded here for the engine: nothing secret goes
    into `mcp_env`.
  - The URL half of CX-C25 stands: inherited server URLs never reach argv.
  - So `tests/test_codex_provider_edges.py::test_cx_c10_hostile_mcp_env_values_inject_no_config_keys`
    is correct. The env-allowlist cases in `tests/test_codex_provider_review.py`
    must be removed or rewritten by `tester`.
- **`window_minutes: null` on the rollout path** stays invalid (the event is
  skipped). Only the live path names a window `window`.
- **Not black-box testable,** and accepted: the `internal` class of CX-C24,
  and the "one shared validator" half of CX-C21.

## Full-suite regressions after the offline work (host run at 858c70c)

Besides the 72 phase2 failures, which fail by design, the run has 5
failures:

- **3 characterization tests pin "three shipped providers":**
  - `test_c2_auth_characterization::test_load_providers_builds_the_three_real_shipped_providers`
  - `…::test_shipped_providers_yaml_folds_into_three_independent_families`
  - `test_c2_provider_harness::test_shipped_providers_yaml_parses_into_the_three_real_providers`

  CX-D1 makes codex the fourth. These are deliberate test updates, done by
  `tester`.
- **2 SP-R5 drift tests are pre-empted by the CX-C19 refusal:**
  - `test_session_persistence::test_sp_r5_drift_refusal_with_agents_inside_says_stop_first`
  - `test_session_persistence_adversary::test_sp_r5_drift_message_never_prescribes_refusing_command_when_stopped`

  On a host where codex is installed, `docker up` returns the
  versions-root refusal ("That ends any agent still inside") before the
  SP-R5 drift path can name the agents inside.

- **CX-C27 — a missing versions root is drift, and follows SP-R5.**
  - A container that lacks a declared versions root is reported through the
    same drift path as any other missing mount, with the same SP-R5
    behaviour: name the agents inside and say stop first, and never
    prescribe a command that would refuse.
  - The CX-C19 wording ("this container lacks the versions root <X>") stays
    as the description of that drift item.
  - Verified by: the two SP-R5 tests above, green on a host with codex
    installed, plus the existing CX-C19 tests.

## Live results

- **2026-09-28 ~21:40 UTC:**
  - `multiagents auth login codex` succeeded, into the docker backing.
  - **L5, partly checked.** `budget_status` shows codex `known: true`,
    `source: app-server`, bucket `codex`, 5h 0% and weekly 15%. The live
    `account/rateLimits/read` works from the host against the backing
    profile.
  - `refresh-models` fails until Codex has been used once in the profile,
    because `models_cache.json` does not exist yet. The first app-server
    read creates it. This is a minor finding.
- **L6/L1, first probe (ag-7270de, codex-probe on gpt-5.6-luna).** It failed
  with 401 "Missing bearer".
  - The adapter ran in the container.
  - `MULTIAGENTS_BIN` resolved.
  - The network reached `api.openai.com`, and the thread started.
  - **Cause: a contract error, mine.** CX-C8 revised says that under docker
    `CODEX_HOME=$HOME/.codex`. But an agent's HOME is its own per-agent home
    (`~/.multiagents/homes/<slug>/<agent>`), not the user's. So Codex got an
    empty profile. The backing is mounted at `MULTIAGENTS_PRIVATE_HOME`
    (`/home/theobroma/.codex`).
- **CX-C8, revised again:**
  - Under `MULTIAGENTS_EXECUTOR=docker`, the adapter sets
    `CODEX_HOME=$MULTIAGENTS_PRIVATE_HOME`.
  - If that variable is missing under docker, it refuses with a `codex:`
    line.
  - It never falls back to `$HOME/.codex`.
  - The tests that assert `$HOME/.codex` for docker change deliberately.
