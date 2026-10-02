# Claude under the docker executor authenticates — the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Ticket:** bug-07d880, blocking. It is fixed in-house and never submitted.
- **Ids:** `DK-R*`, never renumbered.
- **Why:** the user wants to go back to `executor.kind: docker` (needed for the second agy account, `context/specs/agy-second-account.md`).

## Root cause (bug-reporter ag-b41640, live probe)

- The CLI does honour `ANTHROPIC_BASE_URL`, and requests reach the auth sidecar (`authproxy.py`), which forwards upstream.
- When the vault has `accounts/*`, `Accounts.labels()` (`authproxy.py` ~149-162) returns only those accounts and drops the top-level default login.
- `multiagents docker login claude`, `claude.sh check` and `refresh_private_credentials` (`docker.py` ~1398) all touch only the top-level `.credentials.json`. The sidecar therefore keeps serving the expired `accounts/a`.
- Upstream 401s are relayed without any log event (`authproxy.py` ~309-320), and there is no failover.
- `claude-b` (`env: {CLAUDE_CONFIG_DIR: ~/.multiagents/profiles/claude-b}`) is not mounted in the container, and its profile dir points the CLI away from the sidecar placeholder.

## Behaviours

**DK-R1: every vault login is in the pool.**
- The account pool contains the top-level default login when it exists, plus every `accounts/<label>`.
- A login refreshed by `multiagents docker login claude` is served.
- **Verified by:** a vault with a fresh top-level file and an expired `accounts/a`. A request through the sidecar authenticates with the fresh one.

**DK-R2: a 401 is logged, and fails over.**
- An upstream 401 for account X emits an `unauthenticated` event naming the account label (never the token).
- X is marked unusable until its credential file changes, i.e. a new login or a refresh.
- The request is retried on the next usable account, and a 401 reaches the agent only when no account is left.
- **Verified by:** two accounts, the first answering 401. The request succeeds on the second, and the event is present.

**DK-R3: renewal and check cover every account.**
- `refresh_private_credentials` and the provider `refresh` action renew every vault account that is near expiry, not only the top-level one.
- `check` reports a per-account status (label → ok, expired or missing), and the provider counts as authenticated if at least one account is usable.
- **Verified by:** an expired `accounts/b` is refreshed, and `check` lists both labels.

**DK-R4: a provider pinned to its own account works under docker.**
- A provider may declare, in provider config, which vault account its container requests use. One possibility is a key like `container_account: b`. `claude-b` is the case in point.
- **Pinning.** Requests from that provider's agents are served ONLY with that account. They are never pooled with or failed over to another account. If that account is unusable, the error says so.
- **Login.** `multiagents docker login <provider>` logs into that account.
- **Profile directory.** The provider's relocated profile dir (`CLAUDE_CONFIG_DIR`) exists and is writable inside the container, and the CLI there sees the sidecar placeholder rather than real credentials. Achieve this by mounting, linking or dropping the env var under docker; the implementer chooses, and must explain why.
- **Unpinned providers.** Base `claude` without a pin uses the pool (DK-R1 and DK-R2), minus accounts pinned by other providers.
- **Generic.** No provider name in `src/multiagents/*.py` (P0-R8).
- **Verified by:**
  - an agent of the pinned provider gets account `b`'s token and never `a`'s, even when `b` 401s;
  - base `claude` never gets `b`'s token.

**DK-R5: the budget reads the right account under docker.**
- Under docker, a pinned provider's budget reads its pinned account's quota, and the base provider reads the pool's (or the default's).
- Neither reports the other's.
- **Verified by:** two accounts with different usage; each provider reports its own.

**DK-R6: no regression.**
- The local executor is unchanged, including `claude-b` under local via `CLAUDE_CONFIG_DIR` and `tests/test_budget_extends_reader.py`.
- codex, opencode, deepinfra and agy under docker are unchanged.
- **Verified by:** the existing docker, authproxy, budget and login suites stay green.

## Revision after the advisor's check (2026-10-03, before tests)

These override any earlier wording they contradict.

**DK-R4a: identity the sidecar can trust.**
- Today the sidecar token authenticates only an identity string minted from the project slug (`docker.py` ~2000, `authproxy.py` ~75-109).
- Pinning requires the host to mint, per launch, a signed claim that carries the provider (or the account it is pinned to). The signing is the same HMAC scheme as today. An unsigned header is never trusted.
- This enforces routing identity, not isolation between agents in one container. Say so in a code comment.

**DK-R4b: account namespace.**
- `default` is reserved for the top-level vault login. An existing `accounts/default` is a load-time error whose message says how to rename it.
- Labels are validated: lowercase letters, digits, `-` and `_`.
- A pinned provider's label refers to the vault of the provider it takes its credentials from (its `extends`/`auth_from` owner). An inherited private profile does not get a separate vault for this purpose.
- `docker login <pinned provider> --account X`, with X different from the pin, is refused.
- The pin configuration reaches the sidecar before it admits requests, and survives a sidecar restart or config reload, because it is derived from config at each start.

**DK-R2a: bounded failover that keeps pins.**
- At most one attempt per eligible account.
- A retry happens only before any response byte has been sent to the agent.
- Pins are enforced on 401 AND on 429/529: today's rate-limit handling unpins (`authproxy.py` ~209-216), and that must stop for pinned agents.
- "Credential changed" is detected by content (e.g. a hash), not by mtime alone.

**DK-R3a: check and renewal under pins.**
- For a pinned provider, `check` and auth status report THAT account's status. "Any usable account" applies only to unpinned providers.
- Renewal enumerates every account, even when the default is healthy. Today the refresh gate looks only at the top-level file (`docker.py` ~1395-1402).

**DK-R5 (precise): budget under docker.**
- Under docker, the budget resolves the selected vault account(s) explicitly, and never falls back to host credentials. Today `claude.sh budget` returns 64 and the built-in reads host credentials (`budget.py` ~493, ~535-563, ~1032).
- **Pinned provider:** the quota of its account.
- **Unpinned provider:** each eligible account (the pool minus pinned accounts) is read, and headroom is the **best** usable account's, because routing fails over to it. The windows are reported per account. If no account is readable, the result is unknown.
- The account selection is part of the budget cache identity.

## Out of scope

- Switching this project to docker, and recreating the container: the user does that, guided by the orchestrator.
- Any `egress_allowlist` change.
