# What environment reaches scripts and agents (EV)

Findings: **F112** (context/review/C2-seam.md — `scripts.build_env`) and **F33**
(context/review/C1-sandbox-executor.md — the executor's `build_env`). Two
different functions; one group because both decide which ambient variables
cross a trust boundary. Plan: `context/plans/2026-10-05-scheduler-first-real-use.md`
§2, group *env*.

## Behaviours

**EV-R1 (F112).** The environment a provider script receives (via
`scripts.build_env`) no longer starts from the full ambient environment. It is
exactly:
- a named, documented allowlist of ambient variables, each taken from the
  ambient environment only if set:
  - process basics: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`,
    `LC_*`, `TZ`, `TMPDIR`;
  - profile/data locations the shipped scripts and their CLIs use:
    `CLAUDE_CONFIG_DIR` (claude.sh:127,198), `XDG_DATA_HOME` (opencode.sh:30),
    `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` (agy's
    keyring login);
  - network plumbing (addresses, not secrets): `HTTP_PROXY`, `HTTPS_PROXY`,
    `NO_PROXY` and their lowercase forms, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
    `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`;
  - the provider selectors scripts read today, named one by one:
    `MULTIAGENTS_CODEX_PROFILE` (codex.py:70), `MULTIAGENTS_OPENCODE_PLAN`,
    `MULTIAGENTS_ZAI_ORIGIN` (opencode.sh:23-24,73). No other ambient
    `MULTIAGENTS_*` variable is trusted.
- the `MULTIAGENTS_*` keys `build_env` already computes;
- the provider's own configured `env:` block;
- the trusted action overlays callers pass today (launch, resume, budget,
  compact, auth-profile `extra_env`: scripts.py:166,273,334; driver.py:1313-1315,
  1572-1576), with their existing precedence and protected-key enforcement.
Nothing else — in particular no ambient token, API key or base URL — is
forwarded unless one of the sources above names it.
Verified by: `test_build_env_copies_the_full_ambient_process_environment`
inverted; a test that a sentinel secret-looking ambient variable (and an
unlisted ambient `MULTIAGENTS_*` one) is absent; that each allowlisted variable
is present when set; that an action overlay still wins as today.

**EV-R2 (F112).** Every shipped provider script's actions behave as before
under EV-R1. If a shipped script needs an ambient variable not on the list, it
is added to the list by name with a one-line reason beside it — or, if it is
provider-specific, to that provider's `env:` block. Adding a broad pattern
(anything wider than `LC_*`) is a decision: stop and say so.
Verified by: the existing script/auth/budget suites green; review of each
addition.

**EV-R3 (F33).** In the executor's `build_env`, a key named in `blocked` is
never forwarded, whether it comes from the always-on base keys or from
`passthrough`. (Blocking `PATH` is the operator's choice and is honoured.)
Verified by: `test_build_env_base_keys_are_forwarded_even_if_named_in_blocked`
inverted; tests for a blocked base key and a blocked passthrough key.

**EV-R4.** With an empty `blocked` list and the default passthrough, the agent
environment is unchanged from main.
Verified by: a test comparing the default output before/after, and the executor
suites green.

## Out of scope
- F113 (provider identity in `MULTIAGENTS_PROVIDER`) and other C2-seam findings.
