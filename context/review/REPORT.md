# Review — multiagents

## What this covers

Seven bounded contexts were mapped (`context/review/MAP.md`). Two were reviewed in full, one was reviewed partially, and four were not reviewed at all.

| Context | Harness | Characterization | Audit | Adversary | Findings |
|---|---|---|---|---|---|
| **C1** sandbox & egress | done | 3 surfaces | done | done | 38 |
| **C2** provider seam | done | 3 surfaces | done | **budget.py only** | 30 |
| **C3** agent lifecycle | done | **not covered** | **not covered** | **not covered** | 0 |
| C4–C7 | mapped, not started | — | — | — | — |

**68 distinct findings** across C1 and C2 (61 + 7 from C2's adversary). C3's harness is merged and proven (7 green proof tests) but nothing beyond that ran. C4–C7 were ranked and budgeted but never reached.

**What was NOT reviewed and why.** C2's adversary and C3's characterization/audit/adversary were planned and budgeted. Both hit their budget ceiling — but not from the volume of work. Every ceiling here was exhausted by **cache-read token accounting on single runs**: `ctx-lifecycle` recorded 2,043,007 tokens against a 400,000 ceiling from **one** harness run, of which 1,939,456 were cache reads at a real cost of $0.51. Meanwhile the `claude` provider reports no token total at all and counted as zero against its tags throughout. This is a defect in the tooling (`bug-565863`), not a statement about the reviewed code. The brake is absent where the money is and overwhelming where it is not.

**The gap worth naming — now answered, and the answer is no.** On C1, the adversary produced the review's most consequential single finding — F50, that deleting one line from `write_proxy_config` inverts the proxy from allow-list to open relay while all 48 characterization tests continue to pass. C2's 188 characterization tests were the open question; someone has now asked it of the 70 that cover `budget.py`, and the answer is that **they are not load-bearing for routing or accounting correctness**. Seven mutations were applied; four survived — the `reserve` boundary, the severity thresholds at 75 and 90 in both derivations, the cache TTL boundary, and the fresh-read `spent` merge. The seam and auth surfaces (the other 118 tests) have not been attacked. The seven new findings are F150–F156.

**Two facts about the test suite.** The repository's suite is green in no environment: on a host with docker it fails one test (`test_the_claude_script_uses_the_container_profile_only_where_it_should`), and inside an agent container it fails seventeen different ones (`PermissionError: ... can_spawn is false`). The documented baseline of "545 passed / 3 skipped" describes neither. One test is deliberately red as standing evidence for F100. The review added roughly 380 characterization tests across C1 and C2, and they pass.

---

## The six that matter

These are the findings that would change what someone does this week. Ranked by consequence, not severity label.

1. **F50** (critical, rewrite) — Deleting `FilterDefaultDeny Yes` from `write_proxy_config` inverts the proxy from allow-list to deny-list (open relay). All 48 characterization tests pass. The suite is not load-bearing for the security-critical directive it generates.

2. **F150** (high) — The C2 characterization suite *defends* F122's defect: `test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object` asserts the broken cache behaviour, so fixing the code fails the test. The test's name reads like an intentional invariant; whoever fixes F122 sees a red test and reverts. F50 and F150 together say something neither could alone: a characterization suite can fail in both directions, by not noticing a defect and by protecting one.

3. **F1** (critical) — An `egress_allowlist` entry containing `|` turns the allowlist into an allow-all. The regex escapes only the literal dot; POSIX ERE alternation has the lowest precedence of any operator, so `evil.com|.*` produces a pattern that matches any host. Confirmed by reproduction against the real `write_proxy_config`.

4. **F112** (high) — `scripts.build_env` copies the full ambient process environment (`dict(os.environ)`) into every provider script invocation, unfiltered. Inside an agent container this includes `CLAUDE_CODE_MESSAGING_TOKEN`, `ANTHROPIC_BASE_URL`, sandbox proxy URLs, and `MULTIAGENTS_ROOT`. A project-local provider script (which wins by precedence) receives the calling process's live credentials.

5. **F120** (critical) — Multi-account claude quota reading is unreachable. `read_claude(config_dir=...)` exists specifically to read per-account credential files, but every real invocation goes through `claude.sh`'s `exit 64` fallback to `builtin()` called with **zero arguments**, silently discarding the caller's `config_dir`. The `config_dir` parameter is dead code from every real caller today.

6. **F130 + F131 + F132 + F140** (high/high/high/medium) — All three shipped provider scripts and the Python `_claude_token` function treat credential-file presence as validity. A corrupt, truncated, or format-changed credentials file that the code cannot read a clock from is reported as "logged in" / "valid token." The runner discovers the truth only when the agent's first turn 401s.

---

## By context

### C1 — Sandbox and egress boundary

Complete review: harness, three characterization surfaces (allowlist, auth-proxy, executor), adversary (mutation + fuzzing), and structural audit. 38 findings.

C1 is the only context where being wrong costs something that cannot be taken back: a credential leaving the network. It is the sole egress path, reachable by untrusted agent output by construction. The review confirmed the risk is real and concentrated in `write_proxy_config` and the auth proxy's `_scrub` function. The allowlist regex escapes only the literal dot, which is the root cause of ten findings (F1, F10, F12, F13, F14, F50, F51, F52, F53, F54). The adversary proved the 48-test suite does not validate the tinyproxy directives that decide what those patterns mean — a mutation that turns the proxy into an open relay passes all of them. The auth proxy's `_scrub` is a complete no-op on non-JSON error bodies (F24), bypassing even its own shape-based patterns. The executor's `mounts()` dedup silently downgrades read-only to writable on path collision (F30). Three high-severity bugs in lifecycle management (F60, F61, F62) leave containers running after stop and swallow network-connect failures as success.

| Id | Severity | Class | Evidence | Summary | Disposition |
|---|---|---|---|---|---|
| F50 | critical | security | reproduction | Removing FilterDefaultDeny Yes inverts proxy to open relay; all 48 tests pass | rewrite |
| F1 | critical | security | reproduction | Pipe in allowlist entry defeats entire allowlist mechanism | fix |
| F10 | high | security | reproduction | Bare suffix entries (e.g. `com`) act as wildcards across unrelated hosts | rewrite |
| F13 | high | bug | reproduction | Unbalanced grouping metacharacters crash tinyproxy's regex evaluation | rewrite |
| F14 | high | security | reproduction | Balanced `()`/`[]` create silent regex match, ignoring literal meaning | fix |
| F24 | high | security | reproduction | `_scrub` is a complete no-op on non-JSON error bodies | fix |
| F30 | high | security | reproduction | Read-only extra_mounts silently downgraded to writable on path collision | fix |
| F60 | high | bug | reproduction | `stop()` never stops or removes the auth container | fix |
| F61 | high | bug | trace | `ensure_proxy` returns success when bridge-network connect fails | fix |
| F62 | high | bug | trace | `ensure_auth_proxy` returns success when bridge-network connect fails | fix |
| F2 | medium | correctness | trace | Non-string allowlist entry crashes with uncaught AttributeError | fix |
| F11 | medium | maintainability | reproduction | Dead/malformed entries silently match nothing | fix |
| F12 | medium | security | reproduction | Unescaped quantifiers `*+?{}` change match semantics | fix |
| F20 | medium | correctness | reproduction | Ordinary upstream errors (non-429/529) emit no on_event at all | fix |
| F21 | medium | security | reproduction | `_scrub` leaves PII (org, email) unscrubbed despite comment claiming otherwise | fix |
| F31 | medium | correctness | reproduction | Built-in mounts dropped silently if directory doesn't exist yet | fix |
| F34 | medium | correctness | reproduction | `prepare_home` raises uncaught on file-at-target or unwritable parent | fix |
| F51 | medium | correctness | reproduction | FilterType ere→regex changes pattern semantics silently | fix |
| F52 | medium | correctness | reproduction | FilterCaseSensitive Off→On breaks case-insensitive DNS matching | fix |
| F53 | medium | correctness | reproduction | FilterURLs Off→On changes what patterns are matched against | fix |
| F54 | medium | correctness | reproduction | Filter file path change undetected by any test | fix |
| F63 | medium | security | reproduction | `credential_drift` interpolates paths into shell command without quoting | fix |
| F64 | medium | architecture | trace | `ensure_running` performs side effects before verifying image exists | fix |
| F66 | medium | maintainability | reproduction | `Handler._event` silently swallows all exceptions from callback | fix |
| F67 | medium | performance | reproduction | `do_POST` retries rate-limited accounts with zero backoff | fix |
| F22 | low | correctness | reproduction | Error passthrough drops all upstream response headers | fix |
| F23 | low | bug | reproduction | Every HTTP verb forwarded upstream as POST, including literal GET | fix |
| F32 | low | correctness | reproduction | `pids_limit`/`cpus`/`memory` of 0 silently omitted; negative passed unvalidated | fix |
| F33 | low | correctness | reproduction | `build_env` blocked list doesn't guard base keys (same defect as F73; severity and class contested — F73 reads medium/security) | fix |
| F35 | low | correctness | reproduction | String-form extra_mounts always writable; relative paths unresolved | fix |
| F55 | low | correctness | reproduction | `egress_allowlist: null` raises uncaught TypeError | fix |
| F56 | low | maintainability | reproduction | Filter file trailing newline untested | accept |
| F65 | low | bug | trace | `project_placeholder` leaves credential temp file on disk if interrupted | fix |
| F68 | low | performance | reproduction | `mark_limited` unpins all agents simultaneously (thundering herd) | accept |
| F69 | low | bug | reproduction | `seed_private_state` TOCTOU-vulnerable PID check on lock deletion | accept |
| F70 | low | bug | trace | `Handle.stop` may SIGKILL reassigned process group after timeout | accept |
| F72 | low | performance | trace | `Accounts.token` cache has no eviction, grows unbounded | accept |
| F75 | low | security | opinion | `_copy_settings` follows symlinks for JSON branch without checking | accept |

### C2 — Provider seam, quota and failover

Harness, three characterization surfaces (provider, seam, budget + auth), structural audit, and adversary against `budget.py` only (70 of 188 tests). 30 findings (23 + 7 from the adversary run: F150–F156). The seam and auth surfaces have not been attacked.

C2 has the highest aggregate fan-in in the codebase and holds the invariant the user named: provider literals must not be hardcoded. The review found the seam is not merely under-tested — it has a failing test on trunk that nobody is reading (the deliberately-red test for F100). The provider scripts' `build_env` copies the full ambient process environment into every script invocation (F112), handing live credentials to project-local scripts. Multi-account claude quota reading is completely unreachable (F120). All three shipped provider scripts treat credential-file presence as validity (F130, F131, F132). The budget cache is keyed only by provider name, causing test-state bleed (F100), and cache hits mutate the shared cached object (F122). `run_action`'s "never raises" contract is violated by non-UTF-8 output (F110). `choose_provider` treats an unread provider as usable when preferred but unusable when fallback (F121).

| Id | Severity | Class | Evidence | Summary | Disposition |
|---|---|---|---|---|---|
| F120 | critical | bug | reproduction | Multi-account claude quota reading unreachable; config_dir is dead code | fix |
| F100 | high | correctness | reproduction | `budget._cache` keyed only by provider name; unrelated tests bleed state | fix |
| F110 | high | correctness | reproduction | `run_action` raises UnicodeDecodeError, contradicting "never raises" contract | fix |
| F112 | high | security | reproduction | `build_env` copies full ambient environment to provider scripts | fix |
| F121 | high | bug | reproduction | `choose_provider` treats missing entry differently by argument position | fix |
| F130 | high | bug | reproduction | `claude.sh check` reports "logged in" for corrupt/unreadable credentials | fix |
| F131 | high | bug | reproduction | `agy.sh check` authenticates on any non-empty token file | accept |
| F132 | high | bug | reproduction | `opencode.sh check` defaults to "1 credential" on any unparseable output | fix |
| F21 | medium | security | reproduction | (C1 auth-proxy) `_scrub` leaves PII unscrubbed — listed here for cross-reference | fix |
| F111 | medium | correctness | reproduction | Script background children outlive `run_action`'s timeout | fix |
| F113 | medium | correctness | reproduction | `MULTIAGENTS_PROVIDER` env var can mismatch the script that actually ran | fix |
| F122 | medium | bug | reproduction | Cache hit mutates shared Budget, overwrites `spent` instead of merging | fix |
| F135 | medium | bug | reproduction | Credential-count extraction fails when digit opens the CLI's line | fix |
| F136 | medium | bug | reproduction | `looks_like_auth_failure` "401" marker is unanchored substring match | fix |
| F137 | medium | bug | reproduction | `looks_like_auth_failure` misses common real-world auth-failure phrasings | fix |
| F140 | medium | bug | reproduction | `_claude_token` returns token as valid when `expiresAt` is non-numeric | fix |
| F133 | low | correctness | reproduction | `extends:` nonexistent parent creates phantom family group | fix |
| F114 | low | maintainability | reproduction | Timeout and non-executable OSError both return exit code 124 | accept |
| F115 | low | maintainability | reproduction | Provider `env:` block cannot reference `MULTIAGENTS_*` values from same call | accept |
| F123 | low | bug | reproduction | Script headroom accepted with no range validation | accept |
| F124 | low | maintainability | reproduction | Empty response and parse failure both produce `known=False` with no note | accept |
| F141 | low | performance | trace | `detect_opencode_subscription` reads disk on every call with no caching | accept |
| F142 | low | maintainability | trace | `read_claude` sets source before cache read; misleading when both fail | accept |
| F143 | low | bug | trace | `refresh_models` writes non-atomically; crash mid-write leaves corrupt file | fix |
| F150 | high | correctness | reproduction | Suite pins F122's defect as correct; fixing the cache path fails the test | fix |
| F151 | medium | correctness | reproduction | `reserve` boundary (`>=` → `>`) survives all 70 tests | fix |
| F152 | medium | correctness | reproduction | Severity thresholds at 75 and 90 unpinned in both `read_claude` and script derivation | fix |
| F153 | low | correctness | reproduction | Cache TTL boundary (`<` → `<=`) survives all 70 tests | fix |
| F154 | medium | correctness | reproduction | Fresh-read `spent` merge at line 672 is correct but unpinned; undefended against regression | fix |
| F155 | low | correctness | reproduction | `usable` headroom boundary pinned loosely enough that a shift from 0.02 to 0.021 passes | accept |
| F156 | low | maintainability | reproduction | Two correct `usable` behaviours (unknown headroom, negative headroom) completely untested | fix |

### C3 — Agent lifecycle and concurrent tree state

Harness only, merged and proven. `tests/support/c3_harness.py` and 7 green proof tests establish that the lifecycle is drivable from inside an agent: `runner`, `tree`, `gitops`, `watchdog` and `procs` are all reachable. Only `driver.py` is not, because its entry points launch an interactive CLI. No characterization, no audit, no adversary, no findings.

`runner.py` at 61% coverage (435 uncovered statements) is the largest absolute block of untested logic in the codebase. This context was budgeted at 150k but hit its ceiling from cache-read accounting, not work volume.

### C4–C7

Mapped and ranked, never reviewed. C4 (configuration, 90k budget) has the highest blast radius by fan-in but is also the best-tested code. C5 (MCP server, 90k) has the lowest coverage in the codebase at 36%. C6 (CLI, 60k) is the highest-churn file. C7 (monitor, 50k) is mostly read-only presentation except `actions.py` at 41% coverage.

---

## Rewrites proposed

Three findings carry `Disposition: rewrite`. These are the expensive decisions and they should not be buried among the fixes.

**F50** — The `write_proxy_config` function and its 48-test suite need to be rewritten together. The suite validates pattern generation but never validates the tinyproxy directives that decide what those patterns mean. A rewrite must add tests that assert `FilterDefaultDeny Yes`, `FilterType ere`, `FilterCaseSensitive Off`, `FilterURLs Off`, and the `FilterFile` path are all present and correct in the generated config. Without this, the suite is not load-bearing for the security boundary it claims to cover.

**F10** — Bare or short generic suffix entries (e.g. `com`, `io`) act as wildcards across unrelated hosts because the regex anchors to `(^|\.)` and allows any prefix. A rewrite must either validate that each entry is a plausible hostname (rejecting bare TLDs) or change the regex generation to require a dot-separated structure.

**F13** — Unbalanced grouping metacharacters (`(`, `[`) create syntactically invalid ERE lines that crash tinyproxy's evaluation on every request. A rewrite must escape all ERE metacharacters (not just `.`), which would also resolve F1, F12, and F14 as a side effect.

---

## Patterns

Recurring shapes across contexts — the same defect seen from different angles.

**1. The allowlist regex escapes only the literal dot.** F1, F10, F12, F13, F14, F50, F51, F52, F53, F54 — ten findings, all stemming from `host.replace(".", r"\.")` as the entire escaping strategy in `write_proxy_config`. The adversary (F50–F54) proved the tinyproxy directives themselves are also untested. Fixing the escaping to `re.escape`-equivalent behaviour resolves F1, F12, F13, F14 in one change; F10 and F50–F54 need suite changes.

**2. Credential presence treated as validity.** F130 (`claude.sh`), F131 (`agy.sh`), F132 (`opencode.sh`), F140 (`_claude_token` in Python) — all four treat "file exists and I couldn't read a clock from it" as "authenticated" / "valid token." The pattern spans all three shipped provider scripts and the Python fallback. The runner discovers the truth only when the agent's first turn 401s.

**3. Silent success on failure.** F61, F62 (network-connect failure returns `ok: True`), F130, F132 (unreadable credentials → "logged in"), F20 (upstream errors invisible to monitoring via `on_event`). The system prefers to report success than to admit it cannot tell. An operator watching events sees rate-limit churn and rejected tokens but is completely blind to the upstream itself returning errors.

**4. build_env inconsistencies across C1 and C2.** F33 (C1: `build_env` in `base.py` forwards BASE_ENV_KEYS even when named in `blocked`) and F112 (C2: `build_env` in `scripts.py` copies the full ambient environment to provider scripts). Two different `build_env` functions in two different contexts, both with the same shape: the environment construction does not enforce the boundary its docstring or contract claims. F33 was filed twice (as F33 low/`correctness` and F73 medium/`security`); F73 is accepted as the duplicate. The severity disagreement is recorded but not resolved — forwarding a variable the configuration explicitly blocked is arguably the security reading, and that is the one the reader should weigh.

**5. Cache and mutable-state aliasing.** F100 (`budget._cache` keyed only by provider name, causing test-state bleed) and F122 (cache hit mutates the shared cached `Budget` object, overwriting `spent` instead of merging). Module-level mutable state shared across callers without isolation or copy-on-read.

**6. A characterization suite can fail in both directions.** F50 (C1: the suite does not notice the proxy directive is removed) and F150 (C2: the suite defends the broken cache behaviour, so a fix fails the test). Two contexts, two opposite failure modes, same conclusion — the suite is not load-bearing for the correctness it claims.

**7. The same missing boundary in two independent derivations.** F152: severity thresholds at 75 and 90 are unpinned in both `read_claude` (line 462-464) and the script derivation (line 634). Two independent code paths, same gap. F154 compounds this: the *correct* merge at line 672 is itself unpinned, so a fix that makes the cache path match line 672 leaves line 672 undefended against later regression.

---

## Health

**Coverage as it stands.** C1's modules: `docker.py` 66%, `base.py` 76%, `authproxy.py` 50% (the lowest of any non-read-UI module). C2's modules: `budget.py` 81%, `providers.py` 93%, `scripts.py` 87%, `auth.py` 77%, `catalog.py` 84%, `supervisor.py` 93%. C3's `runner.py` is 61% with 435 uncovered statements — the largest absolute block of untested logic in the codebase.

**What the characterization suite pinned.** Roughly 380 new tests across C1 and C2, all passing. The C1 allowlist suite (48 tests) thoroughly validates pattern generation but does not validate the tinyproxy directives. The C1 auth-proxy suite pins header/body/query forwarding, account-credential edge cases, rate-limit sequencing, and the scrub boundary. The C1 executor suite pins mount dedup, `run_args` argv construction, `build_env`, and `prepare_home`. The C2 provider suite pins cache behaviour, `run_action` contracts, `build_env` environment construction, `resolve`/`find_script`, budget reading, `choose_provider` routing, auth checking, and config folding.

**Behaviours pinned as WRONG.** F100 has a deliberately-red test (`test_two_unrelated_tests_sharing_a_provider_name_bleed_through_the_cache`) as standing evidence. The C1 adversary's mutation tests (F50–F54) pin the gap between pattern generation and directive enforcement. The C2 auth characterization pins all three shipped scripts' "presence = validity" pattern (F130, F131, F132).

**What could not be characterized at all.** C3's lifecycle (runner, driver, tree, watchdog, gitops) — the harness exists and is proven but no characterization ran. C2's adversary covered `budget.py`'s 70 tests only; the seam and auth surfaces (118 tests) have not been attacked. C4–C7 entirely.

---

## Confidence

| Evidence tier | Count | Share |
|---|---|---|
| reproduction | 57 | 84% |
| trace | 9 | 13% |
| opinion | 2 | 3% |
| **total** | **68** | |

57 of 68 findings (84%) are reproduced: the real production code was called against a temp directory or fake upstream, and the asserted behaviour was observed. 9 (13%) are traced: the code path is unambiguous from reading but a reproduction would require a cooperative upstream or conditions not available in the test environment. 2 (3%) are opinion: the auditor judged the shape of the code without confirming behaviour.

The seven adversary findings (F150–F156) are all reproduction — mutations applied to the real code and tested against the real suite. The trace findings are concentrated in C1's audit pass (F61, F62, F64, F70, F72) and C2's audit pass (F141, F142, F143).

---

## Result

**Path:** `context/review/REPORT.md`

**Total findings:** 68 distinct. Reconciliation: was 61; C2's adversary produced 7 mutations yielding 7 findings (F150–F156), of which two (F-A1, F-A2) restated F122 and were not refiled, so 7 new ids from 7 distinct defects. 61 + 7 = 68.

**By severity:** 3 critical, 16 high, 25 medium, 24 low. (Was 3/15/22/21; +1 high from F150, +3 medium from F151/F152/F154, +3 low from F153/F155/F156.)

**By evidence tier:** 57 reproduction, 9 trace, 2 opinion. (Was 50/9/2; +7 reproduction from F150–F156.)

**By context:** C1: 38, C2: 30 (23 + 7 from adversary), C3: 0, C4–C7: not reviewed.

**The six led with:** F50, F150, F1, F112, F120, F130+F131+F132+F140.

**Rewrites proposed:** 3 (F50, F10, F13).

### Defects in the review itself — resolved

1. **F74 — resolved.** Withdrawn by its own author during the audit run as a restatement of F33. The id was left behind in `C1-sandbox.md`'s severity and evidence tables (lines 249 and 257), where it still appears as a `medium`/`trace` entry; those two counts are each one too high. There is no F74.

2. **F134 — resolved.** Never existed. The findings in `C2-auth.md` were written in the order F130, F131, F132, F135, F133, F136, F137; 134 was simply skipped. Nothing was retracted.

3. **F33 / F73 — resolved as one defect.** F73 is marked `accepted` as the duplicate. F33 is the entry to act on; it has the named reproduction. The two disagree on both severity and class — F33 low/`correctness`, F73 medium/`security` — and that disagreement is deliberately recorded rather than resolved. Forwarding a variable the configuration explicitly blocked is arguably the security reading, and that is the one the reader should weigh.

4. **F71 / F24 — resolved as one defect.** F71 is marked `accepted` as the duplicate. F24 is the entry to act on: it carries a reproduction showing an HTML body containing an `sk-live-…` string passing through untouched, where F71 is only a trace. They disagree on severity (F24 high, F71 medium); the reproduction is the stronger evidence.

### Open question

5. **F131 — high severity, disposition accept.** `agy.sh check` authenticates on any non-empty token file, with no format or expiry check. The finding is high, its author proposed accepting it, and the reasoning offered (a token format that may not carry an expiry) is a judgement about the product, not about the code. The ledger keeps it `open`. Whether to accept a high-severity credential-validity defect is a decision for whoever owns this system, not something this review settles.

### Note on permissions

This reporter's `readonly_paths` was corrected to permit revision of `REPORT.md`, but the change may not be live in the running process yet. If this edit is reverted at the merge gate, that is the reason and not a mistake in the edit itself. The work is committed on branch `agents/reporter/15f9e7` and can be taken off by hand.
