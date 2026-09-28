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
