# Tickets written by ag-a0be27 (copied 2026-09-30 for triage)

TICKET(blocking): container claude 401 persists after re-login: auth-proxy account tokens are never renewed, `check` cannot see it, and the proxy logs no requests
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local (was docker when this happened)
- providers available: claude, codex, opencode

## What happened
With `executor.docker.auth_proxy: true` and two labelled claude accounts in the vault (`accounts/a`, `accounts/b`), every claude agent in the workspace container got HTTP 401, "OAuth access token has expired". This continued after both accounts were re-logged (`auth login claude --account a|b`, "Login successful"), after the auth sidecar was restarted and recreated, and after the workspace container was recreated. The sidecar log showed only its `listening` line. `ANTHROPIC_BASE_URL` was present in the container env. Switching to the local executor made claude work.

## Expected
Agents are meant to reach the API through the sidecar, which swaps in a real token; `multiagents auth` should report the accounts' true state; a 401 from one account should fall back to the other.

## Reproduction
Not reduced. I could not run anything on the machine (no shell), so the points below are from reading the source. Sequence seen: docker executor + `auth_proxy: true` + labelled accounts, wait more than 8 hours after the last login, spawn a claude agent.

## Evidence (source at the commit above)
1. Sidecar silence is expected. `authproxy.py:248` `log_message` is a no-op; `_event` fires only for `rejected`, `exhausted`, `switch` (274, 282, 297). Forwarded requests are never logged, so the log cannot tell whether requests arrive. The `listening` line (407) does include the account labels.
2. Nothing renews per-account tokens. `docker.py:1249-1253` reads the expiry clock from `vault/.credentials.json`. With `accounts/` present that file is absent or stale, so it falls back to the placeholder, whose `expiresAt` is +365 days (`docker.py:1808`), and never refreshes. `claude.sh` `refresh` (line 327) makes the same top-level assumption. `authproxy.Accounts.token` (163) reads only `accessToken`. Access tokens last about 8 hours.
3. `check` cannot see it. `claude.sh` `check` reads the top-level vault file or the placeholder, so `multiagents auth` says "logged in" whatever `accounts/*` holds.
4. No failover on 401. `authproxy.py:309-325` returns the upstream 401 verbatim. Only 429/529 call `mark_limited`, so account b is never tried.
5. Tests: I found none for renewal of `accounts/*` tokens.

## Not established
The 401 that persisted right after re-login is not explained by (2), since fresh tokens should work. I could not observe whether the request reached the sidecar, or whether claude CLI 2.1.280 still sends to `ANTHROPIC_BASE_URL` for an OAuth login. This is where the trail goes cold.

## Scope
Blocks work: every container claude agent fails. It does not corrupt state.

Hypothesis (unconfirmed, inferred from source): (2) explains the first outage; something else explains the post-login 401.

PROPOSED_FIX:
- Add per-account renewal: iterate `accounts/*` in `refresh_private_credentials` and the `refresh` action, using each account's own credential file as the clock and as `CLAUDE_CONFIG_DIR` for the renewal probe.
- Make `check` read `accounts/*` when present.
- On upstream 401, call `mark_limited`-style unpinning and retry on the next account, as for 429.
- Log one line per proxied request (agent, account label, status) in `authproxy`.
- Have the proxy log a per-account expiry at start and on 401.
Risk: the refresh probe is a real API call per account; keep the 30 min early margin and the lock.
```

TICKET(blocking): codex adapter under the local executor runs with an empty per-agent profile, so ChatGPT-logged-in runs 401 against api.openai.com/v1/responses
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
Consults to a codex agent on model gpt-6-astra failed with: `unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, url: https://api.openai.com/v1/responses`. Earlier the same conversation failed with `codex: resume failed: requested '<uuid>', observed ''`. On the host, the private codex profile reports "Logged in using ChatGPT" and its `auth.json` had just been refreshed.

## Expected
A run should use the profile that `login` and `check` inspect.

## Evidence
- A ChatGPT login talks to the ChatGPT backend; a request to `api.openai.com/v1/responses` with no bearer is what the CLI does with no credentials at all. So the run's `CODEX_HOME` was empty.
- `defaults/providers/codex.py:80-87` `run_profile()` under local returns `_host_profile()` (line 59), which is `Path.home()/.multiagents/profiles/codex` (line 67).
- `Path.home()` follows `$HOME`. Agents get a per-agent HOME (`runner.py:1205`; default `home_policy: per-agent`). Codex declares no `home_links` (`providers.yaml:532`), and `MULTIAGENTS_CODEX_PROFILE` is not in `BASE_ENV_KEYS` (`executor/base.py:27`). So the run resolves to `<agent HOME>/.multiagents/profiles/codex`, which `_ensure_profile` creates empty.
- `check`, `login` and `budget` run in the host env with the real HOME, so they see the real profile and report healthy.
- The resume failure is the same cause: an empty home has no session, so no `thread.started` arrives (observed `''`).
- Model is not the cause: `--model` is passed through unmapped (`codex.py:210-211`).
- Earlier runs worked because docker uses `MULTIAGENTS_PRIVATE_HOME`.
- Test gap: `tests/support/codex_harness.py:167` always sets `MULTIAGENTS_CODEX_PROFILE`, so the `Path.home()` fallback under a per-agent HOME is never exercised.

## Reproduction
Not run. Expected: local executor, per-agent HOME, logged-in host profile, any codex consult. A check: `CODEX_HOME=$(mktemp -d) codex exec --json ping` should show the same 401.

## Scope
Blocks work: the codex advisor is unreachable under local. No state corruption.

Hypothesis (unconfirmed, inferred from source; the run's command.json and env were not inspected).

PROPOSED_FIX:
Resolve the host profile from the real home, not `$HOME`: the runner should pass `MULTIAGENTS_CODEX_PROFILE` (absolute, resolved on the host) into the adapter env, or the adapter should use `pwd.getpwuid(os.getuid()).pw_dir`. Also fail loudly when the resolved run profile has no `auth.json`, instead of creating it empty. Add a test with HOME set to a different directory and no `MULTIAGENTS_CODEX_PROFILE`.
```

TICKET(minor): `multiagents auth login codex` writes to a store chosen by the executor with no mention of which
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
`auth login codex` refreshed one store while agents used another. Under docker, `login_action` writes to the container backing (`MULTIAGENTS_PRIVATE_BACKING`, `codex.py:70-77`); under local it writes to the host profile. The command prints the same text either way, and the BRIEF's "not authenticated" advice sent the user to a login that could not help.

## Relation to the codex 401 ticket
Not a cause. Under local, login and run agree on the profile path, but the run's per-agent HOME makes the run resolve a different, empty one (see that ticket).

## Scope
Confusing, not blocking.

PROPOSED_FIX:
Print the profile path that `login` is refreshing and the executor that chose it, and warn when the executor changed since the last login.
```

TICKET(minor): deferred tasks restart on a provider that has just failed
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
After a provider failure the tree paused; deferred tasks restarted on the same failing provider and re-paused it, in a loop.

## Evidence
`runner.py:4266-4322` `resume_deferred` calls `start()` again. The entry stores no failure history (`runner.py:1658`), so routing is guarded only by the provider cooldown. A temporary `provider_down_cooldown_seconds: 10` was set at the time, which lets the cooldown lapse before the next poll. I could not reproduce this, and I cannot separate the config confound from a design gap.

## Scope
Annoying: wasted starts and repeated pauses. No state corruption seen.

Hypothesis (unconfirmed): a lapsed cooldown reads as half-open, so the next start goes to the failed provider.

PROPOSED_FIX:
Record the provider that just failed on the deferred entry and skip it in routing until a fresh probe succeeds, or grow the cooldown on repeated failures.
```

TICKET(minor): doom-loop watchdog false positive on codex `file_change` events
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
Repeated edits to one file by a codex agent trip `doom_loop` ("called 5x with identical arguments and nothing changed on disk") although the file changes each time.

## Evidence
- Signature is `name:args` (`providers.py:129`), and codex `file_change` args are `{"changes": [...]}` with path and kind only (`codex.py:306`).
- The "nothing changed" check hashes `git status --porcelain` (`runner.py:130-148`), which is content-blind: an already-dirty file keeps the same status line.
- The codex block in `providers.yaml` has no `opaque_tools`; only agy does (line 203).

## Reproduction
Not run. Expected: a codex agent edits one already-modified file five times.

## Scope
Annoying: a false `stuck` on productive agents.

PROPOSED_FIX:
Smallest: add `opaque_tools: [file_change]` to the codex block, as agy did for `view_file`. Better: make `_worktree_state` content-aware (hash `git diff` or the dirty files' mtime and size), which fixes the same class for every provider.
```

TICKET(minor): CLI subcommands ignore `args.path` when deciding whether a project exists (`test_doctor_clear_*` fail outside the project directory)
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
`tests/test_core.py` `test_doctor_clear_*` (three tests) fail when the suite runs from a directory that has no `.multiagents/` above it. I did not run them, so which of the three fail this way is unconfirmed.

## Evidence
`cli.py:1458`: `paths = _resolve(args.path) if find_project_root() else None`. `find_project_root()` is called with no argument and so checks the current directory, not `args.path` (`paths.py:82`). Outside a project, `paths` is None and `_clear_provider` has no tree. The same guard appears at `cli.py:1414`, `1589`, `1662`, `1893`, `1968`, `2008` and `2041`.

## Scope
Test fragility, plus a real bug: `multiagents doctor --path X` outside a project ignores X.

PROPOSED_FIX:
Use `if args.path or find_project_root()` at each site, or a small helper. Note `_resolve` registers the project as a side effect.
```

TICKET(minor): `consult` is refused by `max_concurrent` only when it creates a new conversation, so the orchestrator cannot always reach the advisor when busiest
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
A consult was refused with "4 agents already running" while 4 workers ran.

## Evidence
`runner.py:3991` calls `_preflight` only when `_consult_turn` creates a new conversation. `_preflight` holds the `max_concurrent` check (`runner.py:844`). An existing conversation resumes without it. So the refusal shows up when the advisor's route changed and its conversation was replaced, as after moving it to a different provider. The resume path also skips the token and budget ceilings.

## Verdict
A bug, not an intended limit: nothing documents consults as slot-holders, and the two paths disagree. Inferred from source, not run.

## Scope
Blocks the orchestrator from reaching the advisor at high concurrency.

PROPOSED_FIX:
Make consults exempt from the `max_concurrent` count, or give conversational agents one reserved slot, and apply the same preflight rules to the resume path. Flag: the total can then exceed `max_concurrent` by one.
```

TICKET(minor): `multiagents docker rm` leaves the auth sidecar running with its old mounts
```
## Environment
- multiagents commit: ba32688fa8c1
- python: 3.12.14 on Linux 7.0.0
- executor: local
- providers available: claude, codex, opencode

## What happened
After `docker rm` and recreation of the workspace container, the auth sidecar kept running.

## Evidence
`docker.py:1753-1754`: `ensure_auth_proxy` returns "existed" if the sidecar is running, so it keeps the vault and package mounts it was created with (for example after a `credential_scope` change or an upgrade). `docker rm` targets only the workspace container. I did not check what `rm` does with the sidecar; the source read shows only the early return.

## Scope
Stale sidecar; it can serve an old vault or code. Combined with the renewal defect in the claude 401 ticket, it makes recovery hard to reason about.

PROPOSED_FIX:
Have `docker rm` also remove the sidecar, or have `ensure_auth_proxy` compare the running sidecar's mounts and image to the expected ones and recreate on drift.
```