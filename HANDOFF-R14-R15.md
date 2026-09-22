# Handoff — R14/R15 implementation plan

Written by `implementer` run `ag-cbf6c0`, which read the full 902-line test
file and designed the change, then was cut off by a quota window before
writing any code. No commits, no `src/` changes. The plan below was checked
against the tests line by line rather than guessed.

Two things in it are findings rather than plan, and are worth keeping: the
two extra characterization tests it expects to go red beyond the named five,
and its *hypothesis* — explicitly flagged as needing verification — that
`tests/test_char_c1_allowlist.py` stays green.

---

No code changes were made — the worktree is clean (verified via `git status --short` and `git diff --stat`, both empty). All work so far was research/reading; nothing to commit.

## Handoff

**Task:** Implement R14 and R15 of `context/specs/phase2-entry-semantics.md` (+ both amendments at the end, which win over the main body).

**Status: design complete, zero code written.** Next run should implement directly from the plan below rather than re-reading everything.

### What I worked out (not yet in any file)

**R14** — in `src/multiagents/executor/docker.py`, method `write_proxy_config` (~line 719-731): change the per-entry pattern choice. Currently every entry gets `(^|\.)ESCAPED$`. New rule:
- entry has no `.` → exact: `^ESCAPED$`
- entry has `.` and is a valid IPv4 literal → exact: `^ESCAPED$`
- otherwise (has `.`, not IPv4) → unchanged suffix: `(^|\.)ESCAPED$`

`write_proxy_config` must **not** gain a raise — it just changes which pattern string it emits, no validation, no exceptions. This is explicit in the contract (a decided boundary).

**R15** — new validation, but the seam is `DockerExecutor.preflight()` (line ~1054), **not** `config.load()` — the first amendment moved it there and added scope: `multiagents docker up` (`cli.py:2193`, `cmd_docker`) must also call `preflight()` and refuse before `ensure_running()`, printing the same `{"ok": False, "error": ...}` shape it already uses and exiting 1, with nothing started.

Validate only when `self.network_mode == "allowlist"` (not `bridge`/`none` — confirmed by amendment 4).

Designed a `_validate_allowlist_entry(entry) -> str | None` returning a problem message or `None`, checked in this order (order matters for which vocabulary word wins):
1. `not entry.strip()` → empty (also covers whitespace-only strings) — message must contain "empty"
2. `entry != entry.strip()` → whitespace — message must contain one of whitespace/space/blank/tab, and must contain `entry.strip()` as a literal substring (not the raw entry — tabs don't appear verbatim, use `repr()`)
3. `entry.startswith(".")` or `.endswith(".")` → dot — message needs "dot" or "period"
4. IPv6 literal (bare or bracketed — strip optional `[...]`, try `ipaddress.IPv6Address`) → message needs "ipv6" or "address", and must **not** contain the word "port" (word-boundary check, not substring — "not supported" must still pass)
5. `"://" in entry` → URL — message needs "url" or "scheme"
6. valid IPv4 literal (proper regex, each octet 0-255) → **valid**, return `None`
7. (optional, untested but cheap correctness) dotted all-digit string that fails IPv4 range check → treat as malformed address
8. matches `.+:\d+` (host:port shape) → port — message needs "port"
9. hostname grammar check: split on `.`, each label must match `^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$` — if any label fails, generic "not a valid hostname" message naming the entry (this catches all the `NOT_HOSTNAMES` cases: `a(b`, `a[b`, `a(b)c`, `a[bc]d`, `*.example.com`, `a*b`, `evil.com|.*`, interior space, path-without-scheme, userinfo — all fail because of disallowed characters in a label)
10. else → valid, `None`

Every non-empty problem message must also contain the (stripped) entry text as a literal substring — verified against the test file's assertions.

Wire into `preflight()`: after the existing `proxy_image` check, when `network_mode == "allowlist"`, extend `problems` with one message per malformed entry from `self.config.get("egress_allowlist")`, skipping non-string entries (F2/F55, explicitly out of scope — don't touch).

In `cli.py` `cmd_docker`, `action == "up"` branch (~line 2193): call `ex.preflight()` first; if non-empty, print `{"ok": False, "error": "; ".join(problems)}` and `return 1` before calling `ensure_running()`.

### Test file read in full: `tests/test_phase2_entry_semantics.py` (902 lines, read-only, do not edit)

Confirmed the plan against every test in it (R14 tests, R15 malformed-form tests, the `_MALFORMED_PARAMS` word-vocabulary table, the four amendment-driven test blocks: scope-of-r15/`NOT_HOSTNAMES`, IPv4-valid, IPv6-malformed, empty-string, bridge/none-skip). The design above should satisfy all of them — this was checked line-by-line, not guessed.

### Expected side effects, already investigated

- `tests/test_c1_allowlist_characterization.py`: 5 F11-reproduction tests (leading/trailing dot, whitespace, port, scheme+path) will go red — expected, per the task. Additionally I found `test_ip_address_entry_matches_the_exact_address_and_dotted_suffixes` (asserts today's buggy IP-as-suffix behavior) and `test_empty_string_entry_produces_a_pattern_matching_only_a_trailing_dot` (asserts the exact old pattern text `(^|\.)$` for an empty entry) will **also** go red as a direct, correct consequence of the R14 change — not one of the "named five" but same category (pins dead/buggy behavior). Report these honestly when the suite is run; don't touch the file.
- `tests/test_char_c1_allowlist.py`: traced through all 5 tests by hand — every entry in it is dotless (`)`, `]`, `a(b)c`, `a[bc]d`, `a{2}b`) and none of its assertions depend on subdomain/suffix matching, so switching dotless entries from suffix-pattern to exact-pattern should **not** change any result. My working hypothesis is this file stays green despite the task text implying it also goes red — **verify this by actually running it**, don't assume either way.

### Not yet done
- No code written in `docker.py` or `cli.py`.
- Suite not run even once this session.
- No commits.

### Next steps for whoever resumes
1. Implement `_is_ipv4_literal`, `_is_ipv6_literal`, `_LABEL_RE`/hostname-grammar check, and `_validate_allowlist_entry` near `_ere_literal` (~line 100) in `docker.py`.
2. Change `write_proxy_config`'s pattern-selection loop per the R14 rule above.
3. Extend `preflight()` to call the new validator under `network_mode == "allowlist"`.
4. Update `cli.cmd_docker`'s `"up"` branch to preflight-gate before `ensure_running()`.
5. Run `uv run --frozen python -m pytest -q tests/test_phase2_entry_semantics.py` and iterate — commit as soon as R14's tests are green, then again once R15's are, per the "commit as you go" instruction (three prior runs on this pipeline died mid-flight; commit early and often).
6. Run the two characterization files and report exactly what went red — don't touch them regardless of outcome.
7. Run `tests/test_phase2_ere_escaping.py tests/test_phase2_proxy_directives.py` to confirm they're still the required 22 passed.
