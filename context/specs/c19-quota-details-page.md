# C19 — a quota details page with a masked account identity: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requested by:** the user.
- **Ids:** `QD-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer.
- **Depends on:** C18 (`context/specs/c18-monitor-quota-panel.md`), whose structured window lines this page reuses. It starts after C18 merges.

## What the user asked for

> A button on the monitor that opens a new browser tab, a page dedicated to quota details. For each provider it also shows its account identifier, so the user can recognise it. The identifier is masked with `*****` by default, and a small eye button shows it in clear.

## Behaviours

**QD-R1: a button opens the quota page in a new tab.**
- The monitor page has a button, near the quota panel, that opens the quota details page in a **new browser tab**.
- The new page is served by the same monitor server and requires the same token as the existing `/api/*` routes. The token must not end up in a URL that the server logs, or in the page history, beyond what the monitor already does today.
- **Verified by:**
  - the served monitor page contains the control, with a new-tab target;
  - the details route answers 200 with the token and refuses without it, exactly like `/api/state`.

**QD-R2: the page shows every provider's quota in detail.**
- For each configured provider:
  - all its windows, as in C18 MQ-R1/R1a: name, bar, percent, reset label, the uncounted and constraining marks, and the account the window belongs to when the reading carries one;
  - the reading's source and note, the provider extras of C18 MQ-R2a, its install state (MQ-R3a) and its health line.
- A provider with no reading says so.
- The page refreshes on the same cadence as the monitor, or on demand.
- **Verified by:** a fixture reading renders every window and extra of every provider in the page's data model, the same model C18 builds.

**QD-R3: each provider shows an account identity, masked by default.**
- **What is shown.** Each provider row shows the identity of the account it spends against, so the user can recognise it: preferably the account e-mail, otherwise an account or organisation id or name the provider exposes. When no identity can be obtained, it shows "unknown". It never guesses.
- **Masked by default.** The identity is displayed as a fixed `*****`, which reveals neither its length nor any of its characters.
- **The eye button.** A small eye button next to it reveals the clear value, and clicking again masks it.
- **The default holds.** A reload, a new tab or the periodic refresh all return to masked. The reveal state is not persisted.
- **The clear value stays on the server until asked for.** The state the page polls carries only the masked form and an `identity_available` flag. The clear value is fetched by a separate authenticated request when the eye is clicked, one provider at a time.
- **Verified by:**
  - the polled state contains no clear identity;
  - the reveal endpoint returns it with the token and refuses without it;
  - the rendered page shows `*****` until revealed.

**QD-R4: identity comes from the provider, never from a secret.**
- Each provider obtains its identity through an optional new provider action, `identity`, following the same script/action contract as `usage`, `check` and `budget`. It prints `{"identity": "<string>", "kind": "email|account|org"}`, or exits 64 when it has none.
- **Never a secret.** The action must never print a token, key, cookie or any credential material, not even a fragment such as the last four characters of an API key.
- **Where it may read.** It may read non-secret profile metadata, such as an e-mail the CLI stores beside its credentials or an OAuth profile endpoint, or decode the non-secret identity claims of an ID token. It returns only the identity claim.
- **Accounts sharing credentials.** Providers that share credentials (`auth_from`, the sidecar vault accounts) show the identity of the account actually used. claude-b shows vault account b's identity, and claude shows the default account's.
- **Shipped coverage.** It covers claude/claude-b, codex/codex-b, agy/agy-b (and their partners through `auth_from`), opencode and opencode-zai, where an identity is obtainable. The implementer says per provider what source it used, or why there is none. For example, a bare API-key provider may have no identity, and then shows "unknown".
- **Cached.** It runs on the host with the same timeout and caching as `usage` (C18 MQ-R4a), and never blocks the page.
- **Verified by:**
  - fixture credentials or profile files in a temporary HOME give the expected identity for each shipped provider;
  - a fixture whose credential file contains a token never has that token anywhere in the action's output, the server's responses or the monitor logs.

**QD-R5: identities are never recorded.**
- Identities do not go into the event log, the tree, run directories, tickets, transcripts or any file the monitor writes. They are held in memory only.
- **Verified by:** after a reveal, no file under the project's `.multiagents/` contains the fixture identity.

**QD-R6: no regression.**
- The monitor's existing page and panel, as of C18, the `/api/*` token checks and `budget_status` are unchanged.

## Out of scope

- Editing, switching or logging into accounts from the page.
- A persistent "always reveal" preference.
