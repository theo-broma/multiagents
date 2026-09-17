# Review — multiagents

## What this covers

Seven bounded contexts were mapped (`context/review/MAP.md`). Two were reviewed in full, one was reviewed partially, and four were not reviewed at all.

| Context | Harness | Characterization | Audit | Adversary | Findings |
|---|---|---|---|---|---|
| **C1** sandbox & egress | done | 3 surfaces | done | done | 39 |
| **C2** provider seam | done | 3 surfaces | done | **not covered** | 23 |
| **C3** agent lifecycle | done | **not covered** | **not covered** | **not covered** | 0 |
| C4–C7 | mapped, not started | — | — | — | — |

**62 distinct findings** across C1 and C2. C3's harness is merged and proven (7 green proof tests) but nothing beyond that ran. C4–C7 were ranked and budgeted but never reached.

**What was NOT reviewed and why.** C2's adversary and C3's characterization/audit/adversary were planned and budgeted. Both hit their budget ceiling — but not from the volume of work. Every ceiling here was exhausted by **cache-read token accounting on single runs**: `ctx-lifecycle` recorded 2,043,007 tokens against a 400,000 ceiling from **one** harness run, of which 1,939,456 were cache reads at a real cost of $0.51. Meanwhile the `claude` provider reports no token total at all and counted as zero against its tags throughout. This is a defect in the tooling (`bug-565863`), not a statement about the reviewed code. The brake is absent where the money is and overwhelming where it is not.

**The gap worth naming.** On C1, the adversary produced the review's most consequential single finding — F50, that deleting one line from `write_proxy_config` inverts the proxy from allow-list to open relay while all 48 characterization tests continue to pass. C2 now carries 188 characterization tests that **nobody has asked the same question of**. Whether C2's suite is load-bearing is unknown. A reader deciding what to do next should know this specific question is open rather than answered in the negative.

**Two facts about the test suite.** The repository's suite is green in no environment: on a host with docker it fails one test (`test_the_claude_script_uses_the_container_profile_only_where_it_should`), and inside an agent container it fails seventeen different ones (`PermissionError: ... can_spawn is false`). The documented baseline of "545 passed / 3 skipped" describes neither. One test is deliberately red as standing evidence for F100. The review added roughly 380 characterization tests across C1 and C2, and they pass.

---

## The five that matter

These are the findings that would change what someone does this week. Ranked by consequence, not severity label.

1. **F50** (critical, rewrite) — Deleting `FilterDefaultDeny Yes` from `write_proxy_config` inverts the proxy from allow-list to deny-list (open relay). All 48 characterization tests pass. The suite is not load-bearing for the security-critical directive it generates.

2. **F1** (critical) — An `egress_allowlist` entry containing `|` turns the allowlist into an allow-all. The regex escapes only the literal dot; POSIX ERE alternation has the lowest precedence of any operator, so `evil.com|.*` produces a pattern that matches any host. Confirmed by reproduction against the real `write_proxy_config`.

3. **F112** (high) — `scripts.build_env` copies the full ambient process environment (`dict(os.environ)`) into every provider script invocation, unfiltered. Inside an agent container this includes `CLAUDE_CODE_MESSAGING_TOKEN`, `ANTHROPIC_BASE_URL`, sandbox proxy URLs, and `MULTIAGENTS_ROOT`. A project-local provider script (which wins by precedence) receives the calling process's live credentials.

4. **F120** (critical) — Multi-account claude quota reading is unreachable. `read_claude(config_dir=...)` exists specifically to read per-account credential files, but every real invocation goes through `claude.sh`'s `exit 64` fallback to `builtin()` called with **zero arguments**, silently discarding the caller's `config_dir`. The `config_dir` parameter is dead code from every real caller today.

5. **F130 + F131 + F132 + F140** (high/high/high/medium) — All three shipped provider scripts and the Python `_claude_token` function treat credential-file presence as validity. A corrupt, truncated, or format-changed credentials file that the code cannot read a clock from is reported as "logged in" / "valid token." The runner discovers the truth only when the agent's first turn 401s.

---

## By context

### C1 — Sandbox and egress boundary

Complete review: harness, three characterization surfaces (allowlist, auth-proxy, executor), adversary (mutation + fuzzing), and structural audit. 39 findings.

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
| F71 | medium | security | trace | `_scrub` returns non-JSON error bodies completely unredacted | fix |
| F73 | medium | security | trace | `build_env` forwards BASE_ENV_KEYS even when named in `blocked` | fix |
| F22 | low | correctness | reproduction | Error passthrough drops all upstream response headers | fix |
| F23 | low | bug | reproduction | Every HTTP verb forwarded upstream as POST, including literal GET | fix |
| F32 | low | correctness | reproduction | `pids_limit`/`cpus`/`memory` of 0 silently omitted; negative passed unvalidated | fix |
| F33 | low | correctness | reproduction | `build_env` blocked list doesn't guard base keys (same defect as F73) | fix |
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

Complete except adversary: harness, three characterization surfaces (provider, seam, budget + auth), and structural audit. 23 findings. **No mutation testing was done on C2.**

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

**4. build_env inconsistencies across C1 and C2.** F33/F73 (C1: `build_env` in `base.py` forwards BASE_ENV_KEYS even when named in `blocked`) and F112 (C2: `build_env` in `scripts.py` copies the full ambient environment to provider scripts). Two different `build_env` functions in two different contexts, both with the same shape: the environment construction does not enforce the boundary its docstring or contract claims.

**5. Cache and mutable-state aliasing.** F100 (`budget._cache` keyed only by provider name, causing test-state bleed) and F122 (cache hit mutates the shared cached `Budget` object, overwriting `spent` instead of merging). Module-level mutable state shared across callers without isolation or copy-on-read.

---

## Health

**Coverage as it stands.** C1's modules: `docker.py` 66%, `base.py` 76%, `authproxy.py` 50% (the lowest of any non-read-UI module). C2's modules: `budget.py` 81%, `providers.py` 93%, `scripts.py` 87%, `auth.py` 77%, `catalog.py` 84%, `supervisor.py` 93%. C3's `runner.py` is 61% with 435 uncovered statements — the largest absolute block of untested logic in the codebase.

**What the characterization suite pinned.** Roughly 380 new tests across C1 and C2, all passing. The C1 allowlist suite (48 tests) thoroughly validates pattern generation but does not validate the tinyproxy directives. The C1 auth-proxy suite pins header/body/query forwarding, account-credential edge cases, rate-limit sequencing, and the scrub boundary. The C1 executor suite pins mount dedup, `run_args` argv construction, `build_env`, and `prepare_home`. The C2 provider suite pins cache behaviour, `run_action` contracts, `build_env` environment construction, `resolve`/`find_script`, budget reading, `choose_provider` routing, auth checking, and config folding.

**Behaviours pinned as WRONG.** F100 has a deliberately-red test (`test_two_unrelated_tests_sharing_a_provider_name_bleed_through_the_cache`) as standing evidence. The C1 adversary's mutation tests (F50–F54) pin the gap between pattern generation and directive enforcement. The C2 auth characterization pins all three shipped scripts' "presence = validity" pattern (F130, F131, F132).

**What could not be characterized at all.** C3's lifecycle (runner, driver, tree, watchdog, gitops) — the harness exists and is proven but no characterization ran. C2's adversary — whether C2's 188 tests are load-bearing in the same sense as C1's 48 is unknown. C4–C7 entirely.

---

## Confidence

| Evidence tier | Count | Share |
|---|---|---|
| reproduction | 50 | 81% |
| trace | 10 | 16% |
| opinion | 2 | 3% |
| **total** | **62** | |

50 of 62 findings (81%) are reproduced: the real production code was called against a temp directory or fake upstream, and the asserted behaviour was observed. 10 (16%) are traced: the code path is unambiguous from reading but a reproduction would require a cooperative upstream or conditions not available in the test environment. 2 (3%) are opinion: the auditor judged the shape of the code without confirming behaviour.

The trace findings are concentrated in C1's audit pass (F61, F62, F64, F70, F71, F72, F73) and C2's audit pass (F141, F142, F143). F71 is justified as trace: non-JSON bodies with embedded secrets cannot be reproduced without a cooperative upstream, and the code path at `authproxy.py:367` is unambiguous.

---

## Result

**Path:** `context/review/REPORT.md`

**Total findings:** 62 distinct (after deduplication of F33/F73, which are the same defect).

**By severity:** 3 critical, 15 high, 24 medium, 20 low.

**By evidence tier:** 50 reproduction, 10 trace, 2 opinion.

**By context:** C1: 39, C2: 23, C3: 0, C4–C7: not reviewed.

**The five led with:** F50, F1, F112, F120, F130+F131+F132+F140.

**Rewrites proposed:** 3 (F50, F10, F13).

### Defects in the review itself

1. **F74 is referenced but missing.** `C1-sandbox.md`'s severity table lists F74 among the 7 medium findings (F63, F64, F66, F67, F71, F73, F74), but the finding itself does not appear in the file. The file jumps from F73 to F75. Either F74 was lost during writing or the severity table is wrong.

2. **F134 is missing from C2-auth.md.** The IDs jump from F133 to F135. Either F134 was never filed or was retracted without recording the retraction.

3. **F33 and F73 are the same defect.** Both describe `build_env` in `base.py` forwarding BASE_ENV_KEYS even when named in `blocked`. F33 (C1-sandbox-executor.md, low, reproduction) and F73 (C1-sandbox.md, medium, trace) are the same finding filed twice by two different agents with different severity assessments. The report carries both IDs and notes the duplication; the implement team should fix one.

4. **F71 and F24 overlap.** F71 (C1-sandbox.md, medium, security, trace) describes the security consequence of `_scrub` returning non-JSON bodies unredacted. F24 (C1-sandbox-authproxy.md, high, security, reproduction) describes the mechanism by which `_scrub` is a no-op on non-JSON. Same root cause, different angles, different severity. Both are carried; the implement team should treat them as one fix.

5. **F131 has severity high but disposition accept.** `agy.sh check` authenticates on any non-empty token file — filed as high severity but the auditor downgraded the disposition to `accept` because the token format may not carry an expiry. This is internally consistent (the finding is real, but unfixable without format changes) but worth flagging: a high-severity finding with disposition `accept` is unusual and the reader should know the auditor's reasoning.
