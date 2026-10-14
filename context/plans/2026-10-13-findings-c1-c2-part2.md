---
status: imported
applied_in: f229cf22
imported_in: e3808c1c
---
# Review findings, C1 and C2, part 2: the medium and low ones

The initializer triaged with the user on 2026-10-13 the 40 findings still
`open` after part 1 (`2026-10-13-findings-c1-c2-part1.md`). Each was checked
against main at 67411d14: its characterization test, which pins the
defect, still passes. Three were checked by hand: F55 is fixed, F2 and F11
are not.

**Already recorded in the ledger**, with the reason in each note:
- **`fixed`:** F55. `allowlist_entries` reads `... or []`; it came with ST9.
- **`accepted`:** F68, F72, F75, F115 and F141.
- **`deferred` to phase 7 part 2:** F31, F32, F34, F35, F64, F65, F69 and
  F70. These are executor lifecycle defects; part 2 rewrites that code, and
  its spec must carry them as requirements.
- **`scheduled`, pointing to this plan:** the 26 findings below.

**Order.** This plan starts after part 1 has merged, because item 1
depends on part 1's rework of `_scrub` (F24) and both plans touch
`executor/docker.py`. Inside the plan, the items are independent apart from
their locks.

## Apply now

The standing pipeline applies: tester, implementer, then a reviewer on
every implementer run. Item 5 is tests only: a tester and a reviewer, no
implementer. The orchestrator writes the contracts, citing these ids, and
groups or splits items as the files allow.

Where a characterization test pins the defect (F150's lesson), a tester
reverses it deliberately; it is never "fixed" by the implementer.

**1. Sandbox and egress (C1): F2, F11, F21, F63.** An adversary pass runs
before merge, because this is the egress control and the credential
boundary.
- **F2.** A non-string `egress_allowlist` entry crashes config generation;
  `[5]` raises `AttributeError` on main. It must be refused with a
  diagnostic, like the other refused entries.
- **F11.** `allowlist_entries` still accepts `" a.com"` (whitespace) and
  `"b.com:443"` (a port) as allowed hosts that match nothing. ST9 already
  refuses a leading dot, a public suffix and IPv6. These two shapes must
  be refused with a diagnostic too. `_validate_allowlist_entry` already
  words them; reuse it.
- **F21.** `_scrub` removes only content shaped like a secret, so an
  organisation name or an email in an upstream error body passes. It
  builds on part 1's F24 rework. The contract decides what counts as
  personal data here, with the redaction literals and the email shape as
  the minimum.
- **F63.** `credential_drift` puts paths into a shell command without
  quoting them. Pass an argv, or quote. Never build a shell string from a
  path.

**2. Failure classification (C2): F136, F137, F20, F114.** Since TF,
`looks_like_auth_failure` decides the failure class, and therefore whether
a run is retried (`failures.py:160`, `runner.py:7652`).
- **F136.** `"401"` is an unanchored substring. Match an HTTP status, not
  any text that contains 401.
- **F137.** Common real authentication-failure phrasings are missed. Add
  them from real provider outputs. TF already collected samples in
  `providers.yaml` `failures.*`.
- **F20.** Upstream errors other than 429/529 emit no `on_event`. Emit
  one, without the body's content.
- **F114.** A timeout and a "script is not executable" `OSError` share an
  exit code. Give them distinct, documented codes.
- **TF must stay green.** The TF suite is part of "done" for this item.

**3. Auth proxy (C1): F66, F67, F23, F22.**
- **F66.** `Handler._event` swallows every exception from the callback.
  Log it, through the redactor, and keep serving.
- **F67.** A rate-limited account is retried with zero backoff. Add a
  bounded backoff consistent with TF's retry budget and quota holds; it
  must not fight them.
- **F23.** Every verb is forwarded as `POST`. Forward the request's own
  verb.
- **F22.** The error passthrough drops the upstream headers. Keep the safe
  ones, the `retry-after` / rate-limit family above all. Never forward
  `set-cookie` or auth headers.

**4. Quotas and budget (C2): F171 first, then F160, F161, F123, F124, F142,
F143.** Lock `budget.py`.
- **F171.** The cooldown decoration lands on the shared cached `Budget`.
  The note accumulates on every cache hit, and the severity stays
  `critical` after the cooldown has lapsed. This distorts routing, so it
  goes first.
- **F160 and F161.** Claude spend is invisible in `budget_status` and in
  `render()`'s per-node label. Show it, or show "not reported" honestly;
  never a blank or zero.
- **F123.** A script's `headroom` outside [0, 1] is accepted. Refuse it
  (`known=False` with a note) or clamp it, but say so.
- **F124 and F142.** An empty script response must carry an explanatory
  note. `read_claude` must not claim `cachedUsageUtilization` as its source
  when nothing was read.
- **F143.** Write `models.yaml` atomically: a temp file in the same
  directory, then a rename.

**5. Tests that protect nothing (C2 `budget.py`): F151, F152, F153, F155,
F156.** Tests only. Pin each boundary so that the mutation the adversary
applied fails:
- `reserve` `>=`;
- the severity thresholds at 75 and 90, in both derivations;
- the cache TTL `<`;
- the `usable` headroom at 0.02.

Cover the two untested `usable` behaviours. The reviewer checks each
mutation fails, by applying it.

**6. Provider seam (C2): F113, F133.**
- **F113.** `MULTIAGENTS_PROVIDER` must name the provider whose script
  actually runs, including through `extends:`.
- **F133.** An `extends:` naming a provider that does not exist is refused
  at load with a message. Today it forms a phantom family.

**7. When everything has merged,** set each finding to `fixed` in the
ledger, citing the merge commit.

## Next phase

- **Phase 7 part 2: one container per run.** It is specified with the user
  from `phase7-nodes-and-containers.md` (PAC-R*), with the scope recorded
  in the plan of 2026-10-12:
  - tmux step 2;
  - the live node view;
  - microVM as an option;
  - the 8 findings deferred here (F31, F32, F34, F35, F64, F65, F69, F70),
    as requirements of the rewritten executor.
- **When this plan and part 1 are done,** the ledger holds no live finding
  apart from those 8. The review of C1 and C2 is then acted on.
- **C3 to C7 were never reviewed** (`REPORT.md`, "What this covers"). Whether
  to review them is the user's choice, to raise once part 2 is specified.

## Config changes

None.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
