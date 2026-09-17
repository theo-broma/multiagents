# C2 — Provider seam, quota and failover (auditor)

Findings from reading `budget.py`, `providers.py`, `scripts.py`, `auth.py`,
`catalog.py`, `models.py`, `supervisor.py`, `providers.yaml`, and the shipped
provider scripts. The existing 19 findings (F100–F137) are not refiled.

**Sound parts.** The atomic-write pattern (`write to .tmp; replace`) is used
correctly in `_shared_usage` (budget.py:387-391) and `save_local`
(catalog.py:87-90). The `fcntl`-based cross-process lock in `_shared_usage`
is held across the fetch, not across I/O — correct. `resolve_inheritance`
has a `seen` set preventing infinite `extends:` cycles. The `Budget.usable`
property correctly treats unknown headroom as usable (the module's stated
policy) while still respecting cooldown. The `choose_provider` chain walk
correctly breaks on `"defer"` and skips `allowed`-absent names.

---

**F140** — `_claude_token` returns a token as valid when `expiresAt` is present but non-numeric, because the `isinstance` guard skips the expiry check entirely
*Class:* bug
*Severity:* medium
*Where:* `src/multiagents/budget.py:183` (`if isinstance(expires_ms, (int, float)) and expires_ms / 1000.0 <= time.time():`)
*Evidence:* reproduction
*Proof:* `tests/test_c2_auditor_findings.py::test_claude_token_returns_token_when_expires_at_is_non_numeric`
*What happens:* If the credential file's `expiresAt` field is present but not a number — possible during a CLI update that changes format, a file-write race, or manual editing — the token is returned as valid with no expiry check at all. An expired token would then be sent to the usage API, getting a 401. The 401 is caught and degraded to `known=False`, so no routing hazard, but the function's stated contract ("return None if expired") is violated and the token is unnecessarily registered for redaction.
*Disposition:* fix
*Reasoning:* The guard should be `if expires_ms is not None:` and either parse the value or treat a non-numeric present expiry as "unknown, so don't return the token." Two lines, no behaviour change for the happy path.

**F141** — `detect_opencode_subscription` reads the auth.json file from disk on every call with no caching, doing redundant I/O and `register_literal` work on every `read_opencode` invocation
*Class:* performance
*Severity:* low
*Where:* `src/multiagents/budget.py:497-523` (`detect_opencode_subscription`), called from `budget.py:555` (`read_opencode`) every time `probe_opencode()` returns None (which is always today)
*Evidence:* trace — `read_opencode` (budget.py:550) calls `probe_opencode()` (always returns None, budget.py:547), then unconditionally calls `detect_opencode_subscription()` (budget.py:555), which opens and parses `~/.local/share/opencode/auth.json` (budget.py:509-512) and registers every secret-bearing string as a redaction literal (budget.py:519-522). This happens on every `read_provider` call for opencode, which happens on every agent spawn.
*What happens:* Disk I/O and string registration are repeated on every budget check for opencode, despite the auth.json file changing at most once per login (days or weeks). The credential file is small, so the absolute cost is low, but the pattern is wrong: a value that changes on the scale of days is re-read on the scale of seconds. If `register_literal` has non-trivial per-call overhead (it feeds a regex engine), it compounds.
*Disposition:* accept
*Reasoning:* Low severity because the file is tiny and the I/O is cached by the kernel. The right fix is a module-level cache with a short TTL or an mtime check, but that's optimisation, not correctness. Not worth changing before a measured need.

**F142** — `read_claude` sets `source="cachedUsageUtilization"` at construction time, so when the cache file is missing AND the API fetch fails the returned Budget claims a source that never existed
*Class:* maintainability
*Severity:* low
*Where:* `src/multiagents/budget.py:408` (`budget = Budget(provider="claude", known=False, source="cachedUsageUtilization")`), `budget.py:433-437` (fetch fails, no cached utilization: `budget.note = note` then `return budget` — `source` unchanged)
*Evidence:* trace — `budget.py:408` creates the Budget with `source="cachedUsageUtilization"` before any file is read; `budget.py:417-420` reads the state file (may fail, setting only `note`); `budget.py:428-434` attempts the API fetch (may fail, sets `note` if no cached data); `budget.py:435-437` returns the Budget with `known=False`, `source="cachedUsageUtilization"`, and a note — even though neither the cache nor the API provided data.
*What happens:* A caller inspecting `budget.source` sees `"cachedUsageUtilization"` and may conclude the number came from the CLI's cache file, when in fact nothing was read. `known=False` prevents routing errors, but diagnostic tooling that groups by `source` would misattribute these "no data" Budgets as cache reads. The `note` field carries the real explanation, so nothing is silently lost — but the `source` field is actively misleading.
*Disposition:* accept
*Reasoning:* `known=False` plus the `note` make the correct answer available to any caller that checks them. `source` is cosmetic here. Worth fixing by deferring the `source` assignment until after the cache read succeeds (default to `""` or `"none"`), but low urgency.

**F143** — `refresh_models` writes `models.yaml` directly to the target path with no atomic rename, so a crash or kill mid-write leaves a corrupt or truncated file that the next reader will see
*Class:* bug
*Severity:* low
*Where:* `src/multiagents/models.py:66-71` (`with target.open("w") as handle: handle.write(header); yaml.safe_dump(...)`), contrasted with the correct atomic pattern in `budget.py:387-391` and `catalog.py:87-90` (write to `.tmp`, then `replace`)
*Evidence:* trace — `models.py:66` opens the target file for writing (truncating it immediately); `models.py:67-71` writes the header and YAML dump; if the process is killed between truncate and completion, the file on disk is empty or partial. A subsequent `load_local` in `catalog.py` or a manual reader would get a parse error or partial data.
*What happens:* If `refresh-models` is interrupted (OOM, SIGKILL, disk full), `models.yaml` is left corrupt. The next session that reads it — or `check_model_catalog` which loads the local snapshot — would see a broken file. The fix is the same atomic pattern used elsewhere: write to `.tmp`, then `tmp.replace(target)`.
*Disposition:* fix
*Reasoning:* One-liner to switch to the `.tmp`/`replace` pattern already used in this codebase (catalog.py:87-90, budget.py:387-391). Low severity because `refresh-models` is a maintenance command, not a hot-path operation, but a corrupt models.yaml would break model validation until manually repaired.

## Result

**Path:** `context/review/C2-audit.md`
**Id range:** F140–F143 (4 findings)

**Counts by severity:**
- medium: 1 (F140)
- low: 3 (F141, F142, F143)

**Counts by evidence tier:**
- reproduction: 1 (F140)
- trace: 3 (F141, F142, F143)

**Counts by class:**
- bug: 2 (F140, F143)
- performance: 1 (F141)
- maintainability: 1 (F142)

**Finding I would insist on:** F140 — the token expiry bypass is the only finding here with a correctness impact on a security-sensitive path. It's two lines to fix and prevents a class of silent failure (returning an unverified-expired token as valid).

**What I did not get to:** I did not deeply audit the supervisor.py event-loop logic or the `parse_models` provider method for edge cases in model-list parsing, as the brief emphasised concurrency/caching/error-path defects which I prioritised. The provider-plugin invariant (no hardcoded provider names outside the baseline) was checked via grep; all hits fall within the 5 already-argued sites from the brief, so no new finding was filed there.
