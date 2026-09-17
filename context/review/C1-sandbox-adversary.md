# Adversary Findings — write_proxy_config Allowlist Enforcement

## Verdict

**REJECTED (1): The suite is not load-bearing for the security-critical configuration it generates.**

The 48-test suite thoroughly validates the filter pattern generation logic and correctly catches mutations to the regex construction. However, it completely fails to validate the tinyproxy.conf directives that determine whether the proxy operates as an allow-list or deny-all-by-default. A mutation that removes `FilterDefaultDeny Yes` — which inverts the entire security model from "deny everything except what's listed" to "allow everything except what's listed" — passes all 48 tests.

This is a critical gap: the suite would not notice if the proxy became an open relay.

---

## Findings

### F50 (CRITICAL): FilterDefaultDeny directive is untested

**Input:** Remove line 719 from `src/multiagents/executor/docker.py`:
```python
"FilterDefaultDeny Yes\n"
```

**Location:** `src/multiagents/executor/docker.py:719`

**Outcome:** All 48 tests pass. The suite does not verify that the generated tinyproxy.conf contains the directive that makes the proxy deny-by-default. Without this directive, tinyproxy defaults to allow-by-default, inverting the security model. The filter file becomes a deny-list instead of an allow-list.

**Severity:** Critical. This is the single directive that determines whether the proxy operates as a security boundary or an open relay.

---

### F51: FilterType directive is untested

**Input:** Change line 721 from `"FilterType ere\n"` to `"FilterType regex\n"` (BRE instead of ERE).

**Location:** `src/multiagents/executor/docker.py:721`

**Outcome:** All 48 tests pass. The generated filter patterns use ERE syntax (`|`, `+`, `?`, `()`), but the suite does not verify that tinyproxy is configured to interpret them as ERE. With BRE mode, these patterns would be interpreted differently or fail to compile.

**Severity:** High. The patterns would not work as intended.

---

### F52: FilterCaseSensitive directive is untested

**Input:** Change line 722 from `"FilterCaseSensitive Off\n"` to `"FilterCaseSensitive On\n"`.

**Location:** `src/multiagents/executor/docker.py:722`

**Outcome:** All 48 tests pass. DNS is case-insensitive, so the proxy must match hosts case-insensitively. The suite tests case-insensitive matching via the harness but does not verify the config directive that enables it.

**Severity:** Medium. Would cause legitimate hosts to be refused if their case differs from the allowlist.

---

### F53: FilterURLs directive is untested

**Input:** Change line 723 from `"FilterURLs Off\n"` to `"FilterURLs On\n"`.

**Location:** `src/multiagents/executor/docker.py:723`

**Outcome:** All 48 tests pass. The generated patterns match against the bare host (e.g., `example.com`), not the full URL. With `FilterURLs On`, tinyproxy would match against `http://example.com/path`, causing all patterns to fail.

**Severity:** High. All allowlist entries would silently stop working.

---

### F54: Filter file path is untested

**Input:** Change line 720 from `'Filter "/etc/tinyproxy/filter"\n'` to `'Filter "/etc/tinyproxy/filters"\n'` (note the trailing 's').

**Location:** `src/multiagents/executor/docker.py:720`

**Outcome:** All 48 tests pass. The suite does not verify that the path in the Filter directive matches the actual location where the filter file is written. tinyproxy would fail to load the filter rules, resulting in an empty allowlist (deny-all).

**Severity:** High. The proxy would deny all outbound traffic.

---

### F55: Null allowlist handling is untested

**Input:** Set `egress_allowlist: null` in the project config (instead of omitting it or setting it to `[]`).

**Location:** `src/multiagents/executor/docker.py:699`

**Outcome:** The code raises `TypeError: 'NoneType' object is not iterable` at `list(None)`. The `or []` guard on line 699 handles this case, but no test exercises it.

**Severity:** Low. The code handles it correctly, but the lack of test coverage means a regression would go unnoticed.

---

### F56 (minor): Filter file trailing newline is untested

**Input:** Remove the `+ "\n"` from line 708.

**Location:** `src/multiagents/executor/docker.py:708`

**Outcome:** All 48 tests pass. The filter file would not end with a newline. This is unlikely to cause issues (tinyproxy likely handles both cases), but POSIX text files should end with a newline.

**Severity:** Low. Cosmetic issue.

---

## Tests Committed

Two new test files:

1. **tests/test_adversary_allowlist_mutation.py** (13 tests)
   - Tests for each surviving mutation (F50-F56)
   - Tests for edge cases: None entries, integer entries, boolean entries
   - Tests for harness function return types

2. **tests/test_adversary_allowlist_fuzz.py** (23 tests)
   - Boundary values: consecutive dots, IPv6, very long hostnames, empty hosts
   - Unicode edge cases: NFC vs NFD normalization, zero-width characters, mixed scripts
   - Property-based checks: roundtrip, monotonicity, empty allowlist behavior
   - Random fuzzing with seed 42 (100 iterations)

**Command to run all adversary tests:**
```bash
uv run --frozen python -m pytest -q tests/test_adversary_allowlist_mutation.py tests/test_adversary_allowlist_fuzz.py
```

**All 36 adversary tests pass against the current code.**

---

## What the Suite Does Well

The existing 48-test suite thoroughly validates:
- Filter pattern generation logic (regex escaping, anchoring, subdomain matching)
- Allow/refuse decision correctness for various host patterns
- Edge cases in pattern matching (quantifiers, character classes, alternation)
- Malformed entries (unbalanced brackets, non-string entries)

The suite would catch any regression in the pattern generation logic itself.

---

## What the Suite Does Not Validate

The suite treats `write_proxy_config` as a black box that produces filter patterns, but does not validate:
- The tinyproxy.conf directives that determine the proxy's security posture
- The consistency between the Filter directive path and the actual filter file location
- The interaction between the generated patterns and the proxy configuration

This is a critical gap: the patterns are useless without the correct proxy configuration.

---

## Recommendation

Add tests that validate the complete tinyproxy.conf output, not just the filter patterns. Specifically:
- Assert that `FilterDefaultDeny Yes` is present
- Assert that `FilterType ere` is present
- Assert that `FilterCaseSensitive Off` is present
- Assert that `FilterURLs Off` is present
- Assert that the Filter directive path matches the actual filter file location

These tests would make the suite load-bearing for the security boundary it claims to test.

---

**VERDICT(rejected, 1): One critical defect — the suite would not notice if the proxy became an open relay.**
